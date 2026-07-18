"""lib/api_keys/ — Bearer-token API key auth.

Single source of truth for who is authorised to call any non-public
route. Personal browsing, headless SDK clients, OpenAI/Anthropic-compat
callers, and the local UI all authenticate the same way: a token
minted here, validated by ``validate_token``, and carried in any of
``Authorization: Bearer …`` / ``x-api-key`` / the ``tofu_session``
cookie.

On first boot the server calls :func:`bootstrap_personal_key` which
mints one ``tofu_admin_…`` key when the store is empty, prints its
plaintext to stderr exactly once, and persists only its hash. That
key backs both the local UI (cookie) and personal SDK use
(env / config file).

``TUNNEL_TOKEN`` is preserved as a deprecated back-compat shim — when
set it acts as a synthetic admin credential so existing deployments
don't break, but new code should not reach for it.

Package layout
--------------
This module is a **facade**: it re-exports every public (and every
test-facing private) symbol so historic call sites keep working
byte-identically::

    from lib.api_keys import validate_token, create_key, ALL_SCOPES
    from lib import api_keys; api_keys.bootstrap_personal_key()

Sub-modules:
  _store     — the single ``_cache`` / ``_cache_lock`` plus load/persist
  _context   — AuthContext, ALL_SCOPES, local_admin_context, scope norm
  _crud       — list/get/create/revoke/update/touch key
  _validate  — validate_token
  _firstrun  — bootstrap_personal_key + first-run token lifecycle

Rebindable settings (``_STORE_PATH``, ``_STORE_VERSION``,
``_cache_loaded``, ``_FIRST_RUN_TOKEN_FILE``) are defined HERE, on the
facade, because tests patch/reassign them at this path
(``patch('lib.api_keys._STORE_PATH', …)``,
``api_keys._cache_loaded = False``). The store/first-run helpers resolve
these names through this module at call time, so a test's override is
always honoured. ``_cache`` / ``_cache_lock`` are *not* redefined here —
they are imported by reference from :mod:`lib.api_keys._store` so there
is exactly ONE of each in the process.

Storage
-------
``data/config/api_keys.json`` via ``lib.json_store`` (atomic, locked):

    {
      "version": 1,
      "keys": [
        {
          "id":            "k_a3f2c1",
          "name":          "build-bot",
          "prefix":        "tofu_live_a3f2c1",   # public, shown in UI
          "secret_hash":   "<sha256-of-full-token>",
          "scopes":        ["chat","tasks","files"],
          "rate_limit_rpm":  60,
          "rate_limit_tpd":  1000000,
          "created_at":    1701000000.0,
          "last_used_at":  1701002345.0,
          "expires_at":    null,
          "disabled":      false,
          "metadata":      {"created_by":"alice","note":"CI runner"}
        }
      ]
    }

Tokens are shaped ``tofu_live_<32hex>`` or ``tofu_admin_<32hex>``. Only
``prefix`` is stored in the clear; the full token is shown ONCE at
creation, then only its SHA-256 hash is persisted.

Public API
----------
  list_keys()                         → list[dict] (no secrets)
  get_key_by_id(key_id)               → dict | None
  validate_token(token)               → AuthContext | None
  create_key(name, scopes, ...)       → (dict, plaintext_token)
  revoke_key(key_id)                  → bool
  update_key(key_id, **fields)        → bool
  touch_key(key_id)                   → records last_used_at
  ALL_SCOPES                          → frozenset of scope strings
  AuthContext                         → result of validate_token()
"""

from __future__ import annotations

from lib.config_dir import config_path
from lib.log import get_logger

logger = get_logger(__name__)

# ── Rebindable settings (owned by the facade; patched by tests) ────
_STORE_PATH = config_path('api_keys.json')
_STORE_VERSION = 1

# Loaded-once flag. Reassigned (``= False``) by tests and by
# ``_store._ensure_loaded``; kept here so both see the same binding.
_cache_loaded = False

# Plaintext is dropped here once on first boot so the user (or a
# launcher script) can recover it without grepping the boot log.
# 0600 perms, lives next to api_keys.json so a project-clone never
# inherits the host's key (export.py excludes data/config/* anyway).
_FIRST_RUN_TOKEN_FILE = config_path('.first_run_token')

# ── Shared mutable cache (imported BY REFERENCE — exactly one each) ─
from lib.api_keys._store import (  # noqa: E402
    _cache,
    _cache_lock,
    _ensure_loaded,
    _hash_token,
    _persist,
    _public_view,
)
from lib.api_keys._context import (  # noqa: E402
    _ADMIN_SCOPE,
    ALL_SCOPES,
    AuthContext,
    local_admin_context,
    _normalise_scopes,
)
from lib.api_keys._crud import (  # noqa: E402
    _UPDATABLE,
    create_key,
    get_key_by_id,
    list_keys,
    revoke_key,
    touch_key,
    update_key,
)
from lib.api_keys._validate import validate_token  # noqa: E402
from lib.api_keys._firstrun import (  # noqa: E402
    _clear_first_run_token,
    _purge_stale_first_run_token,
    bootstrap_personal_key,
    has_any_key,
)

__all__ = [
    'AuthContext', 'ALL_SCOPES', 'local_admin_context',
    'list_keys', 'get_key_by_id', 'validate_token',
    'create_key', 'revoke_key', 'update_key', 'touch_key',
    'has_any_key', 'bootstrap_personal_key',
    '_purge_stale_first_run_token',
]
