"""Legacy owner-scoped BYO repository retained for one-way v2 migration.

The Storage Sidecar is the sole provider authority.  Rows are keyed by an
explicit numeric repository owner plus the tenant evolution seam; bearer-key
IDs are credentials, never ownership identities.  Upstream API keys are stored
only as authenticated ciphertext and are decrypted only by :func:`get_provider`
for an outbound request.

Migration/compatibility entry points
------------------------------------
``list_providers`` and ``get_public`` return redacted HTTP-safe documents.
``get_provider`` returns the internal document with plaintext ``api_key``.
``create_provider`` / ``update_provider`` / ``delete_provider`` are atomic
Sidecar commands. ``resolve_model_string`` implements the retired
``model@prov_id`` grammar only for legacy migration tests and consumers; native
HTTP routes use :mod:`lib.model_routing` exclusively.
"""

from __future__ import annotations

from dataclasses import dataclass
import secrets
import time
from typing import Optional

from lib.identity import require_user_id
from lib.log import audit_log, get_logger
from lib.provider_headers import sanitise_extra_headers
from lib.secret_envelope import open_secret, seal_secret, secret_hint
from lib.storage.service import get_storage_client


logger = get_logger(__name__)

__all__ = [
    "ResolvedModel",
    "create_provider",
    "delete_provider",
    "get_provider",
    "get_public",
    "list_providers",
    "redact",
    "resolve_model_string",
    "sanitise_extra_headers",
    "touch_provider",
    "update_provider",
]

_MAX_MODELS_PER_PROVIDER = 64
_MAX_API_KEY_BYTES = 8192
_SECRET_PURPOSE = "byo-provider-api-key"
_UPDATABLE = frozenset({
    "name",
    "base_url",
    "api_key",
    "models",
    "extra_headers",
    "thinking_format",
    "disabled",
})


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """A model alias plus its owner-authorized provider, when suffixed."""

    model_id: str
    provider: Optional[dict]


def _boundary(owner_user_id: int, tenant_id: str | None) -> dict[str, object]:
    return {
        "owner_user_id": require_user_id(
            owner_user_id, context="BYO provider owner"),
        "tenant_id": str(tenant_id or "").strip(),
    }


def _validate_models(models) -> list[dict]:
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    if len(models) > _MAX_MODELS_PER_PROVIDER:
        raise ValueError(
            f"too many models (max {_MAX_MODELS_PER_PROVIDER})")
    from lib.model_registration import normalize_model_entry

    normalized: list[dict] = []
    for index, model in enumerate(models):
        if not isinstance(model, dict):
            raise ValueError(f"models[{index}] must be an object")
        try:
            normalized.append(
                normalize_model_entry(model, reject_legacy_cost=True))
        except ValueError as exc:
            raise ValueError(f"models[{index}]: {exc}") from exc
    return normalized


def _validate_base_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        raise ValueError("base_url is required")
    if len(url) > 500:
        raise ValueError("base_url exceeds 500 characters")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError("base_url must start with http:// or https://")
    from lib.byo_egress import EgressDenied, validate_egress_url

    try:
        validate_egress_url(url)
    except EgressDenied as exc:
        raise ValueError(str(exc)) from exc
    return url


def _validate_thinking_format(value) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise ValueError("thinking_format must be a string")
    from lib.llm_dispatch.provider_registry import is_valid_thinking_format
    from lib.llm_dispatch.slot import THINKING_FORMATS

    normalized = value.strip()
    if not is_valid_thinking_format(normalized):
        raise ValueError(
            "thinking_format=%r is not one of %s (nor a registered "
            "tofu.providers plugin dialect)" % (
                value, sorted(THINKING_FORMATS)))
    return normalized


def _validate_api_key(value: str) -> str:
    normalized = str(value or "").strip()
    if len(normalized.encode("utf-8")) > _MAX_API_KEY_BYTES:
        raise ValueError(f"api_key exceeds {_MAX_API_KEY_BYTES} bytes")
    return normalized


def _register_models(provider_id: str, models: list[dict]) -> None:
    from lib.model_registration import clear_provider_models, register_model

    clear_provider_models(provider_id)
    for model in models:
        register_model(model, provider_id=provider_id)


def _internal_document(row: dict) -> dict:
    document = dict(row)
    ciphertext = str(document.pop("api_key_ciphertext", "") or "")
    document["api_key"] = (
        open_secret(
            ciphertext,
            purpose=_SECRET_PURPOSE,
            owner_user_id=int(document["owner_user_id"]),
            record_id=str(document["id"]),
        )
        if ciphertext
        else ""
    )
    return document


def redact(row: dict) -> dict:
    """Return an idempotent, HTTP-safe provider projection."""
    public = dict(row)
    raw = str(public.pop("api_key", "") or "")
    public.pop("api_key_ciphertext", None)
    public.pop("owner_user_id", None)
    public.pop("tenant_id", None)
    if raw:
        public["key_hint"] = secret_hint(raw)
    else:
        public.setdefault("key_hint", "")
    return public


def list_providers(
    owner_user_id: int, *, tenant_id: str | None = None,
) -> list[dict]:
    """Return newest-first redacted providers for one owner boundary."""
    rows = get_storage_client().query(
        "provider.list", _boundary(owner_user_id, tenant_id))
    return [redact(dict(row)) for row in rows]


def get_provider(
    provider_id: str,
    owner_user_id: int,
    *,
    tenant_id: str | None = None,
) -> Optional[dict]:
    """Return one internal provider document, including plaintext api_key."""
    row = get_storage_client().query(
        "provider.get",
        {
            **_boundary(owner_user_id, tenant_id),
            "provider_id": str(provider_id or "").strip(),
        },
    )
    if row is None:
        return None
    document = _internal_document(dict(row))
    _register_models(document["id"], document.get("models") or [])
    return document


def get_public(
    provider_id: str,
    owner_user_id: int,
    *,
    tenant_id: str | None = None,
) -> Optional[dict]:
    row = get_provider(provider_id, owner_user_id, tenant_id=tenant_id)
    return None if row is None else redact(row)


def create_provider(
    *,
    owner_user_id: int,
    name: str,
    base_url: str,
    api_key: str,
    models: list,
    extra_headers: Optional[dict] = None,
    thinking_format: str = "",
    tenant_id: str | None = None,
) -> dict:
    """Validate, encrypt, and atomically register one provider."""
    boundary = _boundary(owner_user_id, tenant_id)
    normalized_name = str(name or "").strip()
    if not normalized_name:
        raise ValueError("name is required")
    if len(normalized_name) > 80:
        raise ValueError("name exceeds 80 characters")
    normalized_url = _validate_base_url(base_url)
    normalized_key = _validate_api_key(api_key)
    normalized_models = _validate_models(models)
    normalized_headers, header_error = sanitise_extra_headers(extra_headers)
    if header_error:
        raise ValueError(header_error)
    normalized_thinking = _validate_thinking_format(thinking_format)
    provider_id = "prov_" + secrets.token_hex(8)
    ciphertext = (
        seal_secret(
            normalized_key,
            purpose=_SECRET_PURPOSE,
            owner_user_id=int(boundary["owner_user_id"]),
            record_id=provider_id,
        )
        if normalized_key
        else ""
    )
    row = get_storage_client(write=True).command(
        "provider.create",
        {
            **boundary,
            "provider_id": provider_id,
            "name": normalized_name,
            "base_url": normalized_url,
            "api_key_ciphertext": ciphertext,
            "key_hint": secret_hint(normalized_key),
            "models": normalized_models,
            "extra_headers": normalized_headers,
            "thinking_format": normalized_thinking,
            "created_at": time.time(),
        },
        f"provider.create:{provider_id}",
    )
    if row is None:
        raise RuntimeError("provider creation did not return a row")
    document = _internal_document(dict(row))
    _register_models(provider_id, normalized_models)
    audit_log(
        "byo_provider_created",
        provider_id=provider_id,
        owner_user_id=boundary["owner_user_id"],
        tenant_id=boundary["tenant_id"],
        base_url=normalized_url,
        model_count=len(normalized_models),
    )
    return document


def update_provider(
    provider_id: str,
    owner_user_id: int,
    *,
    tenant_id: str | None = None,
    **fields,
) -> bool:
    """Validate and atomically update an owned provider; reject unknown fields."""
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(
            f"unknown provider update fields: {', '.join(sorted(unknown))}")
    boundary = _boundary(owner_user_id, tenant_id)
    updates: dict[str, object] = {}
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise ValueError("name is required")
        if len(name) > 80:
            raise ValueError("name exceeds 80 characters")
        updates["name"] = name
    if "base_url" in fields:
        updates["base_url"] = _validate_base_url(fields["base_url"])
    if "api_key" in fields:
        key = _validate_api_key(fields["api_key"])
        updates["api_key_ciphertext"] = (
            seal_secret(
                key,
                purpose=_SECRET_PURPOSE,
                owner_user_id=int(boundary["owner_user_id"]),
                record_id=provider_id,
            )
            if key
            else ""
        )
        updates["key_hint"] = secret_hint(key)
    if "models" in fields:
        updates["models"] = _validate_models(fields["models"])
    if "extra_headers" in fields:
        headers, error = sanitise_extra_headers(fields["extra_headers"])
        if error:
            raise ValueError(error)
        updates["extra_headers"] = headers
    if "thinking_format" in fields:
        updates["thinking_format"] = _validate_thinking_format(
            fields["thinking_format"])
    if "disabled" in fields:
        if not isinstance(fields["disabled"], bool):
            raise ValueError("disabled must be a boolean")
        updates["disabled"] = fields["disabled"]
    if not updates:
        return get_provider(
            provider_id, owner_user_id, tenant_id=tenant_id) is not None
    row = get_storage_client(write=True).command(
        "provider.update",
        {
            **boundary,
            "provider_id": str(provider_id or "").strip(),
            "updates": updates,
            "updated_at": time.time(),
        },
        f"provider.update:{provider_id}:{secrets.token_hex(8)}",
    )
    if row is None:
        return False
    if "models" in updates:
        _register_models(provider_id, updates["models"])
    audit_log(
        "byo_provider_updated",
        provider_id=provider_id,
        owner_user_id=boundary["owner_user_id"],
        fields=sorted(fields),
    )
    return True


def delete_provider(
    provider_id: str,
    owner_user_id: int,
    *,
    tenant_id: str | None = None,
) -> bool:
    boundary = _boundary(owner_user_id, tenant_id)
    result = get_storage_client(write=True).command(
        "provider.delete",
        {
            **boundary,
            "provider_id": str(provider_id or "").strip(),
        },
        f"provider.delete:{provider_id}:{secrets.token_hex(8)}",
    )
    deleted = bool(result and result.get("deleted"))
    if deleted:
        from lib.model_registration import clear_provider_models

        clear_provider_models(provider_id)
        audit_log(
            "byo_provider_deleted",
            provider_id=provider_id,
            owner_user_id=boundary["owner_user_id"],
        )
    return deleted


def touch_provider(
    provider_id: str,
    owner_user_id: int,
    *,
    tenant_id: str | None = None,
) -> None:
    if not provider_id:
        return
    get_storage_client(write=True).command(
        "provider.touch",
        {
            **_boundary(owner_user_id, tenant_id),
            "provider_id": provider_id,
            "used_at": time.time(),
        },
        None,
    )


def resolve_model_string(
    model: str,
    owner_user_id: int,
    *,
    tenant_id: str | None = None,
) -> Optional[ResolvedModel]:
    """Resolve ``name@prov_id`` without allowing credential-key ownership."""
    if not model or "@" not in model:
        return ResolvedModel(model_id=str(model or "").strip(), provider=None)
    name, _, suffix = model.rpartition("@")
    name = name.strip()
    suffix = suffix.strip()
    if not suffix.startswith("prov_"):
        return ResolvedModel(model_id=model.strip(), provider=None)
    if not name:
        return None
    row = get_provider(suffix, owner_user_id, tenant_id=tenant_id)
    if row is None or row.get("disabled"):
        return None
    return ResolvedModel(model_id=name, provider=row)
