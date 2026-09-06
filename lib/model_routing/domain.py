"""Canonical `tofu.model-routing/v2` entities and cross-reference validation.

The aggregate is deliberately JSON-native so SQLite and PostgreSQL adapters
share one semantic repository contract.  Runtime routing consumes only the
normalized aggregate returned here; legacy catalog/provider documents are
accepted exclusively by :mod:`lib.model_routing.migration`.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

from lib.model_catalog._creator_identity import (
    merge_keys,
    strip_region_display_tag,
    strip_routing_decoration,
)
from lib.model_info import release_date as _release_date
from lib.provider_headers import sanitise_extra_headers


CONTRACT_VERSION = "tofu.model-routing/v2"
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024
MAX_ROUTE_SNAPSHOT_BYTES = 16 * 1024
MAX_COUNTS = {
    "creators": 1024,
    "models": 4096,
    "providers": 256,
    "provider_accesses": 256,
    "connections": 512,
    "credentials": 1024,
    "offerings": 4096,
    "deployments": 8192,
}
_FORBIDDEN_LEGACY_FIELDS = frozenset({"aliases", "request_ids", "routes"})
_FORBIDDEN_SECRET_FIELDS = frozenset({
    "api_key", "api_keys", "secret", "ciphertext", "password", "token",
})


class ModelRoutingError(ValueError):
    """Typed contract or selection failure safe for an API error envelope."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "model_routing_invalid",
        field: str = "",
        candidates: Sequence[Mapping[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.field = field
        self.candidates = [dict(item) for item in (candidates or ())]


@dataclass(frozen=True, slots=True, order=True)
class ModelRef:
    creator_id: str
    model_id: str

    @classmethod
    def from_value(cls, value: Mapping[str, Any], *, field: str = "model") -> "ModelRef":
        if not isinstance(value, Mapping):
            raise ModelRoutingError(f"{field} must be an object", field=field)
        return cls(
            _identifier(value.get("creator_id"), f"{field}.creator_id"),
            _identifier(value.get("model_id"), f"{field}.model_id"),
        )

    def public_dict(self) -> dict[str, str]:
        return {"creator_id": self.creator_id, "model_id": self.model_id}


@dataclass(frozen=True, slots=True)
class ProviderOfferingRef:
    provider_id: str
    offering_id: str

    def public_dict(self) -> dict[str, str]:
        return {"provider_id": self.provider_id, "offering_id": self.offering_id}


@dataclass(frozen=True, slots=True)
class NativeModelSelection:
    model: ModelRef | None
    provider_offering: ProviderOfferingRef | None
    preferred_provider_id: str = ""


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ModelRoutingError(f"{field} must be a string", field=field)
    result = value.strip()
    if not result or len(result) > 256 or any(ord(ch) < 32 for ch in result):
        raise ModelRoutingError(
            f"{field} must be 1..256 printable characters", field=field)
    return result


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelRoutingError(f"{field} must be a positive integer", field=field)
    return value


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelRoutingError(
            f"{field} must be a non-negative integer", field=field)
    return value


def _rows(document: Mapping[str, Any], name: str) -> list[dict[str, Any]]:
    value = document.get(name)
    if not isinstance(value, list):
        raise ModelRoutingError(f"{name} must be an array", field=name)
    maximum = MAX_COUNTS[name]
    if len(value) > maximum:
        raise ModelRoutingError(
            f"{name} exceeds the {maximum} item resource budget", field=name)
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ModelRoutingError(
                f"{name}[{index}] must be an object", field=f"{name}[{index}]")
        rows.append(copy.deepcopy(dict(item)))
    return rows


def _unique_index(
    rows: Sequence[dict[str, Any]], field: str, collection: str,
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        value = _identifier(row.get(field), f"{collection}[{index}].{field}")
        if value in result:
            raise ModelRoutingError(
                f"duplicate {field}: {value}", field=f"{collection}.{field}")
        row[field] = value
        result[value] = row
    return result


def _string_set(value: Any, field: str, *, maximum: int = 512) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ModelRoutingError(
            f"{field} must be an array with at most {maximum} items", field=field)
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        normalized = _identifier(item, f"{field}[{index}]")
        if normalized in seen:
            raise ModelRoutingError(f"{field} contains duplicate {normalized!r}", field=field)
        seen.add(normalized)
        result.append(normalized)
    return sorted(result)


def _scan_for_forbidden_fields(value: Any, path: str = "document") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _FORBIDDEN_LEGACY_FIELDS:
                raise ModelRoutingError(
                    f"legacy field {key!r} is not part of {CONTRACT_VERSION}",
                    kind="legacy_model_routing_state_removed",
                    field=f"{path}.{key}",
                )
            if key in _FORBIDDEN_SECRET_FIELDS:
                raise ModelRoutingError(
                    f"secret material is forbidden at {path}.{key}; store a secret_reference",
                    kind="secret_material_forbidden",
                    field=f"{path}.{key}",
                )
            _scan_for_forbidden_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _scan_for_forbidden_fields(child, f"{path}[{index}]")


def empty_document(*, revision: int = 0) -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "revision": revision,
        **{name: [] for name in MAX_COUNTS},
    }


def parse_native_model_selection(payload: Mapping[str, Any]) -> NativeModelSelection:
    """Parse the only two native v2 model reference forms.

    Official models use ``{creator_id, model_id}``; provider-scoped pending
    identities use ``{provider_id, offering_id}``.  A routing preference is
    orthogonal and never turns a provider into part of model identity.
    """
    if not isinstance(payload, Mapping):
        raise ModelRoutingError("request body must be an object")
    if "provider" in payload:
        raise ModelRoutingError(
            "inline provider blocks were removed; configure a ProviderAccess and use routing",
            kind="legacy_inline_provider_removed",
            field="provider",
        )
    raw = payload.get("model")
    if isinstance(raw, str):
        if "@" in raw:
            raise ModelRoutingError(
                "model@provider selectors were removed; use a structured model reference",
                kind="legacy_model_selector_removed",
                field="model",
            )
        raise ModelRoutingError(
            "native model must be an object", kind="structured_model_ref_required", field="model")
    if not isinstance(raw, Mapping):
        raise ModelRoutingError("model must be an object", field="model")
    keys = set(raw)
    official_keys = {"creator_id", "model_id"}
    provider_keys = {"provider_id", "offering_id"}
    if keys == official_keys:
        model = ModelRef.from_value(raw)
        provider_offering = None
    elif keys == provider_keys:
        model = None
        provider_offering = ProviderOfferingRef(
            _identifier(raw.get("provider_id"), "model.provider_id"),
            _identifier(raw.get("offering_id"), "model.offering_id"),
        )
    else:
        raise ModelRoutingError(
            "model must contain exactly creator_id+model_id or provider_id+offering_id",
            field="model",
        )
    routing = payload.get("routing") or {}
    if not isinstance(routing, Mapping):
        raise ModelRoutingError("routing must be an object", field="routing")
    unknown = set(routing) - {
        "preferred_provider_id",
        "required_context",
        "price_budget",
        "cache_affinity_connection_id",
    }
    if unknown:
        raise ModelRoutingError(
            f"unknown routing fields: {', '.join(sorted(unknown))}", field="routing")
    preferred = ""
    if routing.get("preferred_provider_id") is not None:
        preferred = _identifier(
            routing.get("preferred_provider_id"), "routing.preferred_provider_id")
    if provider_offering is not None and preferred and preferred != provider_offering.provider_id:
        raise ModelRoutingError(
            "a provider-scoped offering cannot prefer a different provider",
            kind="provider_scope_violation",
            field="routing.preferred_provider_id",
        )
    return NativeModelSelection(model, provider_offering, preferred)


# A pure date token makes a spelling a snapshot of the trained model, never
# its canonical name: YYYYMMDD/YYMMDD count double, YYYY or 0MMD count once.
_ALIAS_DATE_TOKEN = re.compile(
    r'(?:^|[^a-z0-9])(20\d{6}|\d{6}|20\d{2}|0\d{3})(?:[^a-z0-9]|$)')
_ALIAS_QUANT_SUFFIX = re.compile(
    r'[-.](?:fp8|fp16|fp32|bf16|int4|int8|awq|gptq)$', re.IGNORECASE)


def _alias_survivor_rank(row: Mapping[str, Any]) -> tuple[Any, ...]:
    """Least-decorated spelling wins: relay decoration, quant and snapshot
    date markers all make an id a re-publication, and the more specific id
    (the longer one) is otherwise the creator's own."""
    model_id = str(row["model_id"])
    lowered = model_id.lower()
    penalty = 0
    if strip_routing_decoration(lowered) != lowered:
        penalty += 4
    if _ALIAS_QUANT_SUFFIX.search(lowered):
        penalty += 2
    for match in _ALIAS_DATE_TOKEN.finditer(lowered):
        penalty += 2 if len(match.group(1)) >= 6 else 1
    return (penalty, -len(model_id), lowered)


def _merge_trained_model_alias_rows(
    model_by_ref: dict[ModelRef, dict[str, Any]],
) -> dict[ModelRef, ModelRef]:
    """Collapse provider respellings of one trained model into a survivor row.

    Identity in the model collection is the trained model: relay SKUs
    (``cerebras-llama-4-…``), dated snapshots, quant suffixes and publisher
    namespaces are provider flower names for the same weights.  Returns the
    alias map from absorbed refs to their survivor; capabilities are unioned
    and context keeps the maximum so no offering validated against an
    absorbed row can become invalid through the merge.
    """
    parent: dict[ModelRef, ModelRef] = {ref: ref for ref in model_by_ref}

    def find(ref: ModelRef) -> ModelRef:
        while parent[ref] != ref:
            parent[ref] = parent[parent[ref]]
            ref = parent[ref]
        return ref

    def union(first: ModelRef, second: ModelRef) -> None:
        first_root, second_root = find(first), find(second)
        if first_root != second_root:
            parent[max(first_root, second_root)] = min(first_root, second_root)

    key_owner: dict[tuple[str, str], ModelRef] = {}
    for ref, row in model_by_ref.items():
        for key in merge_keys(ref.model_id, row.get("display_name")):
            index = (ref.creator_id, key)
            owner = key_owner.get(index)
            if owner is None:
                key_owner[index] = ref
            else:
                union(ref, owner)

    groups: dict[ModelRef, list[ModelRef]] = {}
    for ref in parent:
        groups.setdefault(find(ref), []).append(ref)

    alias: dict[ModelRef, ModelRef] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        members.sort(key=lambda ref: _alias_survivor_rank(model_by_ref[ref]))
        survivor = members[0]
        survivor_row = model_by_ref[survivor]
        member_rows = [model_by_ref[ref] for ref in members]
        survivor_row["capabilities"] = sorted({
            capability
            for row in member_rows
            for capability in row["capabilities"]
        })
        survivor_row["context_window"] = max(
            row["context_window"] for row in member_rows)
        survivor_row["quality_rank"] = max(
            row["quality_rank"] for row in member_rows)
        if survivor_row["display_name"] == survivor_row["model_id"]:
            for row in member_rows[1:]:
                display = strip_region_display_tag(row["display_name"])
                if display and display != row["model_id"]:
                    survivor_row["display_name"] = display
                    break
        for row in member_rows[1:]:
            for field, value in row.items():
                survivor_row.setdefault(field, value)
        for ref in members[1:]:
            alias[ref] = survivor
    return alias


def normalize_document(
    raw: Mapping[str, Any], *, revision: int | None = None,
) -> dict[str, Any]:
    """Return a canonical, deeply copied v2 aggregate or fail closed."""
    if not isinstance(raw, Mapping):
        raise ModelRoutingError("model routing document must be an object")
    allowed = {"contract_version", "revision", "migration", *MAX_COUNTS}
    unknown = set(raw) - allowed
    if unknown:
        raise ModelRoutingError(
            f"unknown model routing fields: {', '.join(sorted(unknown))}")
    if raw.get("contract_version") != CONTRACT_VERSION:
        raise ModelRoutingError(
            f"contract_version must be {CONTRACT_VERSION!r}", field="contract_version")
    normalized_revision = raw.get("revision") if revision is None else revision
    normalized_revision = _non_negative_int(normalized_revision, "revision")
    _scan_for_forbidden_fields(raw)

    creators = _rows(raw, "creators")
    models = _rows(raw, "models")
    providers = _rows(raw, "providers")
    accesses = _rows(raw, "provider_accesses")
    connections = _rows(raw, "connections")
    credentials = _rows(raw, "credentials")
    offerings = _rows(raw, "offerings")
    deployments = _rows(raw, "deployments")

    creator_by_id = _unique_index(creators, "creator_id", "creators")
    for index, creator in enumerate(creators):
        creator["name"] = _identifier(creator.get("name"), f"creators[{index}].name")

    model_by_ref: dict[ModelRef, dict[str, Any]] = {}
    for index, model in enumerate(models):
        ref = ModelRef.from_value(model, field=f"models[{index}]")
        if ref.creator_id not in creator_by_id:
            raise ModelRoutingError(
                f"model references unknown creator {ref.creator_id}", field=f"models[{index}].creator_id")
        if ref in model_by_ref:
            raise ModelRoutingError(
                f"duplicate model identity: {ref.creator_id}/{ref.model_id}", field="models")
        model["creator_id"], model["model_id"] = ref.creator_id, ref.model_id
        model["display_name"] = _identifier(
            model.get("display_name"), f"models[{index}].display_name")
        model["capabilities"] = _string_set(
            model.get("capabilities"), f"models[{index}].capabilities", maximum=64)
        model["context_window"] = _positive_int(
            model.get("context_window"), f"models[{index}].context_window")
        quality = model.get("quality_rank")
        if isinstance(quality, bool) or not isinstance(quality, (int, float)):
            raise ModelRoutingError(
                f"models[{index}].quality_rank must be numeric",
                field=f"models[{index}].quality_rank",
            )
        model["quality_rank"] = float(quality)
        # release_date is derived vendor knowledge projected at read time by
        # public_projection; never owner state — drop any client-echoed copy
        # so a save round-trip cannot persist it into the aggregate.
        model.pop("release_date", None)
        model_by_ref[ref] = model

    alias_map = _merge_trained_model_alias_rows(model_by_ref)
    if alias_map:
        model_by_ref = {
            ref: row for ref, row in model_by_ref.items() if ref not in alias_map}

    provider_by_id = _unique_index(providers, "provider_id", "providers")
    for index, provider in enumerate(providers):
        provider["name"] = _identifier(provider.get("name"), f"providers[{index}].name")
        if provider.get("scope") not in {"public", "owner"}:
            raise ModelRoutingError(
                f"providers[{index}].scope must be public or owner",
                field=f"providers[{index}].scope",
            )

    access_by_id = _unique_index(accesses, "provider_access_id", "provider_accesses")
    access_by_provider: dict[str, dict[str, Any]] = {}
    for index, access in enumerate(accesses):
        provider_id = _identifier(
            access.get("provider_id"), f"provider_accesses[{index}].provider_id")
        if provider_id not in provider_by_id:
            raise ModelRoutingError(
                f"provider access references unknown provider {provider_id}",
                field=f"provider_accesses[{index}].provider_id",
            )
        if provider_id in access_by_provider:
            raise ModelRoutingError(
                f"an owner may have only one ProviderAccess for {provider_id}",
                kind="duplicate_provider_access",
                field="provider_accesses",
            )
        access["provider_id"] = provider_id
        access["enabled"] = bool(access.get("enabled"))
        if not isinstance(access.get("quota_policy"), Mapping):
            raise ModelRoutingError(
                f"provider_accesses[{index}].quota_policy must be an object",
                field=f"provider_accesses[{index}].quota_policy",
            )
        access_by_provider[provider_id] = access

    connection_by_id = _unique_index(connections, "connection_id", "connections")
    for index, connection in enumerate(connections):
        access_id = _identifier(
            connection.get("provider_access_id"), f"connections[{index}].provider_access_id")
        if access_id not in access_by_id:
            raise ModelRoutingError(
                f"connection references unknown provider access {access_id}",
                field=f"connections[{index}].provider_access_id",
            )
        connection["provider_access_id"] = access_id
        base_url = connection.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip() or len(base_url) > 2048:
            raise ModelRoutingError(
                f"connections[{index}].base_url is invalid", field=f"connections[{index}].base_url")
        connection["base_url"] = base_url.strip().rstrip("/")
        connection["priority"] = _non_negative_int(
            connection.get("priority", 100), f"connections[{index}].priority")
        connection["enabled"] = bool(connection.get("enabled"))
        normalized_headers, header_error = sanitise_extra_headers(
            connection.get("extra_headers"))
        if header_error:
            raise ModelRoutingError(
                header_error,
                field=f"connections[{index}].extra_headers",
            )
        connection["extra_headers"] = normalized_headers
        adapter = connection.get("adapter")
        if adapter is not None:
            if not isinstance(adapter, Mapping) or set(adapter) != {
                "agent_id", "port",
            }:
                raise ModelRoutingError(
                    f"connections[{index}].adapter must contain only "
                    "agent_id and port",
                    field=f"connections[{index}].adapter",
                )
            agent_id = _identifier(
                adapter.get("agent_id"),
                f"connections[{index}].adapter.agent_id",
            )
            port = adapter.get("port")
            if (
                isinstance(port, bool)
                or not isinstance(port, int)
                or port < 1
                or port > 65_535
            ):
                raise ModelRoutingError(
                    f"connections[{index}].adapter.port is invalid",
                    field=f"connections[{index}].adapter.port",
                )
            try:
                parsed = urlparse(connection["base_url"])
                base_port = parsed.port
            except ValueError as exc:
                raise ModelRoutingError(
                    f"connections[{index}].base_url has an invalid port",
                    field=f"connections[{index}].base_url",
                ) from exc
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or base_port != port
                or connection.get("protocol") != "openai"
            ):
                raise ModelRoutingError(
                    "desktop adapter connections require an OpenAI loopback "
                    "URL whose port matches adapter.port",
                    kind="desktop_adapter_connection_invalid",
                    field=f"connections[{index}]",
                )
            connection["adapter"] = {"agent_id": agent_id, "port": port}

    # Building the index is also the single duplicate-ID validator; keep that
    # validation even though this pass only needs the credential rows below.
    _unique_index(credentials, "credential_id", "credentials")
    for index, credential in enumerate(credentials):
        access_id = _identifier(
            credential.get("provider_access_id"), f"credentials[{index}].provider_access_id")
        if access_id not in access_by_id:
            raise ModelRoutingError(
                f"credential references unknown provider access {access_id}",
                field=f"credentials[{index}].provider_access_id",
            )
        credential["provider_access_id"] = access_id
        if credential.get("kind") not in {
            "api_key", "oauth", "local_identity", "subscription",
        }:
            raise ModelRoutingError(
                f"credentials[{index}].kind is invalid",
                field=f"credentials[{index}].kind",
            )
        credential["enabled"] = bool(credential.get("enabled"))
        secret_reference = credential.get("secret_reference", "")
        if not isinstance(secret_reference, str) or len(secret_reference) > 256:
            raise ModelRoutingError(
                f"credentials[{index}].secret_reference is invalid",
                field=f"credentials[{index}].secret_reference",
            )
        if credential.get("kind") != "local_identity" and not secret_reference:
            raise ModelRoutingError(
                f"credentials[{index}] requires a secret_reference",
                field=f"credentials[{index}].secret_reference",
            )
        authorization = credential.get("authorization")
        if not isinstance(authorization, Mapping):
            raise ModelRoutingError(
                f"credentials[{index}].authorization must be an object",
                field=f"credentials[{index}].authorization",
            )
        connection_ids = _string_set(
            authorization.get("connection_ids"),
            f"credentials[{index}].authorization.connection_ids",
        )
        for connection_id in connection_ids:
            connection = connection_by_id.get(connection_id)
            if connection is None or connection["provider_access_id"] != access_id:
                raise ModelRoutingError(
                    f"credential authorization crosses provider access at {connection_id}",
                    kind="credential_authorization_scope_violation",
                    field=f"credentials[{index}].authorization.connection_ids",
                )
        authorized_models: list[dict[str, str]] = []
        raw_models = authorization.get("models")
        if not isinstance(raw_models, list) or len(raw_models) > MAX_COUNTS["models"]:
            raise ModelRoutingError(
                f"credentials[{index}].authorization.models must be a bounded array",
                field=f"credentials[{index}].authorization.models",
            )
        seen_sources: set[ModelRef] = set()
        seen_models: set[ModelRef] = set()
        for model_index, raw_ref in enumerate(raw_models):
            source_ref = ModelRef.from_value(
                raw_ref, field=f"credentials[{index}].authorization.models[{model_index}]")
            if source_ref in seen_sources:
                raise ModelRoutingError(
                    "credential authorization contains a duplicate model",
                    field=f"credentials[{index}].authorization.models",
                )
            seen_sources.add(source_ref)
            ref = alias_map.get(source_ref, source_ref)
            if ref not in model_by_ref:
                raise ModelRoutingError(
                    f"credential authorization references unknown model {source_ref}",
                    field=f"credentials[{index}].authorization.models[{model_index}]",
                )
            if ref in seen_models:
                # Provider respellings collapsed into one trained model.
                continue
            seen_models.add(ref)
            authorized_models.append(ref.public_dict())
        credential["authorization"] = {
            "connection_ids": connection_ids,
            "models": sorted(authorized_models, key=lambda item: (item["creator_id"], item["model_id"])),
        }
        if not isinstance(credential.get("quota_policy"), Mapping):
            raise ModelRoutingError(
                f"credentials[{index}].quota_policy must be an object",
                field=f"credentials[{index}].quota_policy",
            )

    offering_by_id = _unique_index(offerings, "offering_id", "offerings")
    for index, offering in enumerate(offerings):
        access_id = _identifier(
            offering.get("provider_access_id"), f"offerings[{index}].provider_access_id")
        if access_id not in access_by_id:
            raise ModelRoutingError(
                f"offering references unknown provider access {access_id}",
                field=f"offerings[{index}].provider_access_id",
            )
        offering["provider_access_id"] = access_id
        state = offering.get("identity_state")
        if state not in {"confirmed", "pending_identity"}:
            raise ModelRoutingError(
                f"offerings[{index}].identity_state is invalid",
                field=f"offerings[{index}].identity_state",
            )
        capabilities = _string_set(
            offering.get("capabilities"), f"offerings[{index}].capabilities", maximum=64)
        context_window = _positive_int(
            offering.get("context_window"), f"offerings[{index}].context_window")
        if state == "confirmed":
            if "pending_model_id" in offering:
                raise ModelRoutingError(
                    "confirmed offering cannot carry pending_model_id",
                    field=f"offerings[{index}].pending_model_id",
                )
            source_ref = ModelRef.from_value(
                offering.get("model"), field=f"offerings[{index}].model")
            ref = alias_map.get(source_ref, source_ref)
            official = model_by_ref.get(ref)
            if official is None:
                raise ModelRoutingError(
                    f"offering references unknown model {source_ref.creator_id}/{source_ref.model_id}",
                    field=f"offerings[{index}].model",
                )
            unsupported = set(capabilities) - set(official["capabilities"])
            if unsupported:
                raise ModelRoutingError(
                    f"offering claims capabilities absent from the official model: {sorted(unsupported)}",
                    kind="offering_capability_expansion",
                    field=f"offerings[{index}].capabilities",
                )
            if context_window > official["context_window"]:
                raise ModelRoutingError(
                    "offering context_window exceeds the official model limit",
                    kind="offering_context_expansion",
                    field=f"offerings[{index}].context_window",
                )
            offering["model"] = ref.public_dict()
        else:
            if "model" in offering:
                raise ModelRoutingError(
                    "pending_identity offering cannot carry an official model reference",
                    field=f"offerings[{index}].model",
                )
            offering["pending_model_id"] = _identifier(
                offering.get("pending_model_id"), f"offerings[{index}].pending_model_id")
        offering["capabilities"] = capabilities
        offering["context_window"] = context_window
        offering["enabled"] = bool(offering.get("enabled"))
        offering["stale"] = bool(offering.get("stale", False))
        offering["priority"] = _non_negative_int(
            offering.get("priority", 100), f"offerings[{index}].priority")

    _unique_index(deployments, "deployment_id", "deployments")
    wire_ids_by_access: dict[tuple[str, str], str] = {}
    deployment_counts: dict[str, int] = {offering_id: 0 for offering_id in offering_by_id}
    for index, deployment in enumerate(deployments):
        offering_id = _identifier(
            deployment.get("offering_id"), f"deployments[{index}].offering_id")
        connection_id = _identifier(
            deployment.get("connection_id"), f"deployments[{index}].connection_id")
        offering = offering_by_id.get(offering_id)
        connection = connection_by_id.get(connection_id)
        if offering is None or connection is None:
            raise ModelRoutingError(
                "deployment references unknown offering or connection",
                field=f"deployments[{index}]",
            )
        if offering["provider_access_id"] != connection["provider_access_id"]:
            raise ModelRoutingError(
                "deployment connection and offering belong to different ProviderAccess resources",
                kind="deployment_scope_violation",
                field=f"deployments[{index}]",
            )
        wire_model_id = _identifier(
            deployment.get("wire_model_id"), f"deployments[{index}].wire_model_id")
        wire_key = (offering["provider_access_id"], wire_model_id)
        if wire_key in wire_ids_by_access:
            raise ModelRoutingError(
                f"wire_model_id {wire_model_id!r} maps to more than one Deployment in a provider",
                kind="duplicate_wire_model_id",
                field=f"deployments[{index}].wire_model_id",
            )
        wire_ids_by_access[wire_key] = str(deployment["deployment_id"])
        deployment["offering_id"] = offering_id
        deployment["connection_id"] = connection_id
        deployment["wire_model_id"] = wire_model_id
        if deployment.get("max_output_tokens") is not None:
            deployment["max_output_tokens"] = _positive_int(
                deployment.get("max_output_tokens"),
                f"deployments[{index}].max_output_tokens",
            )
            if deployment["max_output_tokens"] > 1_000_000:
                raise ModelRoutingError(
                    "deployment max_output_tokens exceeds the hard ceiling",
                    field=f"deployments[{index}].max_output_tokens",
                )
        deployment["priority"] = _non_negative_int(
            deployment.get("priority", 100), f"deployments[{index}].priority")
        deployment["enabled"] = bool(deployment.get("enabled"))
        confidence = deployment.get("identity_confidence")
        probe_status = deployment.get("probe_status")
        if confidence not in {"high", "medium", "low", "pending"}:
            raise ModelRoutingError(
                f"deployments[{index}].identity_confidence is invalid",
                field=f"deployments[{index}].identity_confidence",
            )
        if probe_status not in {"passed", "failed", "unprobed", "stale"}:
            raise ModelRoutingError(
                f"deployments[{index}].probe_status is invalid",
                field=f"deployments[{index}].probe_status",
            )
        if deployment["enabled"] and probe_status != "passed":
            raise ModelRoutingError(
                "an enabled Deployment requires a passed probe",
                kind="unsafe_deployment_enable",
                field=f"deployments[{index}].enabled",
            )
        if (
            deployment["enabled"]
            and offering["identity_state"] == "confirmed"
            and confidence != "high"
        ):
            raise ModelRoutingError(
                "an enabled confirmed Deployment requires high identity confidence",
                kind="unsafe_deployment_enable",
                field=f"deployments[{index}].enabled",
            )
        deployment_counts[offering_id] += 1

    for offering_id, count in deployment_counts.items():
        if count == 0:
            raise ModelRoutingError(
                f"offering {offering_id} has no Deployment", field="deployments")

    result: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "revision": normalized_revision,
        "creators": sorted(creators, key=lambda row: row["creator_id"]),
        "models": sorted(
            model_by_ref.values(),
            key=lambda row: (row["creator_id"], row["model_id"])),
        "providers": sorted(providers, key=lambda row: row["provider_id"]),
        "provider_accesses": sorted(accesses, key=lambda row: row["provider_access_id"]),
        "connections": sorted(connections, key=lambda row: row["connection_id"]),
        "credentials": sorted(credentials, key=lambda row: row["credential_id"]),
        "offerings": sorted(offerings, key=lambda row: row["offering_id"]),
        "deployments": sorted(deployments, key=lambda row: row["deployment_id"]),
    }
    if "migration" in raw:
        if not isinstance(raw["migration"], Mapping):
            raise ModelRoutingError("migration must be an object", field="migration")
        result["migration"] = copy.deepcopy(dict(raw["migration"]))
    return result


def public_projection(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return the validated credential-metadata document.

    A secret reference is an opaque, owner-scoped metadata identifier rather
    than secret material.  Keeping it in the public aggregate is required for
    lossless revision-CAS edits; the encrypted value remains accessible only
    through the repository's independent secret operation.

    Model rows additionally carry the derived ``release_date`` fact from
    :func:`lib.model_info.release_date` when the vendor date is known.  It is
    stamped here, on the read boundary only, and stripped again by
    :func:`normalize_document`, so it never becomes persisted owner state.
    """
    projected = normalize_document(document)
    for model in projected["models"]:
        date = _release_date(model["model_id"])
        if date:
            model["release_date"] = date
    return projected


__all__ = [
    "CONTRACT_VERSION",
    "MAX_COUNTS",
    "MAX_DOCUMENT_BYTES",
    "MAX_ROUTE_SNAPSHOT_BYTES",
    "ModelRef",
    "ModelRoutingError",
    "NativeModelSelection",
    "ProviderOfferingRef",
    "empty_document",
    "normalize_document",
    "parse_native_model_selection",
    "public_projection",
]
