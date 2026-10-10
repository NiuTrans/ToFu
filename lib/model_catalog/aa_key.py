"""Owner Artificial Analysis API key resolution.

Single authority for the three key sources every AA consumer (the
model-intelligence routes and the scheduled auto-update pass) must honour in
order: the encrypted owner-scoped settings secret, the retired personal
``server_config.json`` key (single-user installs only), and the
``TOFU_AA_API_KEY`` environment variable.  This module never returns plaintext
beyond the caller that already owns the boundary; responses surface only the
redacted source/hint produced here.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from lib.auth_mode import is_multi_user
from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.model_routing import ModelRoutingError
from lib.secret_envelope import secret_hint

AA_SECRET_REFERENCE = "settings_model_intelligence_aa"
AA_KEY_ENV = "TOFU_AA_API_KEY"
LEGACY_CONFIG_KEY = "aa_api_key"


def _legacy_config_key() -> str:
    """Read the former personal-install key only outside multi-user mode."""
    if is_multi_user():
        return ""
    config = read_json(config_path("server_config.json"), default={})
    if not isinstance(config, Mapping):
        return ""
    return str(config.get(LEGACY_CONFIG_KEY) or "").strip()


def clear_legacy_config_key() -> None:
    """Remove the retired plaintext key once an owner-scoped secret exists."""
    if is_multi_user():
        return

    def mutate(current):
        updated = dict(current) if isinstance(current, Mapping) else {}
        updated.pop(LEGACY_CONFIG_KEY, None)
        return updated

    update_json_atomic(config_path("server_config.json"), mutate, default={})


def resolve_effective_key(repository, boundary) -> tuple[str, str | None, str]:
    """Return ``(api_key, key_source, key_hint)`` for one owner boundary."""
    try:
        stored = repository.resolve_secret(boundary, AA_SECRET_REFERENCE)
    except ModelRoutingError as exc:
        if getattr(exc, "kind", "") != "credential_secret_missing":
            raise
        stored = ""
    if stored:
        return stored, "settings", secret_hint(stored)
    legacy = _legacy_config_key()
    if legacy:
        return legacy, "legacy_config", secret_hint(legacy)
    environment = str(os.environ.get(AA_KEY_ENV) or "").strip()
    if environment:
        return environment, "env", secret_hint(environment)
    return "", None, ""


__all__ = [
    "AA_KEY_ENV",
    "AA_SECRET_REFERENCE",
    "LEGACY_CONFIG_KEY",
    "clear_legacy_config_key",
    "resolve_effective_key",
]
