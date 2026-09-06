"""Model-routing v2 provider-access HTTP adapter.

`Provider` names a service; the authenticated owner has at most one
`ProviderAccess` aggregate for it. Routes decode/authorize and delegate to the
owner-aware repository; no BYO or model-catalog state is maintained here.
"""

from __future__ import annotations

import copy
import time as _time
from collections.abc import Mapping
from typing import Any

from quart import Blueprint

from lib.api_response import (
    api_bad_request,
    api_conflict,
    api_created,
    api_internal_error,
    api_not_found,
    api_ok,
    api_service_unavailable,
)
from lib.log import audit_log, get_logger
from lib.model_routing import (
    build_discovered_provider_bundle,
    discovered_provider_id,
    ModelRoutingError,
    ModelRoutingRepository,
    OwnerBoundary,
    execute_migration,
    normalize_document,
    plan_legacy_migration,
)
from lib.openapi import api_meta
from lib.provider_headers import sanitise_extra_headers as sanitise_extra_headers
from lib.provider_template_recipes import (
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
    load_provider_templates,
)
from lib.request_parser import parse_body
from lib.api_v1_model_routing_generated import (
    ContractViolation,
    decode_request,
)

from .auth import current_auth, require_scope

# Access-matrix probe engine (Settings → 服务商 → 访问矩阵). Plaintext keys
# are resolved server-side from the owner-scoped secret store; the browser
# only ever addresses credentials by their column index.
from lib.json_store import read_json, write_json_atomic
from lib.model_routing.dispatch_adapter import decode_credential_secret
from lib.provider_probe import (
    CELL_PROBE_LOCK,
    CELL_PROBE_TASKS,
    FairWorkLaneQueueFull,
    build_probe_work,
    normalize_probe_snapshot,
    probe_cache_path,
    probe_cell_key,
    public_probe_snapshot,
    run_cell_probe_task,
    submit_provider_probe_task,
)
from lib.provider_probe_policy import (
    PROVIDER_PROBE_MAX_ATTEMPTS,
    PROVIDER_PROBE_MAX_CELLS,
    PROVIDER_PROBE_MAX_TIMEOUT_SECONDS,
    PROVIDER_PROBE_MIN_TIMEOUT_SECONDS,
)


api_v1_providers_bp = Blueprint("api_v1_providers", __name__)
logger = get_logger(__name__)


def _boundary() -> OwnerBoundary | None:
    context = current_auth()
    if context is None or context.owner_user_id is None:
        return None
    return OwnerBoundary.create(context.owner_user_id, context.tenant_id)


def _repository() -> ModelRoutingRepository:
    return ModelRoutingRepository()


def _expected_revision(body: Mapping[str, Any]) -> int:
    value = body.get("expected_revision")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelRoutingError(
            "expected_revision must be a non-negative integer",
            field="expected_revision",
        )
    return value


def _error(exc: Exception):
    kind = str(getattr(exc, "kind", "") or getattr(exc, "code", ""))
    extras: dict[str, Any] = {"kind": kind or "model_routing_invalid"}
    field = str(getattr(exc, "field", "") or "")
    if field:
        extras["field"] = field
    candidates = getattr(exc, "candidates", None)
    if candidates:
        extras["candidates"] = candidates
    if "conflict" in kind.lower():
        return api_conflict(str(exc), **extras)
    return api_bad_request(str(exc), **extras)


def _contract_rejection(operation_id: str, body: Any):
    """Fail-closed request validation against the generated API v1 contract."""
    try:
        decode_request(operation_id, body)
    except ContractViolation as exc:
        first = exc.violations[0] if exc.violations else ""
        # Violations render as "$.<dotted path>: <reason>".
        field = ""
        if first.startswith("$.") and ":" in first:
            field = first[2:].split(":", 1)[0].split(".", 1)[0]
        extras = {"field": field} if field else {}
        return api_bad_request(str(exc), **extras)
    return None


def _provider_bundle(document: Mapping[str, Any], provider_id: str) -> dict[str, Any] | None:
    provider = next(
        (row for row in document["providers"] if row["provider_id"] == provider_id),
        None,
    )
    if provider is None:
        return None
    accesses = [
        row for row in document["provider_accesses"]
        if row["provider_id"] == provider_id
    ]
    access_ids = {row["provider_access_id"] for row in accesses}
    connections = [
        row for row in document["connections"]
        if row["provider_access_id"] in access_ids
    ]
    connection_ids = {row["connection_id"] for row in connections}
    credentials = [
        row for row in document["credentials"]
        if row["provider_access_id"] in access_ids
    ]
    offerings = [
        row for row in document["offerings"]
        if row["provider_access_id"] in access_ids
    ]
    offering_ids = {row["offering_id"] for row in offerings}
    deployments = [
        row for row in document["deployments"]
        if row["offering_id"] in offering_ids
        and row["connection_id"] in connection_ids
    ]
    return {
        "provider": copy.deepcopy(provider),
        "provider_access": copy.deepcopy(accesses[0]) if accesses else None,
        "connections": copy.deepcopy(connections),
        "credentials": copy.deepcopy(credentials),
        "offerings": copy.deepcopy(offerings),
        "deployments": copy.deepcopy(deployments),
    }


def _remove_provider(document: dict[str, Any], provider_id: str) -> list[str]:
    access_ids = {
        row["provider_access_id"] for row in document["provider_accesses"]
        if row["provider_id"] == provider_id
    }
    connection_ids = {
        row["connection_id"] for row in document["connections"]
        if row["provider_access_id"] in access_ids
    }
    offering_ids = {
        row["offering_id"] for row in document["offerings"]
        if row["provider_access_id"] in access_ids
    }
    secret_references = [
        row["secret_reference"] for row in document["credentials"]
        if row["provider_access_id"] in access_ids and row["secret_reference"]
    ]
    document["providers"] = [
        row for row in document["providers"] if row["provider_id"] != provider_id]
    document["provider_accesses"] = [
        row for row in document["provider_accesses"]
        if row["provider_id"] != provider_id]
    document["connections"] = [
        row for row in document["connections"]
        if row["connection_id"] not in connection_ids]
    document["credentials"] = [
        row for row in document["credentials"]
        if row["provider_access_id"] not in access_ids]
    document["offerings"] = [
        row for row in document["offerings"]
        if row["offering_id"] not in offering_ids]
    document["deployments"] = [
        row for row in document["deployments"]
        if row["offering_id"] not in offering_ids
        and row["connection_id"] not in connection_ids]
    return secret_references


def _reclaim_secret_references(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    references: list[str],
) -> None:
    """Best-effort cleanup after a failed write or superseded credential.

    Aggregate CAS is authoritative. Once it commits, cleanup failure must not
    tell a caller the mutation failed and invite a duplicate retry; the
    repository janitor can reclaim the bounded orphan later.
    """
    for reference in references:
        if not reference:
            continue
        try:
            repository.delete_secret(boundary, reference)
        except Exception as exc:
            logger.error(
                "model-routing secret cleanup failed owner=%s ref=%s: %s",
                boundary.owner_user_id,
                reference[:24],
                exc,
                exc_info=True,
            )


def _apply_bundle(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    document: dict[str, Any],
    body: Mapping[str, Any],
    *,
    uncommitted_secret_references: list[str],
) -> str:
    provider = body.get("provider")
    provider_access = body.get("provider_access")
    if not isinstance(provider, Mapping) or not isinstance(provider_access, Mapping):
        raise ModelRoutingError(
            "provider and provider_access objects are required")
    if "models" in provider:
        raise ModelRoutingError(
            "providers[].models was removed; send offerings and deployments",
            kind="legacy_model_routing_state_removed",
            field="provider.models",
        )
    provider_id = str(provider.get("provider_id") or "").strip()
    if not provider_id or provider_access.get("provider_id") != provider_id:
        raise ModelRoutingError(
            "provider_access.provider_id must equal provider.provider_id")
    for collection in (
        "connections", "credentials", "offerings", "deployments",
    ):
        if not isinstance(body.get(collection), list):
            raise ModelRoutingError(f"{collection} must be an array", field=collection)
    credential_secrets = body.get("credential_secrets") or {}
    if not isinstance(credential_secrets, Mapping):
        raise ModelRoutingError(
            "credential_secrets must be an object", field="credential_secrets")

    document["providers"].append(copy.deepcopy(dict(provider)))
    document["provider_accesses"].append(copy.deepcopy(dict(provider_access)))
    document["connections"].extend(copy.deepcopy(body["connections"]))
    credentials = copy.deepcopy(body["credentials"])
    for credential in credentials:
        if not isinstance(credential, dict):
            raise ModelRoutingError("credentials entries must be objects")
        credential_id = str(credential.get("credential_id") or "")
        if credential_id in credential_secrets:
            secret = repository.put_secret(
                boundary, str(credential_secrets[credential_id]))
            uncommitted_secret_references.append(secret["secret_reference"])
            credential["secret_reference"] = secret["secret_reference"]
            credential["key_hint"] = secret["key_hint"]
    document["credentials"].extend(credentials)
    document["offerings"].extend(copy.deepcopy(body["offerings"]))
    document["deployments"].extend(copy.deepcopy(body["deployments"]))
    for collection in ("creators", "models"):
        values = body.get(collection) or []
        if not isinstance(values, list):
            raise ModelRoutingError(f"{collection} must be an array", field=collection)
        document[collection].extend(copy.deepcopy(values))
    return provider_id


@api_v1_providers_bp.route("/api/v1/model-routing", methods=["GET"])
@require_scope("providers")
@api_meta(summary="Get the owner model-routing v2 authority", tags=["providers"], scope="providers")
def get_model_routing():
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Model routing authority not found")
    authority = _repository().get(boundary)
    return api_ok(model_routing=authority.public_document(), revision=authority.revision)


@api_v1_providers_bp.route("/api/v1/model-routing", methods=["PUT"])
@require_scope("providers")
@api_meta(summary="CAS replace the owner model-routing v2 authority", tags=["providers"], scope="providers")
def put_model_routing():
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Model routing authority not found")
    body = parse_body()
    rejection = _contract_rejection("putModelRouting", body)
    if rejection is not None:
        return rejection
    try:
        expected = _expected_revision(body)
        document = body.get("model_routing")
        if not isinstance(document, Mapping):
            raise ModelRoutingError("model_routing must be an object")
        committed = _repository().compare_and_swap(
            boundary, document, expected_revision=expected)
        return api_ok(
            model_routing=committed.public_document(), revision=committed.revision)
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        if "conflict" in str(getattr(exc, "code", "")).lower():
            return _error(exc)
        return api_internal_error(exc, context="api_v1.model_routing.put")


@api_v1_providers_bp.route("/api/v1/providers", methods=["GET"])
@require_scope("providers")
@api_meta(summary="List service providers and owner access pools", tags=["providers"], scope="providers")
def list_providers_route():
    boundary = _boundary()
    if boundary is None:
        return api_ok(providers=[], revision=0)
    authority = _repository().get(boundary)
    providers = [
        _provider_bundle(authority.document, row["provider_id"])
        for row in authority.document["providers"]
    ]
    return api_ok(providers=[row for row in providers if row], revision=authority.revision)


@api_v1_providers_bp.route("/api/v1/providers/templates", methods=["GET"])
@require_scope("providers")
@api_meta(summary="List provider onboarding recipes", tags=["providers"], scope="providers")
def list_provider_templates_route():
    try:
        return api_ok(items=load_provider_templates())
    except ProviderTemplateRecipeError as exc:
        return api_internal_error(exc, context="api_v1.providers.templates")


@api_v1_providers_bp.route(
    "/api/v1/providers/templates/compile", methods=["POST"])
@require_scope("providers")
@api_meta(summary="Compile a provider recipe into a v2 access draft", tags=["providers"], scope="providers")
def compile_provider_template_route():
    body = parse_body()
    rejection = _contract_rejection("compileProviderTemplate", body)
    if rejection is not None:
        return rejection
    selected = body.get("selected_model_ids")
    if selected is not None and not isinstance(selected, list):
        return api_bad_request(
            "selected_model_ids must be an array", field="selected_model_ids")
    try:
        bundle = compile_provider_template_bundle(
            body.get("template_key"),
            selected_model_ids=selected,
        )
        return api_ok(provider_bundle=bundle)
    except ProviderTemplateRecipeError as exc:
        return api_bad_request(str(exc), field="template_key")


@api_v1_providers_bp.route("/api/v1/providers/probe", methods=["POST"])
@require_scope("providers")
@api_meta(
    summary="Probe an OpenAI-compatible endpoint and compile a v2 ProviderAccess draft",
    tags=["providers"],
    scope="providers",
)
def probe_provider_route():
    """Discover transport facts without persisting or returning plaintext."""

    body = parse_body()
    rejection = _contract_rejection("probeProvider", body)
    if rejection is not None:
        return rejection
    base_url = str(body.get("base_url") or "").strip()
    api_key = str(body.get("api_key") or "").strip()
    models_path = str(body.get("models_path") or "").strip()
    if not base_url:
        return api_bad_request("base_url is required", field="base_url")

    from lib.llm_dispatch.discovery import is_local_endpoint, probe_provider

    if not api_key and not is_local_endpoint(base_url):
        return api_bad_request("api_key is required", field="api_key")
    try:
        result = probe_provider(base_url, api_key, models_path=models_path)
        if not result.get("ok"):
            return api_ok(**result)
        effective_url = str(result.get("base_url") or base_url).strip()
        provider_id = discovered_provider_id(result.get("brand"), effective_url)
        bundle = build_discovered_provider_bundle(
            provider_id=provider_id,
            display_name=str(result.get("name") or "Discovered provider"),
            brand=str(result.get("brand") or "generic"),
            base_url=effective_url,
            models=result.get("models") or [],
            protocol=("local" if result.get("is_local") else "openai"),
        )
        public_result = {
            key: value for key, value in result.items()
            if key not in {"models", "balance_url", "thinking_format"}
        }
        return api_ok(
            **public_result,
            provider_bundle=bundle,
            credential_id=bundle["credentials"][0]["credential_id"],
            model_count=len(bundle["offerings"]),
        )
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.providers.probe_v2")


# ── Access-matrix cell probe ────────────────────────────────────────────
# Settings → 服务商 → 访问矩阵 probes every (credential × wire id) pair of one
# ProviderAccess. Columns are the access's credentials in document order (the
# frontend renders the same order, so cell key ``<idx>::<wire_id>`` aligns);
# rows are confirmed offerings, each probed through its enabled deployments'
# wire ids (falling back to the canonical model id when none are known).


def _probe_cell_cache_key(boundary: OwnerBoundary, provider_id: str) -> str:
    return f"u{boundary.owner_user_id}_{provider_id}"


def _read_persisted_probe_snapshot(cache_key: str) -> dict[str, Any] | None:
    """Read a public snapshot and terminalise impossible post-restart work."""
    cache_path = probe_cache_path(cache_key)
    cached = read_json(cache_path, default=None)
    if not isinstance(cached, dict):
        return None
    cached = normalize_probe_snapshot(cached)
    # A live generation is always present in CELL_PROBE_TASKS. A disk-only
    # ``running`` row therefore belongs to a dead process; returning it would
    # make Settings poll forever for work that cannot resume.
    if cached.get("status") == "running":
        cached = dict(cached)
        cached["status"] = "error"
        cached["finished_at"] = cached.get("finished_at") or _time.time()
        cached["error"] = (
            "Provider probe was interrupted by a server restart; run it again."
        )
        try:
            write_json_atomic(cache_path, cached, fsync=False)
        except Exception as error:
            logger.warning(
                "[CellProbe] failed to settle interrupted snapshot for %s: %s",
                cache_key, error,
            )
    return cached


def _matrix_probe_plan(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
    bundle: Mapping[str, Any],
) -> dict[str, Any]:
    """Resolve one ProviderAccess bundle into engine probe inputs.

    Returns ``{provider, models, api_keys, oauth, adapter, extra_headers}``:
    ``api_keys[i]`` is the resolved plaintext key for column i (never leaves
    the process; ``public_probe_snapshot`` strips the task's private fields).
    A pure subscription access (every credential decodes to an oauth name)
    switches the task to the engine's oauth mode, which resolves the live
    token per cell instead of probing the stored sentinel.
    """
    connections = [
        row for row in bundle["connections"]
        if row.get("enabled", True) and str(row.get("base_url") or "").strip()
    ]
    if not connections:
        raise ModelRoutingError(
            "provider access has no enabled connection with a base_url",
            kind="provider_probe_no_connection",
        )
    primary = connections[0]
    credentials = list(bundle["credentials"])
    if not credentials:
        raise ModelRoutingError(
            "provider access has no credentials to probe",
            kind="provider_probe_no_credentials",
        )
    api_keys: list[str] = []
    oauth_names: list[str] = []
    for credential in credentials:
        reference = str(credential.get("secret_reference") or "")
        plaintext = ""
        if reference:
            try:
                plaintext = repository.resolve_secret(boundary, reference)
            except ModelRoutingError:
                plaintext = ""
        api_key, oauth, _headers = decode_credential_secret(
            plaintext, kind=str(credential.get("kind") or "api_key"))
        api_keys.append(api_key)
        oauth_names.append(oauth)
    oauth_mode = (
        bool(credentials)
        and all(name and name == oauth_names[0] for name in oauth_names)
    )
    deployments_by_offering: dict[str, list[str]] = {}
    for deployment in bundle["deployments"]:
        if deployment.get("enabled", True) is False:
            continue
        wire_id = str(deployment.get("wire_model_id") or "").strip()
        if not wire_id:
            continue
        rows = deployments_by_offering.setdefault(deployment["offering_id"], [])
        if wire_id not in rows:
            rows.append(wire_id)
    models: list[dict[str, Any]] = []
    for offering in bundle["offerings"]:
        if offering.get("identity_state") != "confirmed" or not offering.get("model"):
            continue
        entry: dict[str, Any] = {
            "model_id": str(offering["model"].get("model_id") or ""),
            "aliases": [],
            "capabilities": list(offering.get("capabilities") or []),
        }
        wire_ids = deployments_by_offering.get(offering["offering_id"], [])
        if wire_ids:
            entry["request_ids"] = wire_ids
        models.append(entry)
    if not models:
        raise ModelRoutingError(
            "provider access has no confirmed offerings to probe",
            kind="provider_probe_no_offerings",
        )
    return {
        "provider": {
            "id": str(bundle["provider"]["provider_id"]),
            "base_url": str(primary.get("base_url") or ""),
            "protocol": str(primary.get("protocol") or "openai") or "openai",
            "faces": {},
        },
        "models": models,
        "api_keys": (["oauth-managed"] * len(credentials)) if oauth_mode else api_keys,
        "oauth": oauth_names[0] if oauth_mode else "",
        "adapter": dict(primary.get("adapter") or {}),
        "extra_headers": dict(primary.get("extra_headers") or {}),
    }


@api_v1_providers_bp.route(
    "/api/v1/providers/<provider_id>/probe-cells/start", methods=["POST"])
@require_scope("providers")
@api_meta(
    summary="Start or resume a per-(credential × wire id) reachability probe",
    tags=["providers"],
    scope="providers",
)
def probe_provider_cells_start(provider_id: str):
    """Server-owned background probe behind the Settings access matrix.

    Body: ``{timeout?: int, attempts?: int, force?: bool,
    only?: {key_idxs?: [int], model_ids?: [str]}}``. ``attempts`` (default 3,
    clamped 1..5) re-probes a transiently-failing cell to filter false 429s.
    ``only`` restricts the run to rows/columns/cells and MERGES the fresh
    verdicts into the persisted snapshot. The full (credential × wire id)
    grid is always probed regardless of authorization state — the matrix
    exists to discover reachable pairs the allow-list does not grant yet.
    """
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    body = parse_body()
    rejection = _contract_rejection("startProviderProbeCells", body)
    if rejection is not None:
        return rejection
    try:
        timeout = max(
            PROVIDER_PROBE_MIN_TIMEOUT_SECONDS,
            min(PROVIDER_PROBE_MAX_TIMEOUT_SECONDS,
                int(body.get("timeout") or 30)),
        )
        attempts = max(
            1,
            min(PROVIDER_PROBE_MAX_ATTEMPTS, int(body.get("attempts") or 3)),
        )
    except (TypeError, ValueError, OverflowError):
        return api_bad_request("timeout and attempts must be integers")
    force = bool(body.get("force"))
    only = body.get("only") if isinstance(body.get("only"), Mapping) else {}
    _ok = only.get("key_idxs")
    _om = only.get("model_ids")
    only_key_idxs = ({i for i in _ok if isinstance(i, int) and not isinstance(i, bool)}
                     if isinstance(_ok, list) else None)
    only_model_ids = ({x.strip() for x in _om if isinstance(x, str) and x.strip()}
                      if isinstance(_om, list) else None)
    scoped = bool(only_key_idxs) or bool(only_model_ids)

    repository = _repository()
    cache_key = _probe_cell_cache_key(boundary, provider_id)
    try:
        with CELL_PROBE_LOCK:
            existing = CELL_PROBE_TASKS.get(cache_key)
            if existing and existing["status"] == "running":
                logger.info(
                    "[CellProbe] %s already running — returning live snapshot",
                    cache_key,
                )
                return api_ok(public_probe_snapshot(existing))
            if existing and not force and not scoped:
                return api_ok(public_probe_snapshot(existing))

        if not force and not scoped:
            cached = _read_persisted_probe_snapshot(cache_key)
            if isinstance(cached, dict) and cached.get("cells"):
                logger.info(
                    "[CellProbe] %s resumed from disk (%d cells, status=%s)",
                    cache_key, len(cached["cells"]), cached.get("status"),
                )
                return api_ok(cached)

        authority = repository.get(boundary)
        bundle = _provider_bundle(authority.public_document(), provider_id)
        if bundle is None or bundle.get("provider_access") is None:
            return api_not_found("provider not found")
        plan = _matrix_probe_plan(repository, boundary, bundle)

        work = build_probe_work(
            plan["provider"], plan["models"], plan["api_keys"],
            maximum_cells=PROVIDER_PROBE_MAX_CELLS + 1,
        )
        if not work:
            return api_bad_request("no testable (credential, model) pairs")
        if len(work) > PROVIDER_PROBE_MAX_CELLS:
            return api_bad_request(
                "too many cells to probe (%d > %d)"
                % (len(work), PROVIDER_PROBE_MAX_CELLS))

        # Scoped probe: restrict the run to the requested cells and seed the
        # task with the persisted snapshot's OTHER cells so the result merges
        # into the full grid. Seeds are pruned to the provider's current
        # (credential × wire id) space — a model/credential deleted since the
        # cache was written must not come back as a ghost pip.
        seed_cells: dict[str, Any] = {}
        if scoped:
            full_work = work
            work = [w for w in full_work
                    if (not only_key_idxs or w[0] in only_key_idxs)
                    and (not only_model_ids or w[3] in only_model_ids)]
            if not work:
                return api_bad_request("no cells match the requested scope")
            probed_keys = {probe_cell_key(w[0], w[3]) for w in work}
            valid_keys = {probe_cell_key(w[0], w[3]) for w in full_work}
            cached = _read_persisted_probe_snapshot(cache_key)
            if isinstance(cached, dict) and isinstance(cached.get("cells"), dict):
                for key, value in cached["cells"].items():
                    if key in valid_keys and key not in probed_keys \
                            and isinstance(value, dict):
                        seed_cells[key] = value

        task = {
            "provider_id": cache_key,
            "status": "running",
            "started_at": _time.time(),
            "finished_at": None,
            "total": len(work),
            "done_count": 0,
            "cells": dict(seed_cells),
            "summary": {"ok": 0, "disable": 0},
            "error": None,
            "attempts": attempts,
            "_abort": False,
            "_base_url": plan["provider"]["base_url"],
            "_extra_headers": plan["extra_headers"],
            "_protocol": plan["provider"]["protocol"],
            "_oauth": plan["oauth"],
            "_adapter": plan["adapter"],
            "_owner_user_id": boundary.owner_user_id,
        }
        with CELL_PROBE_LOCK:
            existing = CELL_PROBE_TASKS.get(cache_key)
            if existing and existing.get("status") == "running":
                return api_ok(public_probe_snapshot(existing))
            CELL_PROBE_TASKS[cache_key] = task
            if hasattr(CELL_PROBE_TASKS, "move_to_end"):
                CELL_PROBE_TASKS.move_to_end(cache_key)
            try:
                submit_provider_probe_task(
                    task, work, timeout, runner=run_cell_probe_task)
            except FairWorkLaneQueueFull:
                if CELL_PROBE_TASKS.get(cache_key) is task:
                    CELL_PROBE_TASKS.pop(cache_key, None)
                return api_service_unavailable(
                    "Provider probe queue is full; retry shortly.",
                    retry_after=3,
                    retryable=True,
                )
            except Exception as error:
                if CELL_PROBE_TASKS.get(cache_key) is task:
                    CELL_PROBE_TASKS.pop(cache_key, None)
                return api_internal_error(error, context="provider-probe-submit")

        logger.info(
            "[CellProbe] Admitted background probe for %s "
            "(%d cells, force=%s, scoped=%s, seeded=%d)",
            cache_key, len(work), force, scoped, len(seed_cells),
        )
        return api_ok(public_probe_snapshot(task))
    except ModelRoutingError as exc:
        return _error(exc)


@api_v1_providers_bp.route(
    "/api/v1/providers/<provider_id>/probe-cells/status", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="Poll a provider's access-matrix probe progress",
    tags=["providers"],
    scope="providers",
)
def probe_provider_cells_status(provider_id: str):
    """Live task if any, else the disk snapshot, else ``{status: 'none'}``."""
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    cache_key = _probe_cell_cache_key(boundary, provider_id)
    with CELL_PROBE_LOCK:
        task = CELL_PROBE_TASKS.get(cache_key)
        if task:
            return api_ok(public_probe_snapshot(task))
    cached = _read_persisted_probe_snapshot(cache_key)
    if isinstance(cached, dict) and cached.get("cells") is not None:
        return api_ok(cached)
    return api_ok({"status": "none"})


@api_v1_providers_bp.route("/api/v1/providers", methods=["POST"])
@require_scope("providers")
@api_meta(summary="Create one ProviderAccess aggregate", tags=["providers"], scope="providers")
def create_provider_route():
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    body = parse_body()
    rejection = _contract_rejection("createProvider", body)
    if rejection is not None:
        return rejection
    repository = _repository()
    uncommitted_secret_references: list[str] = []
    try:
        expected = _expected_revision(body)
        authority = repository.get(boundary)
        if authority.revision != expected:
            raise ModelRoutingError(
                "model-routing revision changed",
                kind="model_routing_revision_conflict")
        document = copy.deepcopy(authority.document)
        provider_id = _apply_bundle(
            repository,
            boundary,
            document,
            body,
            uncommitted_secret_references=uncommitted_secret_references,
        )
        committed = repository.compare_and_swap(
            boundary, normalize_document(document), expected_revision=expected)
        uncommitted_secret_references.clear()
        audit_log(
            "model_routing_provider_access_created",
            owner_user_id=boundary.owner_user_id,
            provider_id=provider_id,
            revision=committed.revision,
        )
        return api_created(
            provider=_provider_bundle(committed.public_document(), provider_id),
            revision=committed.revision,
        )
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.providers.create_v2")
    finally:
        _reclaim_secret_references(
            repository, boundary, uncommitted_secret_references)


@api_v1_providers_bp.route("/api/v1/providers/<provider_id>", methods=["GET"])
@require_scope("providers")
@api_meta(summary="Get one ProviderAccess aggregate", tags=["providers"], scope="providers")
def get_provider_route(provider_id: str):
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Provider not found")
    authority = _repository().get(boundary)
    bundle = _provider_bundle(authority.public_document(), provider_id)
    if bundle is None:
        return api_not_found("Provider not found")
    return api_ok(provider=bundle, revision=authority.revision)


@api_v1_providers_bp.route("/api/v1/providers/<provider_id>", methods=["PATCH"])
@require_scope("providers")
@api_meta(summary="CAS replace one ProviderAccess aggregate", tags=["providers"], scope="providers")
def update_provider_route(provider_id: str):
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Provider not found")
    body = parse_body()
    rejection = _contract_rejection("updateProvider", body)
    if rejection is not None:
        return rejection
    repository = _repository()
    uncommitted_secret_references: list[str] = []
    try:
        expected = _expected_revision(body)
        authority = repository.get(boundary)
        if authority.revision != expected:
            raise ModelRoutingError(
                "model-routing revision changed", kind="model_routing_revision_conflict")
        document = copy.deepcopy(authority.document)
        if _provider_bundle(document, provider_id) is None:
            return api_not_found("Provider not found")
        old_secrets = _remove_provider(document, provider_id)
        replacement_id = _apply_bundle(
            repository,
            boundary,
            document,
            body,
            uncommitted_secret_references=uncommitted_secret_references,
        )
        if replacement_id != provider_id:
            raise ModelRoutingError("provider_id cannot change", field="provider.provider_id")
        committed = repository.compare_and_swap(
            boundary, normalize_document(document), expected_revision=expected)
        uncommitted_secret_references.clear()
        active_refs = {
            credential["secret_reference"]
            for credential in committed.document["credentials"]
        }
        _reclaim_secret_references(
            repository,
            boundary,
            [reference for reference in old_secrets
             if reference and reference not in active_refs],
        )
        return api_ok(
            provider=_provider_bundle(committed.public_document(), provider_id),
            revision=committed.revision,
        )
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.providers.update_v2")
    finally:
        _reclaim_secret_references(
            repository, boundary, uncommitted_secret_references)


@api_v1_providers_bp.route("/api/v1/providers/<provider_id>", methods=["DELETE"])
@require_scope("providers")
@api_meta(summary="Delete one ProviderAccess aggregate", tags=["providers"], scope="providers")
def delete_provider_route(provider_id: str):
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Provider not found")
    body = parse_body()
    rejection = _contract_rejection("deleteProvider", body)
    if rejection is not None:
        return rejection
    repository = _repository()
    try:
        expected = _expected_revision(body)
        authority = repository.get(boundary)
        if authority.revision != expected:
            raise ModelRoutingError(
                "model-routing revision changed", kind="model_routing_revision_conflict")
        document = copy.deepcopy(authority.document)
        if _provider_bundle(document, provider_id) is None:
            return api_not_found("Provider not found")
        secrets_to_delete = _remove_provider(document, provider_id)
        committed = repository.compare_and_swap(
            boundary, normalize_document(document), expected_revision=expected)
        _reclaim_secret_references(
            repository, boundary, secrets_to_delete)
        audit_log(
            "model_routing_provider_access_deleted",
            owner_user_id=boundary.owner_user_id,
            provider_id=provider_id,
            revision=committed.revision,
        )
        return api_ok(deleted=provider_id, revision=committed.revision)
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.providers.delete_v2")


@api_v1_providers_bp.route(
    "/api/v1/model-routing/credentials/<credential_id>/secret", methods=["PUT"])
@require_scope("providers")
@api_meta(summary="Replace one encrypted credential secret", tags=["providers"], scope="providers")
def replace_credential_secret(credential_id: str):
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Credential not found")
    body = parse_body()
    rejection = _contract_rejection("putCredentialSecret", body)
    if rejection is not None:
        return rejection
    repository = _repository()
    uncommitted_secret_references: list[str] = []
    try:
        expected = _expected_revision(body)
        secret_value = body.get("secret")
        if not isinstance(secret_value, str):
            raise ModelRoutingError("secret must be a string", field="secret")
        authority = repository.get(boundary)
        if authority.revision != expected:
            raise ModelRoutingError(
                "model-routing revision changed", kind="model_routing_revision_conflict")
        credential = next(
            (row for row in authority.document["credentials"]
             if row["credential_id"] == credential_id),
            None,
        )
        if credential is None:
            return api_not_found("Credential not found")
        prior_reference = credential["secret_reference"]
        stored = repository.put_secret(boundary, secret_value)
        uncommitted_secret_references.append(stored["secret_reference"])
        document = copy.deepcopy(authority.document)
        for row in document["credentials"]:
            if row["credential_id"] == credential_id:
                row["secret_reference"] = stored["secret_reference"]
                row["key_hint"] = stored["key_hint"]
        committed = repository.compare_and_swap(
            boundary, document, expected_revision=expected)
        uncommitted_secret_references.clear()
        if prior_reference and prior_reference != stored["secret_reference"]:
            _reclaim_secret_references(
                repository, boundary, [prior_reference])
        return api_ok(
            credential_id=credential_id,
            key_hint=stored["key_hint"],
            revision=committed.revision,
        )
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_routing.secret")
    finally:
        _reclaim_secret_references(
            repository, boundary, uncommitted_secret_references)


@api_v1_providers_bp.route(
    "/api/v1/model-routing/credentials/<credential_id>/secret/reveal", methods=["POST"])
@require_scope("providers")
@api_meta(summary="Reveal one credential secret plaintext (audited)", tags=["providers"], scope="providers")
def reveal_credential_secret(credential_id: str):
    """Return the plaintext for one owner credential — the Settings key-card
    Show button. POST (not GET) so the deliberate read never rides a shared
    prefetch/cache path; every call is audit-logged like the vault reveal."""
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Credential not found")
    repository = _repository()
    try:
        authority = repository.get(boundary)
        credential = next(
            (row for row in authority.document["credentials"]
             if row["credential_id"] == credential_id),
            None,
        )
        if credential is None:
            return api_not_found("Credential not found")
        secret = repository.resolve_secret(
            boundary, str(credential.get("secret_reference") or ""))
        audit_log(
            "model_routing_credential_secret_revealed",
            owner_user_id=boundary.owner_user_id,
            credential_id=credential_id,
        )
        return api_ok(credential_id=credential_id, secret=secret)
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_routing.secret.reveal")


def _legacy_sources_for_owner(boundary: OwnerBoundary) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from lib import _load_server_config
    from lib.byo_providers import get_provider, list_providers

    config = _load_server_config()
    rows = list_providers(
        boundary.owner_user_id, tenant_id=boundary.tenant_id)
    internal = [
        provider for row in rows
        if (provider := get_provider(
            row["id"], boundary.owner_user_id, tenant_id=boundary.tenant_id))
        is not None
    ]
    return config, internal


@api_v1_providers_bp.route("/api/v1/model-routing/migration/plan", methods=["POST"])
@require_scope("providers")
@api_meta(summary="Build a redacted one-way model-routing migration plan", tags=["providers"], scope="providers")
def migration_plan_route():
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    try:
        config, byo = _legacy_sources_for_owner(boundary)
        plan = plan_legacy_migration(config, byo_providers=byo)
        return api_ok(migration_plan=plan.public_dict())
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_routing.migration_plan")


@api_v1_providers_bp.route("/api/v1/model-routing/migration/commit", methods=["POST"])
@require_scope("providers")
@api_meta(summary="Validate and atomically activate model-routing v2", tags=["providers"], scope="providers")
def migration_commit_route():
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    try:
        config, byo = _legacy_sources_for_owner(boundary)
        plan = plan_legacy_migration(config, byo_providers=byo)
        result = execute_migration(_repository(), boundary, plan)
        if not result.enabled:
            return api_conflict(
                "model-routing migration did not activate",
                kind="model_routing_migration_failed",
                migration_receipt=result.receipt,
            )
        audit_log(
            "model_routing_v2_migrated",
            owner_user_id=boundary.owner_user_id,
            revision=result.authority.revision if result.authority else 0,
            source_digest=plan.source_digest,
        )
        return api_ok(
            model_routing=result.authority.public_document() if result.authority else None,
            revision=result.authority.revision if result.authority else 0,
            migration_receipt=result.receipt,
        )
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_routing.migration_commit")


__all__ = ["api_v1_providers_bp"]
