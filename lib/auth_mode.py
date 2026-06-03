"""lib/auth_mode.py — Single source of truth for the auth gate's mode.

Tofu has three documented operating modes; the auth middleware in
``routes/api_v1/auth.py`` consults this module on every request to
decide what credentials (if any) to require.

Modes
-----
``open`` (default):
    No credential is required. Every request gets a synthetic
    local-admin :class:`~lib.api_keys.AuthContext` so downstream
    ``require_scope(...)`` decorators, idempotency keying, and rate
    limit accounting keep working uniformly. Public-by-default; bind
    host should stay loopback (``127.0.0.1``) unless the operator
    explicitly accepts the risk. Suitable for personal use and
    frontend-only deployments where users cannot be expected to
    paste a token into a URL.

``private``:
    The historical Bearer-token gate. Every non-public route returns
    401 without a valid ``Authorization: Bearer …``, ``x-api-key``,
    or ``tofu_session`` cookie. First-boot bootstrap mints one
    ``tofu_admin_…`` key. Suitable for a single operator using their
    own instance from multiple devices, or any networked deployment.

``multi-user``:
    Same gate as ``private`` (every request needs a valid key) but
    semantically signals that the Settings → API Keys UI is the
    operator-facing administration surface for multiple end-users.
    No behavioral difference today; gives future RBAC/SSO modes a
    clean slot to land in.

Persistence
-----------
The active mode lives at ``data/config/auth.json``::

    {"version": 1, "mode": "open", "since": 1701000000, "set_by": "bootstrap"}

Mutations go through :func:`set_mode` which atomically writes the file,
emits an audit log, and clears the in-memory cache. Reads through
:func:`get_mode` are O(1) after the first hit.

Override priority (highest first):
    1. ``TOFU_AUTH_MODE`` env var (open|private|multi-user) — wins
       over the file. Useful for CI / containers / tests that want
       to lock the mode without editing the on-disk config.
    2. ``data/config/auth.json``.
    3. Default = ``open``.

Switching direction
-------------------
Tightening the gate (``open → private`` / ``open → multi-user``) is
always safe and requires no credential.

Loosening the gate (``private → open`` or ``multi-user → open``) is a
privileged operation; the route handler at ``/api/v1/auth/mode`` gates
it behind the ``admin`` scope. This module does not enforce that —
callers are responsible for the policy check.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import audit_log, get_logger

logger = get_logger(__name__)

_STORE_PATH = config_path('auth.json')
_STORE_VERSION = 1

# Closed enum. New modes must be added here AND to the documentation
# above; ``set_mode`` rejects anything not in this set.
MODE_OPEN = 'open'
MODE_PRIVATE = 'private'
MODE_MULTI_USER = 'multi-user'
ALL_MODES = frozenset({MODE_OPEN, MODE_PRIVATE, MODE_MULTI_USER})

# Default mode when the file is missing AND the env var is unset. Tofu
# ships open-by-default so a fresh ``python server.py`` Just Works for
# the local user without any token plumbing.
_DEFAULT_MODE = MODE_OPEN

_lock = threading.RLock()
_cached: Optional[dict] = None


@dataclass(frozen=True)
class AuthModeState:
    """Resolved state, returned by :func:`get_state`."""
    mode: str
    since: float
    set_by: str
    source: str  # 'env' | 'file' | 'default'


def _normalise(mode: object) -> Optional[str]:
    if not isinstance(mode, str):
        return None
    m = mode.strip().lower()
    if m in ALL_MODES:
        return m
    # Friendly aliases.
    if m in ('multi', 'multiuser', 'multi_user'):
        return MODE_MULTI_USER
    if m in ('public', 'none', 'off', 'disabled', 'no-auth', 'noauth'):
        return MODE_OPEN
    if m in ('on', 'enabled', 'token', 'bearer'):
        return MODE_PRIVATE
    return None


def _read_file() -> Optional[dict]:
    raw = read_json(_STORE_PATH, default=None)
    if not isinstance(raw, dict):
        return None
    mode = _normalise(raw.get('mode'))
    if mode is None:
        return None
    return {
        'mode': mode,
        'since': float(raw.get('since') or 0.0),
        'set_by': str(raw.get('set_by') or '')[:80],
    }


def _resolve() -> AuthModeState:
    env = _normalise(os.environ.get('TOFU_AUTH_MODE', ''))
    if env is not None:
        return AuthModeState(mode=env, since=0.0, set_by='env',
                             source='env')
    row = _read_file()
    if row is not None:
        return AuthModeState(mode=row['mode'], since=row['since'],
                             set_by=row['set_by'], source='file')
    return AuthModeState(mode=_DEFAULT_MODE, since=0.0,
                         set_by='default', source='default')


def get_state(*, refresh: bool = False) -> AuthModeState:
    """Return the resolved :class:`AuthModeState`.

    The result is cached in-process; pass ``refresh=True`` to bypass.
    Mutations through :func:`set_mode` clear the cache automatically.
    """
    global _cached
    with _lock:
        if refresh or _cached is None:
            state = _resolve()
            _cached = {
                'mode': state.mode, 'since': state.since,
                'set_by': state.set_by, 'source': state.source,
            }
            return state
        return AuthModeState(**_cached)


def get_mode() -> str:
    """Return the active mode string. Hot-path; cached."""
    return get_state().mode


def is_open() -> bool:
    return get_mode() == MODE_OPEN


def is_private() -> bool:
    return get_mode() == MODE_PRIVATE


def is_multi_user() -> bool:
    return get_mode() == MODE_MULTI_USER


def requires_credential() -> bool:
    """True iff the current mode rejects unauthenticated requests."""
    return get_mode() in (MODE_PRIVATE, MODE_MULTI_USER)


def env_overrides_file() -> bool:
    """True iff ``TOFU_AUTH_MODE`` is set; UI cannot override an env."""
    return get_state().source == 'env'


def set_mode(new_mode: str, *, set_by: str = '') -> AuthModeState:
    """Persist a new mode. Caller is responsible for permission checks.

    Raises ``ValueError`` on an unknown mode, ``RuntimeError`` if the
    mode is currently locked by ``TOFU_AUTH_MODE``.
    """
    canon = _normalise(new_mode)
    if canon is None:
        raise ValueError(f'Unknown auth mode: {new_mode!r}')
    if env_overrides_file():
        raise RuntimeError('TOFU_AUTH_MODE is set; remove the env var '
                           'before changing the mode at runtime.')
    now = time.time()
    payload = {
        'version': _STORE_VERSION,
        'mode': canon,
        'since': now,
        'set_by': str(set_by or '')[:80],
    }
    def _mutator(_):
        return payload
    update_json_atomic(_STORE_PATH, _mutator, default=payload)
    with _lock:
        global _cached
        _cached = None
    audit_log('auth_mode_changed', mode=canon, set_by=payload['set_by'])
    logger.warning('[AuthMode] mode changed to %s by %r', canon,
                   payload['set_by'] or '<unspecified>')
    return get_state(refresh=True)


def reset_for_tests() -> None:
    """Clear the in-process cache (and remove the file). Tests only."""
    with _lock:
        global _cached
        _cached = None
    try:
        os.remove(_STORE_PATH)
    except FileNotFoundError as e:
        logger.debug('[AuthMode] reset_for_tests: store already absent: %s', e)
    except OSError as e:
        logger.debug('[AuthMode] reset_for_tests: %s', e)


__all__ = [
    'AuthModeState',
    'ALL_MODES',
    'MODE_OPEN', 'MODE_PRIVATE', 'MODE_MULTI_USER',
    'get_state', 'get_mode',
    'is_open', 'is_private', 'is_multi_user',
    'requires_credential', 'env_overrides_file',
    'set_mode', 'reset_for_tests',
]
