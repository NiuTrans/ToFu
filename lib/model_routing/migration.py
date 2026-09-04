"""One-way legacy provider/catalog import into `tofu.model-routing/v2`.

Migration is deliberately explicit: callers first build a redacted plan, then
write encrypted secrets, validate the staged aggregate, and finally commit one
revision with a recovery receipt.  Legacy documents are never runtime fallback
state and no v2 change is projected back into ``providers[].models``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import urlparse

from lib.log import get_logger
from lib.model_catalog import _creator_identity

from .domain import (
    CONTRACT_VERSION,
    ModelRef,
    ModelRoutingError,
    empty_document,
    normalize_document,
)
from .repository import OwnerBoundary, RepositoryPort, StoredAuthority


_DEFAULT_CONTEXT_WINDOW = 128_000
logger = get_logger(__name__)


def _stable_id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256(
        "\0".join(str(part or "") for part in parts).encode("utf-8")
    ).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _clean_id(value: object, fallback: str) -> str:
    text = str(value or "").strip()
    if text and len(text) <= 256 and not any(ord(ch) < 32 for ch in text):
        return text
    return fallback


def _clean_strings(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(
        text for value in values
        if (text := str(value or "").strip())
    ))


def _protocol(value: object, base_url: str = "") -> str:
    text = str(value or "").strip().lower()
    if text in {"responses", "openai-responses", "openai_responses"}:
        return "openai_responses"
    if text in {"anthropic", "local", "custom", "openai"}:
        return text
    if "anthropic" in base_url.lower():
        return "anthropic"
    return "openai"


def _pricing(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    input_price = value.get("input")
    output_price = value.get("output")
    if isinstance(input_price, bool) or not isinstance(input_price, (int, float)):
        return None
    if isinstance(output_price, bool) or not isinstance(output_price, (int, float)):
        return None
    result: dict[str, Any] = {
        "input": max(0.0, float(input_price)),
        "output": max(0.0, float(output_price)),
        "currency": str(value.get("currency") or "USD").upper(),
        "unit": "per_million_tokens",
    }
    for source, target in (
        ("cache_read", "cache_read"),
        ("cacheRead", "cache_read"),
        ("cache_write", "cache_write"),
        ("cacheWrite", "cache_write"),
    ):
        amount = value.get(source)
        if isinstance(amount, (int, float)) and not isinstance(amount, bool):
            result[target] = max(0.0, float(amount))
    return result


def _context_window(row: Mapping[str, Any]) -> int:
    value = row.get("context_window") or row.get("context")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return _DEFAULT_CONTEXT_WINDOW


def _quality_rank(row: Mapping[str, Any]) -> float:
    profile = row.get("capability_profile")
    if isinstance(profile, Mapping):
        quality = profile.get("quality_rank") or profile.get("intelligence")
        if isinstance(quality, (int, float)) and not isinstance(quality, bool):
            return float(quality)
        label = str(profile.get("quality") or "").lower()
        return {"heavy": 80.0, "medium": 50.0, "light": 25.0}.get(label, 50.0)
    quality = row.get("quality_rank")
    return float(quality) if isinstance(quality, (int, float)) and not isinstance(quality, bool) else 50.0


def _redacted_legacy(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key in {"api_key", "api_keys", "secret", "token", "password"}:
                if isinstance(child, list):
                    result[key] = ["***" for _ in child]
                else:
                    result[key] = "***" if child else ""
            else:
                result[str(key)] = _redacted_legacy(child)
        return result
    if isinstance(value, list):
        return [_redacted_legacy(item) for item in value]
    return copy.deepcopy(value)


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class MigrationIssue:
    severity: str
    code: str
    message: str
    path: str = ""
    candidates: tuple[dict[str, str], ...] = ()

    def public_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        if self.path:
            result["path"] = self.path
        if self.candidates:
            result["candidates"] = [dict(candidate) for candidate in self.candidates]
        return result


@dataclass(frozen=True, slots=True, repr=False)
class PendingSecret:
    credential_id: str
    secret_reference: str
    plaintext: str
    key_hint: str

    def __repr__(self) -> str:
        return (
            "PendingSecret(credential_id=%r, secret_reference=%r, "
            "plaintext=<redacted>, key_hint=%r)"
            % (self.credential_id, self.secret_reference, self.key_hint)
        )


@dataclass(slots=True)
class MigrationPlan:
    document: dict[str, Any]
    issues: list[MigrationIssue]
    source_digest: str
    redacted_backup: dict[str, Any]
    _secrets: list[PendingSecret] = field(default_factory=list, repr=False)

    @property
    def blocking_issues(self) -> list[MigrationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    def public_dict(self) -> dict[str, Any]:
        return {
            "contract_version": CONTRACT_VERSION,
            "source_digest": self.source_digest,
            "entity_counts": {
                key: len(self.document[key])
                for key in (
                    "creators", "models", "providers", "provider_accesses",
                    "connections", "credentials", "offerings", "deployments",
                )
            },
            "secrets": [
                {
                    "credential_id": secret.credential_id,
                    "secret_reference": secret.secret_reference,
                    "key_hint": secret.key_hint,
                }
                for secret in self._secrets
            ],
            "issues": [issue.public_dict() for issue in self.issues],
            "ready": not self.blocking_issues,
        }


@dataclass(frozen=True, slots=True)
class MigrationResult:
    enabled: bool
    authority: StoredAuthority | None
    receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _IdentityMatch:
    state: str
    ref: ModelRef | None
    model: dict[str, Any] | None
    confidence: str
    candidates: tuple[dict[str, str], ...] = ()


def _official_rows(
    official_directory: Sequence[Mapping[str, Any]] | None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    exact: dict[str, list[dict[str, Any]]] = {}
    bare: dict[str, list[dict[str, Any]]] = {}
    for source in official_directory or ():
        if not isinstance(source, Mapping):
            continue
        model_id = str(source.get("model_id") or source.get("id") or source.get("md_id") or "").strip()
        creator_id = str(source.get("creator_id") or source.get("family") or "").strip()
        if not model_id or not creator_id:
            continue
        row = {
            "creator_id": creator_id,
            "model_id": model_id,
            "display_name": str(source.get("display_name") or source.get("name") or model_id),
            "capabilities": _clean_strings(source.get("capabilities")) or ["text"],
            "context_window": _context_window(source),
            "quality_rank": _quality_rank(source),
        }
        list_pricing = _pricing(source.get("list_pricing") or source.get("pricing"))
        if list_pricing is not None:
            row["list_pricing"] = list_pricing
        if source.get("lifecycle") in {"stable", "preview", "dated_snapshot", "retired"}:
            row["lifecycle"] = source["lifecycle"]
        exact.setdefault(model_id, []).append(row)
        bare.setdefault(_creator_identity.strip_routing_decoration(model_id), []).append(row)
    return exact, bare


def _identity_match(
    model_id: str,
    *,
    exact: Mapping[str, list[dict[str, Any]]],
    bare: Mapping[str, list[dict[str, Any]]],
    seed: Mapping[str, Any],
) -> _IdentityMatch:
    direct = exact.get(model_id) or []
    if len(direct) == 1:
        row = copy.deepcopy(direct[0])
        return _IdentityMatch(
            "confirmed", ModelRef(row["creator_id"], row["model_id"]), row, "high")
    stripped = _creator_identity.strip_routing_decoration(model_id)
    decorated = stripped != model_id
    decorated_matches = bare.get(stripped) or []
    unique_decorated = {
        (row["creator_id"], row["model_id"]): row for row in decorated_matches
    }
    if decorated and len(unique_decorated) == 1:
        row = copy.deepcopy(next(iter(unique_decorated.values())))
        return _IdentityMatch(
            "confirmed", ModelRef(row["creator_id"], row["model_id"]), row, "high")
    candidates = tuple(
        {"creator_id": creator_id, "model_id": official_model_id}
        for creator_id, official_model_id in sorted(unique_decorated)
    )
    if len(direct) > 1 or len(unique_decorated) > 1:
        return _IdentityMatch("pending_identity", None, None, "pending", candidates)

    # Known creator decoration is itself accepted migration evidence. This
    # path never removes preview/date suffixes: strip_routing_decoration keeps
    # them, so snapshots remain distinct identities.
    family = _creator_identity.creator_family(model_id)
    if family:
        official_id = stripped
        capabilities = _clean_strings(seed.get("capabilities")) or ["text"]
        row = {
            "creator_id": family,
            "model_id": official_id,
            "display_name": str(seed.get("display_name") or official_id),
            "capabilities": capabilities,
            "context_window": _context_window(seed),
            "quality_rank": _quality_rank(seed),
            "lifecycle": (
                "preview" if "preview" in official_id.lower()
                else "dated_snapshot" if any(
                    part.isdigit() and len(part) == 8
                    for part in official_id.replace("_", "-").split("-"))
                else "stable"
            ),
        }
        list_pricing = _pricing(seed.get("pricing"))
        if list_pricing is not None:
            row["list_pricing"] = list_pricing
        return _IdentityMatch(
            "confirmed", ModelRef(family, official_id), row, "high")
    return _IdentityMatch("pending_identity", None, None, "pending")


def _legacy_models_for_provider(
    provider: Mapping[str, Any], model_catalog: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    models = provider.get("models")
    if isinstance(models, list) and models:
        return [copy.deepcopy(dict(row)) for row in models if isinstance(row, Mapping)]
    if not isinstance(model_catalog, Mapping):
        return []
    provider_id = str(provider.get("id") or provider.get("key") or provider.get("brand") or "")
    offerings = model_catalog.get("offerings")
    if not isinstance(offerings, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for offering in offerings.values():
        if not isinstance(offering, Mapping) or str(offering.get("provider_id") or "") != provider_id:
            continue
        configuration = offering.get("configuration")
        row = copy.deepcopy(dict(configuration)) if isinstance(configuration, Mapping) else {}
        row["model_id"] = str(offering.get("model_id") or "")
        row["enabled"] = bool(offering.get("enabled", True))
        rows.append(row)
    return rows


def _provider_sources(
    server_config: Mapping[str, Any],
    byo_providers: Sequence[Mapping[str, Any]] | None,
) -> list[tuple[dict[str, Any], str]]:
    sources: list[tuple[dict[str, Any], str]] = []
    providers = server_config.get("providers") or []
    if isinstance(providers, list):
        sources.extend(
            (copy.deepcopy(dict(provider)), "public")
            for provider in providers if isinstance(provider, Mapping)
        )
    sources.extend(
        (copy.deepcopy(dict(provider)), "owner")
        for provider in (byo_providers or ()) if isinstance(provider, Mapping)
    )
    return sources


def plan_legacy_migration(
    server_config: Mapping[str, Any],
    *,
    byo_providers: Sequence[Mapping[str, Any]] | None = None,
    official_directory: Sequence[Mapping[str, Any]] | None = None,
) -> MigrationPlan:
    """Build a deterministic, redacted migration plan without writes."""
    if not isinstance(server_config, Mapping):
        raise ModelRoutingError("server_config must be an object")
    redacted_backup = {
        "server_config": _redacted_legacy(server_config),
        "byo_providers": _redacted_legacy(list(byo_providers or ())),
    }
    source_digest = _digest(redacted_backup)
    document = empty_document()
    issues: list[MigrationIssue] = []
    pending_secrets: list[PendingSecret] = []
    exact, bare = _official_rows(official_directory)
    model_catalog = server_config.get("model_catalog")
    if not isinstance(model_catalog, Mapping):
        model_catalog = None

    provider_ids: set[str] = set()
    provider_identity_to_access: dict[tuple[str, str], str] = {}
    creator_ids: set[str] = set()
    models_by_ref: dict[ModelRef, dict[str, Any]] = {}
    wire_ids: dict[tuple[str, str], str] = {}

    for provider_index, (legacy, scope) in enumerate(_provider_sources(server_config, byo_providers)):
        legacy_id = str(legacy.get("id") or legacy.get("key") or "").strip()
        name = str(legacy.get("name") or legacy.get("label") or legacy_id or "Provider").strip()
        base_url = str(legacy.get("base_url") or "").strip().rstrip("/")
        host = (urlparse(base_url).hostname or "").lower()
        identity_key = (scope, legacy_id or f"{name.casefold()}@{host}")
        if identity_key in provider_identity_to_access:
            issues.append(MigrationIssue(
                "warning", "duplicate_provider_merged",
                f"duplicate provider {name!r} was folded into one ProviderAccess",
                f"providers[{provider_index}]",
            ))
            # Duplicate public cards may be protocol faces. Reprocessing their
            # models against the first aggregate would require preserving a
            # second credential pool; keep them distinct unless IDs match.
            if not legacy_id:
                identity_key = (scope, f"{identity_key[1]}#{provider_index}")
            else:
                continue

        provider_id = _clean_id(
            legacy_id,
            _stable_id("provider", scope, name.casefold(), host),
        )
        if provider_id in provider_ids:
            provider_id = _stable_id("provider", scope, provider_id, provider_index)
        provider_ids.add(provider_id)
        access_id = _stable_id("access", provider_id)
        provider_identity_to_access[identity_key] = access_id
        document["providers"].append({
            "provider_id": provider_id,
            "name": name,
            "scope": scope,
            **({"brand": str(legacy["brand"])} if legacy.get("brand") else {}),
        })
        document["provider_accesses"].append({
            "provider_access_id": access_id,
            "provider_id": provider_id,
            "display_name": name,
            "enabled": not bool(legacy.get("disabled")) and bool(legacy.get("enabled", True)),
            "quota_policy": {
                **({"rpm": int(legacy["rpm"])} if isinstance(legacy.get("rpm"), int) and legacy["rpm"] >= 0 else {}),
                **({"balance": float(legacy["balance"])} if isinstance(legacy.get("balance"), (int, float)) and legacy["balance"] >= 0 else {}),
                **({"currency": str(legacy.get("currency") or "USD").upper()} if legacy.get("balance") is not None else {}),
            },
        })

        connection_by_face: dict[str, str] = {}
        connection_specs: list[tuple[str, dict[str, Any]]] = []
        if base_url:
            connection_specs.append(("default", {
                "base_url": base_url,
                "protocol": legacy.get("protocol"),
                "responses_profile": legacy.get("responses_profile"),
            }))
        faces = legacy.get("faces")
        if isinstance(faces, Mapping):
            for face_name, face in faces.items():
                if isinstance(face, Mapping):
                    connection_specs.append((str(face_name), dict(face)))
        endpoints = legacy.get("endpoints")
        if isinstance(endpoints, list):
            for endpoint_index, endpoint in enumerate(endpoints):
                if isinstance(endpoint, str):
                    connection_specs.append((f"endpoint_{endpoint_index}", {"base_url": endpoint}))
                elif isinstance(endpoint, Mapping):
                    connection_specs.append((
                        str(endpoint.get("id") or endpoint.get("name") or f"endpoint_{endpoint_index}"),
                        dict(endpoint),
                    ))
        if not connection_specs:
            connection_specs.append(("default", {"base_url": "local://identity", "protocol": "local"}))
        for connection_index, (face_name, spec) in enumerate(connection_specs):
            endpoint_url = str(spec.get("base_url") or base_url or "local://identity").strip().rstrip("/")
            connection_id = _stable_id("conn", access_id, face_name, endpoint_url)
            if face_name in connection_by_face:
                face_name = f"{face_name}_{connection_index}"
            connection_by_face[face_name] = connection_id
            document["connections"].append({
                "connection_id": connection_id,
                "provider_access_id": access_id,
                "base_url": endpoint_url,
                "protocol": _protocol(spec.get("protocol") or legacy.get("protocol"), endpoint_url),
                "enabled": not bool(spec.get("disabled")) and bool(spec.get("enabled", True)),
                "priority": int(spec.get("priority") or connection_index * 10 + 100),
                # Header values may themselves be credentials. Migration
                # places all of them in the independently encrypted secret
                # envelope instead of the inspectable Connection metadata.
                "extra_headers": {},
                **({"adapter": copy.deepcopy(
                    spec.get("adapter") or legacy.get("adapter"))}
                   if spec.get("adapter") or legacy.get("adapter") else {}),
                **({"region": str(spec["region"])} if spec.get("region") else {}),
                **({"gateway_namespace": str(face_name)} if face_name != "default" else {}),
            })

        raw_keys: list[str] = []
        if isinstance(legacy.get("api_keys"), list):
            raw_keys.extend(str(key or "").strip() for key in legacy["api_keys"])
        elif "api_key" in legacy:
            raw_keys.append(str(legacy.get("api_key") or "").strip())
        oauth = str(legacy.get("oauth") or "").strip()
        secret_headers = {
            str(key): str(value)
            for key, value in (legacy.get("extra_headers") or {}).items()
        } if isinstance(legacy.get("extra_headers"), Mapping) else {}
        credential_kind = "api_key"
        if oauth:
            raw_keys = [""]
            credential_kind = "oauth"
        elif secret_headers and not raw_keys:
            # Header-only gateways still need one encrypted Credential.  The
            # hint describes the non-empty secret envelope without exposing a
            # header name or value.
            raw_keys = [""]
        elif not any(raw_keys) and not secret_headers:
            raw_keys = [""]
            credential_kind = "local_identity"
        credentials_for_key: list[dict[str, Any]] = []
        for key_index, key in enumerate(raw_keys):
            credential_id = _stable_id("cred", access_id, key_index)
            reference = "" if credential_kind == "local_identity" else _stable_id("mrs", credential_id)
            hint = (
                (key[:4] + "…" + key[-4:] if len(key) > 8 else "***")
                if key else ("headers" if secret_headers else "")
            )
            credential = {
                "credential_id": credential_id,
                "provider_access_id": access_id,
                "kind": credential_kind,
                "secret_reference": reference,
                "key_hint": hint,
                "enabled": True,
                "authorization": {
                    "connection_ids": sorted(connection_by_face.values()),
                    "models": [],
                },
                "quota_policy": {
                    **({"rpm": int(legacy["rpm"])} if isinstance(legacy.get("rpm"), int) and legacy["rpm"] >= 0 else {}),
                },
            }
            document["credentials"].append(credential)
            credentials_for_key.append(credential)
            if reference:
                encrypted_value = json.dumps({
                    "format": "tofu.credential-secret/v1",
                    "api_key": key if credential_kind == "api_key" else "",
                    "oauth": oauth if credential_kind == "oauth" else "",
                    "extra_headers": secret_headers,
                }, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
                pending_secrets.append(PendingSecret(
                    credential_id, reference, encrypted_value, hint))

        models = _legacy_models_for_provider(legacy, model_catalog)
        for model_index, seed in enumerate(models):
            logical_id = str(seed.get("model_id") or seed.get("id") or "").strip()
            if not logical_id:
                issues.append(MigrationIssue(
                    "error", "model_id_missing", "legacy model has no model_id",
                    f"providers[{provider_index}].models[{model_index}]",
                ))
                continue
            identity = _identity_match(
                logical_id, exact=exact, bare=bare, seed=seed)
            capabilities = _clean_strings(seed.get("capabilities")) or ["text"]
            context_window = _context_window(seed)
            if identity.state == "confirmed" and identity.ref is not None and identity.model is not None:
                ref = identity.ref
                model_row = models_by_ref.get(ref)
                if model_row is None:
                    model_row = copy.deepcopy(identity.model)
                    model_row["capabilities"] = sorted(set(model_row["capabilities"]) | set(capabilities))
                    model_row["context_window"] = max(model_row["context_window"], context_window)
                    models_by_ref[ref] = model_row
                    if ref.creator_id not in creator_ids:
                        creator_ids.add(ref.creator_id)
                        document["creators"].append({
                            "creator_id": ref.creator_id,
                            "name": ref.creator_id.replace("_", " ").replace("-", " ").title(),
                        })
                    document["models"].append(model_row)
                else:
                    model_row["capabilities"] = sorted(set(model_row["capabilities"]) | set(capabilities))
                    model_row["context_window"] = max(model_row["context_window"], context_window)
                # An offering is an actual subset. Migration data can be more
                # expressive than an injected directory row, so the official
                # aggregate above first absorbs the known legacy facts.
                offering_model = ref.public_dict()
                pending_model_id = None
                identity_state = "confirmed"
            else:
                offering_model = None
                pending_model_id = logical_id
                identity_state = "pending_identity"
                issues.append(MigrationIssue(
                    "warning", "pending_identity",
                    f"model identity {logical_id!r} is not unambiguous; it remains provider-scoped",
                    f"providers[{provider_index}].models[{model_index}]",
                    identity.candidates,
                ))

            offering_identity = (
                f"{offering_model['creator_id']}/{offering_model['model_id']}"
                if offering_model else f"pending:{pending_model_id}"
            )
            offering_id = _stable_id("off", access_id, offering_identity)
            if any(row["offering_id"] == offering_id for row in document["offerings"]):
                issues.append(MigrationIssue(
                    "error", "duplicate_offering",
                    f"provider contains duplicate offering identity {offering_identity}",
                    f"providers[{provider_index}].models[{model_index}]",
                ))
                continue
            offering: dict[str, Any] = {
                "offering_id": offering_id,
                "provider_access_id": access_id,
                "identity_state": identity_state,
                "enabled": bool(seed.get("enabled", True)) and identity_state == "confirmed",
                "stale": bool(seed.get("catalog_retired") or seed.get("stale")),
                "capabilities": capabilities,
                "context_window": context_window,
                "priority": int(seed.get("priority") or 100),
            }
            if offering_model:
                offering["model"] = offering_model
            else:
                offering["pending_model_id"] = pending_model_id
            actual_pricing = _pricing(seed.get("pricing"))
            if actual_pricing is not None:
                offering["actual_pricing"] = actual_pricing
            document["offerings"].append(offering)

            # key_access grants the model to only the named key cells. Absent
            # key_access means every credential may use it.
            key_access = seed.get("key_access")
            for key_index, credential in enumerate(credentials_for_key):
                authorized = True
                if isinstance(key_access, Mapping):
                    cell = key_access.get(str(key_index), key_access.get(key_index))
                    authorized = isinstance(cell, Mapping) and not bool(cell.get("disabled"))
                if authorized and offering_model is not None:
                    credential["authorization"]["models"].append(copy.deepcopy(offering_model))

            explicit_wire_ids = _clean_strings(seed.get("request_ids"))
            if explicit_wire_ids:
                request_ids = explicit_wire_ids
            else:
                request_ids = _clean_strings([logical_id, *_clean_strings(seed.get("aliases"))])
            if not request_ids:
                request_ids = [logical_id]
            face_name = str(seed.get("face") or "default")
            # Some legacy templates represented one account with protocol
            # faces and selected the Anthropic wire implicitly by creator
            # family.  Preserve that measured transport requirement while
            # compiling faces into explicit v2 Connections.
            if (
                not seed.get("face")
                and _creator_identity.creator_family(logical_id) == "anthropic"
                and "anthropic" in connection_by_face
            ):
                face_name = "anthropic"
            connection_id = connection_by_face.get(face_name) or connection_by_face.get("default")
            if connection_id is None:
                connection_id = next(iter(connection_by_face.values()))
            for wire_index, wire_model_id in enumerate(request_ids):
                wire_key = (access_id, wire_model_id)
                deployment_id = _stable_id("dep", access_id, connection_id, wire_model_id)
                if wire_key in wire_ids:
                    issues.append(MigrationIssue(
                        "error", "ambiguous_wire_model_id",
                        f"wire ID {wire_model_id!r} maps to multiple offerings in provider {name!r}",
                        f"providers[{provider_index}].models[{model_index}]",
                    ))
                    continue
                wire_ids[wire_key] = deployment_id
                safe_enable = (
                    offering["enabled"]
                    and identity.confidence == "high"
                    and not offering["stale"]
                )
                document["deployments"].append({
                    "deployment_id": deployment_id,
                    "offering_id": offering_id,
                    "connection_id": connection_id,
                    "wire_model_id": wire_model_id,
                    "enabled": safe_enable,
                    "identity_confidence": identity.confidence,
                    # Existing enabled routes are migration evidence. Newly
                    # discovered deployments use the separate probe workflow.
                    "probe_status": "passed" if safe_enable else "unprobed",
                    "priority": int(seed.get("priority") or wire_index * 10 + 100),
                })

    # The domain requires every Offering to retain a diagnostic Deployment.
    # A duplicate-wire error may have consumed every candidate; synthesize a
    # disabled, non-routable namespaced ID so the staged document stays
    # inspectable while the error still blocks commit.
    deployed_offerings = {row["offering_id"] for row in document["deployments"]}
    connection_by_access = {
        row["provider_access_id"]: row["connection_id"]
        for row in document["connections"]
    }
    for offering in document["offerings"]:
        if offering["offering_id"] in deployed_offerings:
            continue
        fallback_wire = str(offering.get("pending_model_id") or offering["offering_id"])
        document["deployments"].append({
            "deployment_id": _stable_id("dep", offering["offering_id"], "blocked"),
            "offering_id": offering["offering_id"],
            "connection_id": connection_by_access[offering["provider_access_id"]],
            "wire_model_id": f"blocked/{fallback_wire}",
            "enabled": False,
            "identity_confidence": "pending",
            "probe_status": "unprobed",
            "priority": 1000000,
        })

    # Dedupe credential model grants after every offering has contributed.
    for credential in document["credentials"]:
        models = credential["authorization"]["models"]
        unique = {
            (row["creator_id"], row["model_id"]): row for row in models
        }
        credential["authorization"]["models"] = [
            unique[key] for key in sorted(unique)
        ]

    try:
        document = normalize_document(document)
    except ModelRoutingError as exc:
        issues.append(MigrationIssue(
            "error", exc.kind, str(exc), exc.field))
    return MigrationPlan(
        document=document,
        issues=issues,
        source_digest=source_digest,
        redacted_backup=redacted_backup,
        _secrets=pending_secrets,
    )


def validate_migration_plan(
    plan: MigrationPlan,
    *,
    existing_secret_references: Sequence[str] = (),
) -> dict[str, Any]:
    """Validate counts, references, secrets, and selectable candidates."""
    if plan.blocking_issues:
        raise ModelRoutingError(
            "migration plan has blocking issues",
            kind="model_routing_migration_blocked",
        )
    document = normalize_document(plan.document)
    planned_references = {secret.secret_reference for secret in plan._secrets}
    available = planned_references | {str(value) for value in existing_secret_references}
    missing = sorted({
        credential["secret_reference"]
        for credential in document["credentials"]
        if credential["secret_reference"]
        and credential["secret_reference"] not in available
    })
    if missing:
        raise ModelRoutingError(
            f"migration references missing encrypted secrets: {missing}",
            kind="model_routing_migration_secret_missing",
        )
    enabled_offerings = {
        offering["offering_id"] for offering in document["offerings"]
        if offering["enabled"] and not offering.get("stale")
    }
    candidates = [
        deployment for deployment in document["deployments"]
        if deployment["enabled"] and deployment["offering_id"] in enabled_offerings
    ]
    confirmed_offerings = sum(
        offering["identity_state"] == "confirmed"
        for offering in document["offerings"]
    )
    if confirmed_offerings and not candidates:
        raise ModelRoutingError(
            "migration would enable no candidate routes",
            kind="model_routing_migration_no_candidates",
        )
    return {
        "entity_counts": {
            name: len(document[name])
            for name in (
                "creators", "models", "providers", "provider_accesses",
                "connections", "credentials", "offerings", "deployments",
            )
        },
        "secret_references": len(available),
        "candidate_routes": len(candidates),
        "pending_identities": sum(
            offering["identity_state"] == "pending_identity"
            for offering in document["offerings"]
        ),
    }


def execute_migration(
    repository: RepositoryPort,
    boundary: OwnerBoundary,
    plan: MigrationPlan,
    *,
    now: Callable[[], float] = time.time,
) -> MigrationResult:
    """Write secrets, validate staging, then atomically switch authority."""
    receipt_id = _stable_id("migration", boundary.tenant_id, boundary.owner_user_id, plan.source_digest)
    base_receipt: dict[str, Any] = {
        "receipt_id": receipt_id,
        "source_digest": plan.source_digest,
        "started_at": now(),
        "redacted_backup": copy.deepcopy(plan.redacted_backup),
        "status": "pending",
    }

    def persist_failure_receipt(receipt: Mapping[str, Any]) -> None:
        try:
            repository.record_migration_receipt(boundary, receipt)
        except Exception as receipt_error:
            logger.warning(
                'Model-routing migration receipt persistence failed owner=%s: %s',
                boundary.owner_user_id,
                str(receipt_error)[:300],
            )

    if plan.blocking_issues:
        receipt = {
            **base_receipt,
            "status": "rejected",
            "issues": [issue.public_dict() for issue in plan.blocking_issues],
            "finished_at": now(),
        }
        persist_failure_receipt(receipt)
        return MigrationResult(False, None, receipt)

    try:
        existing_metadata = getattr(repository, "secret_metadata", lambda _boundary: [])(boundary)
        existing_refs = [row["secret_reference"] for row in existing_metadata]
        written_refs: list[str] = []
        for pending in plan._secrets:
            repository.put_secret(
                boundary,
                pending.plaintext,
                secret_reference=pending.secret_reference,
            )
            written_refs.append(pending.secret_reference)
        validation = validate_migration_plan(
            plan, existing_secret_references=[*existing_refs, *written_refs])
        current = repository.get(boundary)
        staged = copy.deepcopy(plan.document)
        staged["migration"] = {
            "source": "legacy_provider_configuration",
            "migrated_at": now(),
            "receipt_id": receipt_id,
        }
        receipt = {
            **base_receipt,
            "status": "committed",
            "validation": validation,
            "from_revision": current.revision,
            "to_revision": current.revision + 1,
            "finished_at": now(),
        }
        authority = repository.compare_and_swap(
            boundary,
            staged,
            expected_revision=current.revision,
            migration_receipt=receipt,
        )
        return MigrationResult(True, authority, receipt)
    except Exception as exc:
        # The caller receives a bounded recovery receipt, while operators still
        # need a durable trace that distinguishes validation/CAS/storage
        # failures.  Never include the staged document or secret plaintext.
        logger.warning(
            'Model-routing migration failed owner=%s tenant=%s error=%s: %s',
            boundary.owner_user_id,
            boundary.tenant_id,
            type(exc).__name__,
            str(exc)[:300],
        )
        receipt = {
            **base_receipt,
            "status": "failed",
            "error_kind": str(getattr(exc, "kind", "model_routing_migration_failed")),
            "error": str(exc)[:500],
            "finished_at": now(),
        }
        persist_failure_receipt(receipt)
        return MigrationResult(False, None, receipt)


__all__ = [
    "MigrationIssue",
    "MigrationPlan",
    "MigrationResult",
    "execute_migration",
    "plan_legacy_migration",
    "validate_migration_plan",
]
