"""lib/memory/user_profile/_paths.py — scope + on-disk path resolution.

Pure filesystem-path plumbing for the personal-preference profile: resolve
the storage *scope* from a request ``AuthContext``, sanitise it into a safe
directory name, and map ``(scope) -> absolute file path`` for both the profile
body and the pending-proposals file. No I/O of the profile content itself lives
here (see ``._io`` / ``._pending``).
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


def resolve_profile_scope(ctx) -> str:
    """Resolve the profile storage scope from a request ``AuthContext``.

    The rule is deliberately minimal: the scope is the authenticated
    positive ``owner_user_id`` assigned at the authentication boundary
    (``_mint_session_key`` in ``routes/api_v1/users.py``). Open mode
    (synthetic local-admin) and private mode (a Bearer key with no tenant
    binding) both leave ``user_id`` empty, so they resolve to ``''`` — the
    single shared global profile, exactly the personal-install semantic.

    Fail-safe: anything we can't read an explicit ``user_id`` off of yields
    ``''`` (the global file), never a half-built scope.
    """
    try:
        owner_user_id = getattr(ctx, 'owner_user_id', None)
        return str(owner_user_id) if owner_user_id is not None else ''
    except Exception as e:
        logger.debug('[UserProfile] scope resolve failed: %s', e)
        return ''


def _server_memories_dir() -> str:
    """Return ``<data>/memories`` (parent of the global store).

    Resolved fresh each call (mirrors ``storage._server_data_dir``) so tests
    can redirect via ``$TOFU_DATA_DIR``.
    """
    from lib.memory.storage import _server_data_dir
    return os.path.join(_server_data_dir(), 'memories')


def _sanitize_scope(scope: str) -> str:
    """Turn an identity scope (a multi-user ``user_id``) into a safe dir name.

    Returns ``''`` for an empty/falsy scope — the signal to use the single
    global file (open / private mode: one operator, no tenant binding). For a
    real scope we combine a charset-restricted prefix (readability) with a
    SHA-256 suffix (collision-resistance + traversal-proofing), so a hostile
    ``user_id`` like ``../../etc`` can never escape ``<data>/memories/profiles``.
    """
    import hashlib
    import re
    s = (scope or '').strip()
    if not s:
        return ''
    digest = hashlib.sha256(s.encode('utf-8')).hexdigest()[:16]
    safe = re.sub(r'[^A-Za-z0-9_-]', '', s)[:32]
    return f'{safe}_{digest}' if safe else digest


def profile_path(scope: str = '') -> str:
    """Absolute path to the personal-preference profile file for *scope*.

    ``scope=''`` (the default) → the single global file
    ``<data>/memories/.tofu_user_profile.md`` — the personal-install / open /
    private-mode profile. This keeps every existing deployment BYTE-IDENTICAL:
    open mode and private mode never set a ``user_id``, so they always land
    here, and there is no migration.

    A non-empty *scope* (a multi-user tenant ``user_id``) → a per-tenant file
    ``<data>/memories/profiles/<sanitized-scope>/.tofu_user_profile.md`` so one
    tenant's profile is never injected into another's prompt. The ``.tofu``
    prefix is preserved on the filename, and rooting under ``data/`` keeps the
    profile project-independent (follows the user across projects).
    """
    from lib.agent_artifacts import USER_PROFILE_FILE
    base = _server_memories_dir()
    sid = _sanitize_scope(scope)
    if not sid:
        return os.path.join(base, USER_PROFILE_FILE)
    return os.path.join(base, 'profiles', sid, USER_PROFILE_FILE)


def context_path(scope: str = '') -> str:
    """Absolute path to the structured, per-user context document."""
    from lib.agent_artifacts import USER_CONTEXT_FILE
    base = _server_memories_dir()
    sid = _sanitize_scope(scope)
    if not sid:
        return os.path.join(base, USER_CONTEXT_FILE)
    return os.path.join(base, 'profiles', sid, USER_CONTEXT_FILE)


def context_changes_path(scope: str = '') -> str:
    """Absolute path to the bounded undo log for assistant-learned context."""
    from lib.agent_artifacts import USER_CONTEXT_CHANGES_FILE
    base = _server_memories_dir()
    sid = _sanitize_scope(scope)
    if not sid:
        return os.path.join(base, USER_CONTEXT_CHANGES_FILE)
    return os.path.join(base, 'profiles', sid, USER_CONTEXT_CHANGES_FILE)


def _pending_path(scope: str = '') -> str:
    """Path of the propose/confirm queue for one profile scope.

    Pending preferences contain the same private user facts as the confirmed
    context.  They must follow the context's tenant boundary instead of using
    one process-global file for every authenticated user.
    """
    from lib.agent_artifacts import USER_PROFILE_PENDING_FILE
    base = _server_memories_dir()
    sid = _sanitize_scope(scope)
    if not sid:
        return os.path.join(base, USER_PROFILE_PENDING_FILE)
    return os.path.join(base, 'profiles', sid, USER_PROFILE_PENDING_FILE)
