"""Owner-scoped Artificial Analysis settings and score projection.

This boundary enriches canonical Creator/Model facts only.  It does not expose
or reconstruct the removed model-catalog authority, and it never reads model
supply, aliases, deployments, credentials belonging to providers, or routes.
The AA API key is stored encrypted through an owner-aware repository seam and
is never returned to the browser.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from quart import Blueprint

from lib.api_response import api_bad_request, api_internal_error, api_not_found, api_ok
from lib.auth_mode import is_multi_user
from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log
from lib.model_catalog.aa import aa_block_for_models, refresh_scores
from lib.model_routing import ModelRoutingError, ModelRoutingRepository, OwnerBoundary
from lib.openapi import api_meta
from lib.request_parser import parse_body
from lib.secret_envelope import secret_hint

from .auth import current_auth, require_scope


api_v1_model_intelligence_bp = Blueprint("api_v1_model_intelligence", __name__)

_AA_SECRET_REFERENCE = "settings_model_intelligence_aa"
_AA_KEY_ENV = "TOFU_AA_API_KEY"
_LEGACY_CONFIG_KEY = "aa_api_key"
_MAX_API_KEY_CHARACTERS = 256


def _boundary() -> OwnerBoundary | None:
    context = current_auth()
    if context is None or context.owner_user_id is None:
        return None
    return OwnerBoundary.create(context.owner_user_id, context.tenant_id)


def _repository() -> ModelRoutingRepository:
    """Encrypted owner-secret adapter; replaceable in tests and later stores."""
    return ModelRoutingRepository()


def _legacy_config_key() -> str:
    """Read the former personal-install key only outside multi-user mode."""
    if is_multi_user():
        return ""
    config = read_json(config_path("server_config.json"), default={})
    if not isinstance(config, Mapping):
        return ""
    return str(config.get(_LEGACY_CONFIG_KEY) or "").strip()


def _remove_legacy_config_key() -> None:
    if is_multi_user():
        return

    def mutate(current: Any) -> dict[str, Any]:
        updated = dict(current) if isinstance(current, Mapping) else {}
        updated.pop(_LEGACY_CONFIG_KEY, None)
        return updated

    update_json_atomic(config_path("server_config.json"), mutate, default={})


def _effective_key(
    repository: ModelRoutingRepository,
    boundary: OwnerBoundary,
) -> tuple[str, str | None, str]:
    try:
        stored = repository.resolve_secret(boundary, _AA_SECRET_REFERENCE)
    except ModelRoutingError as exc:
        if getattr(exc, "kind", "") != "credential_secret_missing":
            raise
        stored = ""
    if stored:
        return stored, "settings", secret_hint(stored)
    legacy = _legacy_config_key()
    if legacy:
        return legacy, "legacy_config", secret_hint(legacy)
    environment = str(os.environ.get(_AA_KEY_ENV) or "").strip()
    if environment:
        return environment, "env", secret_hint(environment)
    return "", None, ""


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
    body = parse_body()
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
