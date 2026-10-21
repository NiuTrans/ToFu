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
    api_bad_request as api_bad_request,
    api_conflict as api_conflict,
    api_created as api_created,
    api_internal_error as api_internal_error,
    api_not_found as api_not_found,
    api_ok as api_ok,
    api_service_unavailable as api_service_unavailable,
)
from lib.log import audit_log, get_logger
from lib.model_routing import (
    build_discovered_provider_bundle,
    discovered_provider_id,
    ModelRoutingError,
    ModelRoutingRepository,
    OwnerBoundary,
    execute_migration,
    plan_legacy_migration,
)
from lib.openapi import api_meta
from lib.provider.provider_headers import sanitise_extra_headers as sanitise_extra_headers
from lib.provider.provider_template_recipes import (
    ProviderTemplateRecipeError,
    compile_provider_template_bundle,
    load_provider_templates,
)
from lib.catpaw_local_import import (
    CatpawLocalImportError,
    cli_installed as catpaw_cli_installed,
    detect_local_key as catpaw_detect_local_key,
    kick_cli_login as catpaw_kick_cli_login,
    read_local_key as catpaw_read_local_key,
    secret_envelope as catpaw_secret_envelope,
)
from lib.request_parser import parse_body
from lib.generated_api_v1_model_routing import (
    ContractViolation,
    decode_request,
)

from .auth import current_auth, require_scope

# Access-matrix probe engine (Settings → 服务商 → 访问矩阵). Plaintext keys
# are resolved server-side from the owner-scoped secret store; the browser
# only ever addresses credentials by their column index.
from lib.json_store import read_json, write_json_atomic
from lib.provider.provider_probe import (
    CELL_PROBE_LOCK,
    CELL_PROBE_TASKS,
    align_probe_snapshot,
    build_probe_work,
    normalize_probe_snapshot,
    probe_cache_path,
    probe_cell_key,
    public_probe_snapshot,
    run_cell_probe_task,
    submit_provider_probe_task,
)
from lib.provider.provider_probe_policy import (
    PROVIDER_PROBE_MAX_ATTEMPTS,
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


def _hot_reload_dispatcher_slots() -> None:
    """Drop the dispatcher singleton after a committed authority change.

    ``reset_dispatcher`` is the public lifecycle port; the next pick rebuilds
    slots from the current v2 document. A reset failure must not fail the
    request — the commit already landed.
    """
    try:
        from lib.llm_dispatch import reset_dispatcher
        reset_dispatcher()
    except Exception as exc:
        logger.error(
            "model-routing committed but dispatcher reset failed: %s", exc,
            exc_info=True)


def _sync_one_card_from_template(boundary, repository, provider_id: str) -> dict | None:
    """Converge ONE installed card with its provider template, incrementally.

    A template seeds NEW installs only, so a card created before a recipe
    gained a face-pinned wire id never grows that alias on its own — and the
    matrix is exactly where the gap becomes visible (the row has no cell for
    the working wire id, so every visible cell is the broken one). Converge
    it here with deterministic additions only: no probe, no network, no
    verdicts consumed. A template that does not match this card is a no-op.

    Returns the sync stats, or None when the card has no template. Failures
    are diagnostics: the probe must still run against the current document.
    """
    from lib.model_routing.template_supply import sync_template_supply

    document = repository.get(boundary).document
    access_ids = [str(row.get('provider_access_id') or '')
                  for row in document.get('provider_accesses') or []
                  if isinstance(row, dict)
                  and row.get('provider_id') == provider_id]
    access_ids = [value for value in access_ids if value]
    if not access_ids:
        return None
    try:
        stats = sync_template_supply(
            boundary=boundary, repository=repository,
            provider_access_ids=access_ids, backfill_only=True)
    except Exception as exc:
        logger.warning(
            'template supply: incremental sync failed for %s: %s',
            provider_id, exc, exc_info=True)
        return None
    if (stats.get('added_connections') or stats.get('added_deployments')
            or stats.get('repaired_connections')):
        _hot_reload_dispatcher_slots()
    return stats

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
    if kind.startswith("provider_probe_"):
        return api_bad_request({"kind": kind, "message": str(exc)},
                               **{key: value for key, value in extras.items() if key != "kind"})
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


from lib.model_routing.probe_plan import provider_bundle as _provider_bundle


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
    from lib.model_routing.catalog_merge import merge_missing_catalog_entities
    merge_missing_catalog_entities(document, body)
    return provider_id


def _refresh_legacy_key_hints(repository, boundary, authority):
    """Re-seal credentials whose stored hint predates the asterisk mask format.

    Existing secrets were persisted with the `head…tail` ellipsis hint; the
    settings document keeps serving that format until each secret is
    re-sealed once. A CAS conflict just defers the rewrite to the next read.
    """
    stale = [
        row for row in (authority.document.get("credentials") or [])
        if isinstance(row, dict)
        and "…" in str(row.get("key_hint") or "")
        and str(row.get("secret_reference") or "")
    ]
    if not stale:
        return authority
    hints: dict[str, str] = {}
    for row in stale:
        reference = str(row["secret_reference"])
        try:
            plaintext = repository.resolve_secret(boundary, reference)
            metadata = repository.put_secret(
                boundary, plaintext, secret_reference=reference)
        except ModelRoutingError:
            continue
        hints[reference] = str(metadata.get("key_hint") or "")
    if not hints:
        return authority

    working = copy.deepcopy(authority.document)
    for credential in working.get("credentials") or []:
        if not isinstance(credential, dict):
            continue
        hint = hints.get(str(credential.get("secret_reference") or ""))
        if hint:
            credential["key_hint"] = hint
    try:
        return repository.compare_and_swap(
            boundary, working, expected_revision=authority.revision)
    except ModelRoutingError:
        return authority


@api_v1_providers_bp.route("/api/v1/model-routing", methods=["GET"])
@require_scope("providers")
@api_meta(summary="Get the owner model-routing v2 authority", tags=["providers"], scope="providers")
def get_model_routing():
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Model routing authority not found")
    repository = _repository()
    authority = repository.get(boundary)
    authority = _refresh_legacy_key_hints(repository, boundary, authority)
    from lib.model_routing.controls import reasoning_controls
    return api_ok(model_routing=authority.public_document(), revision=authority.revision,
                  metadata_inheritance_supported=True,
                  reasoning_controls=reasoning_controls(authority.document))


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
        from lib.model_routing.registration import prepare_registration_document
        repository = _repository()
        authority = repository.get(boundary)
        if authority.revision != expected:
            raise ModelRoutingError("model-routing revision changed", kind="model_routing_revision_conflict")
        document = prepare_registration_document(document, previous=authority.document)
        committed = repository.compare_and_swap(
            boundary, document, expected_revision=expected)
        _hot_reload_dispatcher_slots()
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
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Model routing authority not found")
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
            official_directory=_repository().get(boundary).document['models'],
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

    boundary = _boundary()
    if boundary is None:
        return api_not_found("Model routing authority not found")
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
            official_directory=_repository().get(boundary).document['models'],
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
# rows are confirmed enabled offerings, each probed through all of its
# request names (falling back to the canonical model id when none are known).
# Disabled Offerings are omitted; credential grants do not filter the grid.


def _probe_cell_cache_key(boundary: OwnerBoundary, provider_id: str) -> str:
    return f"u{boundary.owner_user_id}_{provider_id}"


def _probe_credential_ids(repository, boundary, provider_id: str) -> list[str] | None:
    """Ordered credential column identities of the provider's current grid."""
    try:
        bundle = _provider_bundle(repository.get(boundary).public_document(), provider_id)
    except Exception as error:
        logger.warning("[CellProbe] cannot resolve credential columns for %s: %s",
                       provider_id, error)
        return None
    if not bundle:
        return None
    return [str(row["credential_id"]) for row in bundle["credentials"]]


def _read_persisted_probe_snapshot(
    cache_key: str,
    credential_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """Read a public snapshot and terminalise impossible post-restart work.

    ``credential_ids`` re-binds positional column evidence to the CURRENT
    credential identities. Without it a key deleted after the probe leaves its
    verdicts sitting on whichever key inherited the same column index.
    """
    cache_path = probe_cache_path(cache_key)
    cached = read_json(cache_path, default=None)
    if not isinstance(cached, dict):
        return None
    cached = normalize_probe_snapshot(cached)
    if credential_ids is not None:
        cached = align_probe_snapshot(cached, credential_ids)
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


from lib.model_routing.probe_plan import (
    matrix_probe_plan as _matrix_probe_plan,
    manual_probe_bundle,
)


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
    verdicts into the persisted snapshot. Manual diagnostics include disabled
    Offerings/Connections and ungranted cells without changing routing state.

    One bounded exception, and it is a WRITE: the card is first converged with
    its provider template (``sync_template_supply(backfill_only=True)`` scoped
    to this access), so a wire id the template gained after this card was
    installed actually has a cell to probe. That step adds template-derived
    Connections/Deployments only — it never probes, never edits owner values,
    and never touches another provider. Without it the matrix shows only the
    pre-existing cells, so a row whose public id is rate-limited reads as
    "this model does not work" even when a working wire exists.
    Optional temporary request names use saved owner-scoped transport/identity
    references; no draft is saved and completion cannot mint grants.
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

    # Explicit draft targets mean new work, never a cached completed run.
    explicit_targets = "targets" in body
    repository = _repository()
    cache_key = _probe_cell_cache_key(boundary, provider_id)
    try:
        authority = repository.get(boundary)
        bundle = _provider_bundle(authority.public_document(), provider_id)
        if bundle is None or bundle.get("provider_access") is None:
            return api_not_found("provider not found")
        if "credential_ids" in body and body["credential_ids"] != [
            row["credential_id"] for row in bundle["credentials"]
        ]:
            raise ModelRoutingError(
                "The credential columns have changed. Refresh Settings before detecting so the intended key is tested.",
                kind="provider_probe_stale_selection", field="credential_ids")
        # Converge THIS card with its template before planning, so the matrix
        # actually contains a cell for a wire id the template gained after the
        # card was installed. Without this the user sees only the pre-existing
        # (broken) cells and concludes the new request name does not work.
        # Deterministic additions only: no probe, no verdicts consumed.
        synced = _sync_one_card_from_template(boundary, repository, provider_id)
        card_changed = bool(synced and (synced.get("added_connections")
                                        or synced.get("added_deployments")
                                        or synced.get("repaired_connections")))
        if card_changed:
            authority = repository.get(boundary)
            bundle = _provider_bundle(authority.public_document(), provider_id)
            if bundle is None or bundle.get("provider_access") is None:
                return api_not_found("provider not found")
        if explicit_targets:
            bundle = manual_probe_bundle(bundle, body["targets"])
        credential_ids = [str(row["credential_id"]) for row in bundle["credentials"]]
        with CELL_PROBE_LOCK:
            existing = CELL_PROBE_TASKS.get(cache_key)
            if existing and existing["status"] == "running":
                logger.info(
                    "[CellProbe] %s already running — returning live snapshot",
                    cache_key,
                )
                return api_ok(public_probe_snapshot(existing))
            if (existing and not force and not scoped and not explicit_targets
                    and not card_changed):
                return api_ok(public_probe_snapshot(existing))

        # A card that just gained a wire id has cells the snapshot cannot
        # describe; resuming it would render the new row permanently unprobed.
        if not force and not scoped and not explicit_targets and not card_changed:
            cached = _read_persisted_probe_snapshot(cache_key, credential_ids)
            if isinstance(cached, dict) and cached.get("cells"):
                logger.info(
                    "[CellProbe] %s resumed from disk (%d cells, status=%s)",
                    cache_key, len(cached["cells"]), cached.get("status"),
                )
                return api_ok(cached)

        plan = _matrix_probe_plan(repository, boundary, bundle, manual=True)

        work = build_probe_work(
            plan["provider"], plan["models"], plan["api_keys"],
            request_contexts=plan.get("request_contexts"),
            credential_contexts=plan.get("credential_contexts"),
        )
        if not work:
            raise ModelRoutingError(
                "No testable request names were selected. Check the model's request name and saved connection.",
                kind="provider_probe_empty_scope")
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
                raise ModelRoutingError(
                    "The selected request name or credential is no longer in this matrix. Refresh Settings and retry.",
                    kind="provider_probe_empty_scope", field="only")
            probed_keys = {probe_cell_key(w[0], w[3]) for w in work}
            valid_keys = {probe_cell_key(w[0], w[3]) for w in full_work}
            cached = _read_persisted_probe_snapshot(cache_key, credential_ids)
            if isinstance(cached, dict) and isinstance(cached.get("cells"), dict):
                for key, value in cached["cells"].items():
                    if key in valid_keys and key not in probed_keys \
                            and isinstance(value, dict):
                        seed_cells[key] = value

        task = {
            "provider_id": cache_key,
            "credential_ids": credential_ids,
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
            # A live task carries the columns it actually measured, so it needs
            # no authority read on every poll.
            return api_ok(public_probe_snapshot(task))
    credential_ids = _probe_credential_ids(_repository(), boundary, provider_id)
    cached = _read_persisted_probe_snapshot(cache_key, credential_ids)
    if isinstance(cached, dict) and cached.get("cells") is not None:
        return api_ok(cached)
    return api_ok({"status": "none"})


# ── CatPaw one-click local import ───────────────────────────────────────
# Settings → 订阅登录 CatPaw card. The CatPaw/mcopilot CLI owns the only
# credential issuer (an interactive login writes a fixed loopback config), so
# there is no OAuth/device flow to offer. These two routes let a colocated
# desktop import that credential with one click instead of copy-paste. The
# plaintext token never crosses the API: GET returns a presence verdict,
# POST seals it into the owner credential envelope server-side.

_CATPAW_TEMPLATE_KEY = "catpaw"


def _catpaw_existing_access(document: dict[str, Any]) -> dict[str, Any] | None:
    """The owner's existing CatPaw access row, matched by template brand."""
    brand_to_id = {
        str(row.get("brand") or ""): row.get("provider_id")
        for row in document["providers"]
    }
    provider_id = brand_to_id.get(_CATPAW_TEMPLATE_KEY)
    if provider_id is None:
        return None
    for access in document["provider_accesses"]:
        if access.get("provider_id") == provider_id:
            return access
    return None


@api_v1_providers_bp.route(
    "/api/v1/providers/catpaw/local-import", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="CatPaw local-import availability (no secret returned)",
    tags=["providers"],
    scope="providers",
)
def catpaw_local_import_status():
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    detection = catpaw_detect_local_key()
    authority = _repository().get(boundary)
    existing = _catpaw_existing_access(authority.document)
    model_count = 0
    if existing is not None:
        access_id = existing.get("provider_access_id")
        model_count = sum(
            1 for row in authority.document["offerings"]
            if row.get("provider_access_id") == access_id)
    return api_ok(
        available=bool(detection.get("available")),
        connected=existing is not None,
        installed=catpaw_cli_installed(),
        model_count=model_count,
        revision=authority.revision,
    )


@api_v1_providers_bp.route(
    "/api/v1/providers/catpaw/local-import/login", methods=["POST"])
@require_scope("providers")
@api_meta(
    summary="Kick the CatPaw CLI interactive login and return its SSO link",
    tags=["providers"],
    scope="providers",
)
def catpaw_local_import_login():
    """Surface the ``mc`` SSO link so the UI can open it for the user.

    The CLI stays running detached and writes the credential on SSO success;
    the frontend then polls the status endpoint and auto-imports.
    """
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    result = catpaw_kick_cli_login()
    audit_log(
        "model_routing_catpaw_login_kick",
        owner_user_id=boundary.owner_user_id,
        started=bool(result.get("started")),
        login_url_captured=bool(result.get("login_url")),
    )
    return api_ok(**result)


@api_v1_providers_bp.route(
    "/api/v1/providers/catpaw/local-import", methods=["POST"])
@require_scope("providers")
@api_meta(
    summary="Import the local CatPaw CLI credential into model routing",
    tags=["providers"],
    scope="providers",
)
def catpaw_local_import_apply():
    """Create the CatPaw provider from the CLI key, or rotate an existing key."""
    boundary = _boundary()
    if boundary is None:
        return api_bad_request("caller has no repository owner identity")
    repository = _repository()
    uncommitted_secret_references: list[str] = []
    try:
        api_key = catpaw_read_local_key()
    except CatpawLocalImportError as exc:
        return api_bad_request("no usable local CatPaw credential", reason=exc.reason)
    try:
        authority = repository.get(boundary)
        expected = authority.revision
        document = copy.deepcopy(authority.document)
        existing = _catpaw_existing_access(document)
        envelope = catpaw_secret_envelope(api_key)
        provider_id: str
        if existing is not None:
            # Re-import on an already-connected provider rotates its first
            # credential in place; roster/base_url are left untouched.
            access_id = existing["provider_access_id"]
            credential = next(
                (row for row in document["credentials"]
                 if row.get("provider_access_id") == access_id),
                None,
            )
            if credential is None:
                raise ModelRoutingError("catpaw access has no credential to rotate")
            stored = repository.put_secret(boundary, envelope)
            uncommitted_secret_references.append(stored["secret_reference"])
            credential["secret_reference"] = stored["secret_reference"]
            credential["key_hint"] = stored["key_hint"]
            provider_id = str(existing["provider_id"])
        else:
            bundle = compile_provider_template_bundle(_CATPAW_TEMPLATE_KEY,
                official_directory=document['models'])
            body = {
                "provider": bundle["provider"],
                "provider_access": bundle["provider_access"],
                "connections": bundle["connections"],
                "credentials": bundle["credentials"],
                "offerings": bundle["offerings"],
                "deployments": bundle["deployments"],
                "creators": bundle["creators"],
                "models": bundle["models"],
                "credential_secrets": {
                    bundle["credentials"][0]["credential_id"]: envelope,
                },
            }
            provider_id = _apply_bundle(
                repository,
                boundary,
                document,
                body,
                uncommitted_secret_references=uncommitted_secret_references,
            )
        from lib.model_routing.registration import prepare_registration_document
        document = prepare_registration_document(document, previous=authority.document)
        committed = repository.compare_and_swap(
            boundary, document, expected_revision=expected)
        uncommitted_secret_references.clear()
        audit_log(
            "model_routing_catpaw_local_import",
            owner_user_id=boundary.owner_user_id,
            provider_id=provider_id,
            rotated=existing is not None,
            revision=committed.revision,
        )
        _hot_reload_dispatcher_slots()
        access_id = next(
            (row.get("provider_access_id")
             for row in committed.document["provider_accesses"]
             if row.get("provider_id") == provider_id),
            None,
        )
        model_count = sum(
            1 for row in committed.document["offerings"]
            if row.get("provider_access_id") == access_id)
        return api_ok(
            provider_id=provider_id,
            connected=True,
            created=existing is None,
            rotated=existing is not None,
            model_count=model_count,
            revision=committed.revision,
        )
    except ProviderTemplateRecipeError as exc:
        return api_internal_error(exc, context="api_v1.providers.catpaw_import")
    except ModelRoutingError as exc:
        return _error(exc)
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.providers.catpaw_import")
    finally:
        _reclaim_secret_references(
            repository, boundary, uncommitted_secret_references)

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
        from lib.model_routing.registration import prepare_registration_document
        document = prepare_registration_document(document, previous=authority.document)
        committed = repository.compare_and_swap(
            boundary, document, expected_revision=expected)
        uncommitted_secret_references.clear()
        audit_log(
            "model_routing_provider_access_created",
            owner_user_id=boundary.owner_user_id,
            provider_id=provider_id,
            revision=committed.revision,
        )
        _hot_reload_dispatcher_slots()
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
        from lib.model_routing.registration import prepare_registration_document
        document = prepare_registration_document(document, previous=authority.document)
        committed = repository.compare_and_swap(
            boundary, document, expected_revision=expected)
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
        _hot_reload_dispatcher_slots()
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
        from lib.model_routing.registration import prepare_registration_document
        document = prepare_registration_document(document, previous=authority.document)
        committed = repository.compare_and_swap(
            boundary, document, expected_revision=expected)
        _reclaim_secret_references(
            repository, boundary, secrets_to_delete)
        audit_log(
            "model_routing_provider_access_deleted",
            owner_user_id=boundary.owner_user_id,
            provider_id=provider_id,
            revision=committed.revision,
        )
        _hot_reload_dispatcher_slots()
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
        _hot_reload_dispatcher_slots()
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
    from lib.byo.byo_providers import get_provider, list_providers

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
        _hot_reload_dispatcher_slots()
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
