"""Bearer credentials and request authentication contexts.

Credential rows live only in the Storage Sidecar.  This package generates
one-time plaintext tokens, calls semantic identity operations, and translates
authenticated rows into :class:`AuthContext`; it owns no cache or shadow file.
"""

from __future__ import annotations

from lib.config_dir import config_path

_FIRST_RUN_TOKEN_FILE = config_path('.first_run_token')

from lib.api_keys._context import (  # noqa: E402
    _ADMIN_SCOPE,
    ALL_SCOPES,
    AuthContext,
    local_admin_context,
    _normalise_scopes,
)
from lib.api_keys._crud import (  # noqa: E402
    _UPDATABLE,
    create_first_owner_key,
    create_key,
    get_key_by_id,
    list_keys,
    revoke_key,
    update_key,
)
from lib.api_keys._validate import identify_known_token, validate_token  # noqa: E402
from lib.api_keys._firstrun import (  # noqa: E402
    _clear_first_run_token,
    _purge_stale_first_run_token,
    bootstrap_personal_key,
    has_any_key,
)

__all__ = [
    'ALL_SCOPES', 'AuthContext', 'bootstrap_personal_key', 'create_key',
    'get_key_by_id', 'has_any_key', 'list_keys', 'local_admin_context',
    'identify_known_token', 'revoke_key', 'update_key', 'validate_token',
]
