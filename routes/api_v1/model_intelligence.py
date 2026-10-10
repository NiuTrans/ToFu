"""Owner-scoped Artificial Analysis settings and score projection.

This boundary enriches canonical Creator/Model facts only.  It does not expose
or reconstruct the removed model-catalog authority, and it never reads model
supply, aliases, deployments, credentials belonging to providers, or routes.
The AA API key is stored encrypted through an owner-aware repository seam and
is never returned to the browser.
"""

from __future__ import annotations

from typing import Any

from quart import Blueprint

from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.log import audit_log
from lib.model_catalog.aa import aa_block_for_models, refresh_scores
from lib.model_catalog.aa_key import (
    AA_SECRET_REFERENCE as _AA_SECRET_REFERENCE,
)
from lib.model_catalog.aa_key import (
    clear_legacy_config_key as _remove_legacy_config_key,
)
from lib.model_catalog.aa_key import (
    resolve_effective_key as _effective_key,
)
from lib.model_catalog.openrouter import (
    fill_document_list_pricing,
    pricing_block_for_models,
    refresh_pricing_block,
)
from lib.model_routing import ModelRoutingError, ModelRoutingRepository, OwnerBoundary
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import current_auth, require_scope


api_v1_model_intelligence_bp = Blueprint("api_v1_model_intelligence", __name__)

_MAX_API_KEY_CHARACTERS = 256


def _boundary() -> OwnerBoundary | None:
    context = current_auth()
    if context is None or context.owner_user_id is None:
        return None
    return OwnerBoundary.create(context.owner_user_id, context.tenant_id)


def _repository() -> ModelRoutingRepository:
    """Encrypted owner-secret adapter; replaceable in tests and later stores."""
    return ModelRoutingRepository()


def _models(repository: ModelRoutingRepository, boundary: OwnerBoundary) -> list[dict[str, Any]]:
    authority = repository.get(boundary)
    return [dict(row) for row in authority.document.get("models", [])]


def _read_block(*, force: bool) -> dict[str, Any]:
    boundary = _boundary()
    if boundary is None:
        raise LookupError("Model intelligence settings not found")
    repository = _repository()
    models = _models(repository, boundary)
    api_key, key_source, key_hint = _effective_key(repository, boundary)
    projector = refresh_scores if force else aa_block_for_models
    return projector(
        models,
        api_key=api_key,
        key_source=key_source,
        key_hint=key_hint,
    )


@api_v1_model_intelligence_bp.route("/api/v1/model-intelligence/aa", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="Read owner Artificial Analysis score enrichment",
    description=(
        "Returns benchmark status and scores keyed by canonical Creator/Model "
        "identity. Provider supply and plaintext credentials are never included."
    ),
    tags=["models"],
    scope="providers",
)
def get_aa_scores():
    try:
        return api_ok(aa=_read_block(force=False))
    except LookupError:
        return api_not_found("Model intelligence settings not found")
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_intelligence.aa.get")


@api_v1_model_intelligence_bp.route(
    "/api/v1/model-intelligence/aa/refresh", methods=["POST"])
@require_scope("providers")
@api_meta(
    summary="Refresh owner Artificial Analysis scores",
    tags=["models"],
    scope="providers",
)
def refresh_aa_scores():
    try:
        block = _read_block(force=True)
        audit_log("model_intelligence_aa_refresh", status=block.get("status"))
        return api_ok(aa=block)
    except LookupError:
        return api_not_found("Model intelligence settings not found")
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_intelligence.aa.refresh")


@api_v1_model_intelligence_bp.route(
    "/api/v1/model-intelligence/openrouter-pricing", methods=["GET"])
@require_scope("providers")
@api_meta(
    summary="Read the OpenRouter pricing cross-check report",
    description=(
        "Projects public OpenRouter list prices onto canonical Models as a "
        "policy-bucketed report (fillable / official-covered / openrouter-free "
        "/ unmatched / drift). Read-only; never writes the authority."
    ),
    tags=["models"],
    scope="providers",
)
def get_openrouter_pricing():
    try:
        boundary = _boundary()
        if boundary is None:
            raise LookupError("Model intelligence settings not found")
        repository = _repository()
        block = pricing_block_for_models(_models(repository, boundary))
        return api_ok(openrouter_pricing=block)
    except LookupError:
        return api_not_found("Model intelligence settings not found")
    except Exception as exc:
        return api_internal_error(
            exc, context="api_v1.model_intelligence.openrouter_pricing.get")


@api_v1_model_intelligence_bp.route(
    "/api/v1/model-intelligence/openrouter-pricing/refresh", methods=["POST"])
@require_scope("providers")
@api_meta(
    summary="Refresh OpenRouter pricing and fill uncovered Model list prices",
    description=(
        "Force-fetches the public OpenRouter catalog, then CAS-writes "
        "list_pricing onto unpriced Models that have no official-direct "
        "coverage. Officially priced Models are never rewritten; all-zero "
        "OpenRouter rows never become a $0 list price."
    ),
    tags=["models"],
    scope="providers",
)
def refresh_openrouter_pricing():
    try:
        boundary = _boundary()
        if boundary is None:
            raise LookupError("Model intelligence settings not found")
        repository = _repository()
        block = refresh_pricing_block(_models(repository, boundary))
        fillable = block.get("fillable") or {}
        applied: list[str] = []
        revision = repository.get(boundary).revision
        if block.get("status") == "ok" and fillable:
            holder: dict[str, list[str]] = {"applied": []}

            def mutation(working: dict[str, Any]) -> dict[str, Any]:
                holder["applied"] = fill_document_list_pricing(working, fillable)
                return working

            committed = repository.mutate(boundary, mutation)
            applied = holder["applied"]
            revision = committed.revision
        audit_log(
            "model_intelligence_openrouter_pricing_refresh",
            status=block.get("status"),
            applied=len(applied),
        )
        return api_ok(
            openrouter_pricing=block, applied=applied, revision=revision)
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), kind=getattr(exc, "kind", "model_routing_error"))
    except LookupError:
        return api_not_found("Model intelligence settings not found")
    except Exception as exc:
        return api_internal_error(
            exc, context="api_v1.model_intelligence.openrouter_pricing.refresh")


@api_v1_model_intelligence_bp.route(
    "/api/v1/model-intelligence/aa/key", methods=["PUT"])
@require_scope("providers")
@api_meta(
    summary="Save or clear the owner Artificial Analysis API key",
    description=(
        "Stores a non-empty key encrypted and owner-scoped; an empty key clears "
        "the saved value. The response contains only redacted key metadata."
    ),
    tags=["models"],
    scope="providers",
)
def put_aa_key():
    boundary = _boundary()
    if boundary is None:
        return api_not_found("Model intelligence settings not found")
    body = parse_body(exact=('api_key',))
    api_key = body.get("api_key")
    if not isinstance(api_key, str):
        return api_bad_request("api_key must be a string", field="api_key")
    normalized = api_key.strip()
    if len(normalized) > _MAX_API_KEY_CHARACTERS:
        return api_bad_request(
            f"api_key must be at most {_MAX_API_KEY_CHARACTERS} characters",
            field="api_key",
        )
    repository = _repository()
    try:
        if normalized:
            repository.put_secret(
                boundary,
                normalized,
                secret_reference=_AA_SECRET_REFERENCE,
            )
            action = "saved"
        else:
            repository.delete_secret(boundary, _AA_SECRET_REFERENCE)
            action = "cleared"
        _remove_legacy_config_key()
        models = _models(repository, boundary)
        effective_key, key_source, key_hint = _effective_key(repository, boundary)
        block = refresh_scores(
            models,
            api_key=effective_key,
            key_source=key_source,
            key_hint=key_hint,
        ) if effective_key else aa_block_for_models(
            models,
            api_key="",
            key_source=None,
            key_hint="",
        )
        audit_log("model_intelligence_aa_key_change", action=action)
        return api_ok(aa=block)
    except ModelRoutingError as exc:
        return api_bad_request(str(exc), kind=getattr(exc, "kind", "credential_invalid"))
    except Exception as exc:
        return api_internal_error(exc, context="api_v1.model_intelligence.aa.key")
