"""Owner-bound reference to a Codex-managed login; no refresh-token import.

Discovery runs at subscription reconciliation, not passive token reads. Local
reads follow Codex atomically replaced files. Desktop reads use an addressed,
permission-gated command and an expiring memory-only access-token snapshot.
Token_store persists only the source/account reference and logout tombstone.
"""
from __future__ import annotations

import logging
import math
import os
import time

from lib.log import get_logger, log_event
from lib.identity import PERSONAL_USER_ID, require_user_id
from lib.ttl_cache import TTLCache
from lib.weak_lock_pool import WeakLockPool
import threading
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)
_TRANSIENT_READ_ISSUES = frozenset({
    'codex_command_timeout', 'codex_command_unavailable',
    'desktop_login_stale', 'desktop_login_unavailable', 'desktop_disconnected',
})

_REMOTE_TTL = 30
_remote = TTLCache(ttl=_REMOTE_TTL, max_size=resolve_resource_budget(
    'TOFU_MAX_INFLIGHT_TASKS', maximum=1024), name='codex_shared_access')
_locks = WeakLockPool(threading.Lock)


def local_owner(owner_user_id=None) -> int | None:
    """The existing process OAuth store belongs only to personal deployment.

    TODO(enterprise): inject an owner-scoped OAuth store before allowing
    shared login in distributed deployments; never borrow the host's identity.
    """
    from runtime_guards import load_deployment_configuration
    if os.environ.get('TOFU_CODEX_SHARED_LOGIN', '1') == '0':
        return None
    if load_deployment_configuration().mode != 'personal':
        return None
    owner = PERSONAL_USER_ID if owner_user_id is None else require_user_id(
        owner_user_id, context='Codex shared login')
    return owner if owner == PERSONAL_USER_ID else None


def _agents(owner: int) -> list[dict]:
    from lib.desktop.bridge import list_agents
    return [agent for agent in list_agents(user_id=str(owner))
            if agent.get('online') and agent.get('user_id') == str(owner)
            and (agent.get('capabilities') or {}).get('codex_login') == 1]


def _snapshot(value, account: str = '') -> dict:
    """Validate a desktop result without trusting its shape or logging it."""
    if not isinstance(value, dict):
        return {'shared_login_issue': 'desktop_login_unavailable'}
    token = value.get('access_token')
    identity = value.get('account_id')
    expire = value.get('expire')
    if not (isinstance(token, str) and 0 < len(token) <= 65536
            and all(32 < ord(c) < 127 for c in token)
            and isinstance(identity, str) and 0 < len(identity) <= 256
            and type(expire) in (int, float) and math.isfinite(expire)
            and expire > time.time()):
        return {'shared_login_issue': 'desktop_login_unavailable'}
    if account and identity != account:
        return {'shared_login_issue': 'account_changed'}
    return {'access_token': token, 'account_id': identity, 'expire': expire,
            'email': str(value.get('email') or '')[:320],
            'plan_type': str(value.get('plan_type') or '')[:64],
            'credential_source': 'desktop_codex', 'shared_login_issue': ''}


def _remote_snapshot(owner: int, agent_id: str, account: str, *, refresh: bool) -> dict:
    key = (owner, agent_id, account)
    # Revoked/re-paired/disconnected devices cannot authorize cached reads.
    if not any(a['agent_id'] == agent_id for a in _agents(owner)):
        _remote.invalidate(key)
        return {'shared_login_issue': 'desktop_disconnected'}
    with _locks.lock_for(key):
        cached = _remote.get(key)
        if cached and not refresh:
            return _snapshot(cached, account)
        if not refresh:
            return {'shared_login_issue': 'desktop_login_stale'}
        from lib.desktop.bridge import send_desktop_command
        result, error = send_desktop_command(
            'codex_login_read', {'account_id': account}, timeout=5,
            target_agent_id=agent_id, user_id=str(owner), ttl=5)
        snapshot = _snapshot(None if error else result, account)
        if snapshot.get('access_token'):
            _remote.set(key, snapshot)
        else:
            _remote.invalidate(key)
        return snapshot


def resolve_reference(reference: dict, *, refresh: bool = False) -> dict:
    """Read one bound source and retain failure metadata in the incident stream."""
    started = time.monotonic()
    snapshot = _resolve_reference(reference, refresh=refresh)
    issue = snapshot.get('shared_login_issue')
    if issue:
        source = reference.get('kind') if reference.get('kind') in {'local', 'desktop'} else 'unknown'
        elapsed_ms = round((time.monotonic() - started) * 1000)
        log_event(logger, logging.WARNING, 'codex_shared_login_read_failed',
                  '[CodexSharedLogin] source=%s phase=%s issue=%s elapsed_ms=%d',
                  source, 'refresh' if refresh else 'read', issue, elapsed_ms,
                  credential_source=source, phase='refresh' if refresh else 'read',
                  auth_issue=issue, elapsed_ms=elapsed_ms)
    return snapshot


def refresh_bound_reference(reference: dict) -> dict:
    """Re-read under the existing refresh lock; never revive a replaced binding."""
    from lib.oauth.token_store import load_stored_token, token_path
    from lib.json_store import locked_path
    with locked_path(token_path('codex') + '.refresh'):
        current = load_stored_token('codex') or {}
        if current.get('codex_shared_reference') != reference:
            return {'shared_login_issue': 'codex_source_changed'}
        return resolve_reference(reference, refresh=True)


def recover_bound_access(reference: dict, *, initial_issue: str) -> str | None:
    """One pre-ingress recovery, preserving source failures instead of reauth."""
    started = time.monotonic()
    snapshot = refresh_bound_reference(reference)
    issue = snapshot.get('shared_login_issue') or 'credential_unavailable'
    recovered = bool(snapshot.get('access_token'))
    source = reference.get('kind') if reference.get('kind') in {'local', 'desktop'} else 'unknown'
    log_event(logger, logging.WARNING, 'codex_shared_login_recovery',
              '[CodexSharedLogin] source=%s initial_issue=%s outcome=%s final_issue=%s elapsed_ms=%d',
              source, initial_issue, 'recovered' if recovered else 'failed',
              '' if recovered else issue, round((time.monotonic() - started) * 1000),
              credential_source=source, initial_issue=initial_issue,
              outcome='recovered' if recovered else 'failed',
              auth_issue='' if recovered else issue,
              elapsed_ms=round((time.monotonic() - started) * 1000))
    if recovered:
        return snapshot['access_token']
    if issue in {'codex_access_expired', 'codex_not_signed_in', 'not_chatgpt_login'}:
        return None
    from lib.llm_errors import SubscriptionCredentialUnavailableError
    raise SubscriptionCredentialUnavailableError(
        reason=issue, credential_source=source, transient=issue in _TRANSIENT_READ_ISSUES)


def _resolve_reference(reference: dict, *, refresh: bool = False) -> dict:
    if type(reference.get('owner_user_id')) is not int or reference['owner_user_id'] <= 0:
        return {'shared_login_issue': 'invalid_reference'}
    owner = local_owner(reference['owner_user_id'])
    if owner is None:
        return {'shared_login_issue': 'owner_unavailable'}
    account = reference.get('account_id')
    if not isinstance(account, str) or not account:
        return {'shared_login_issue': 'invalid_reference'}
    kind = reference.get('kind')
    if kind == 'local':
        from lib.oauth.codex_local_login import read_local_login
        return read_local_login(expected_account_id=account, refresh=refresh)
    if kind == 'desktop' and isinstance(reference.get('agent_id'), str):
        return _remote_snapshot(owner, reference['agent_id'], account, refresh=refresh)
    return {'shared_login_issue': 'invalid_reference'}


def discover_reference(*, owner_user_id: int, expected_account_id: str = '') -> dict | None:
    owner = local_owner(owner_user_id)
    if owner is None:
        return None
    from lib.oauth.codex_local_login import read_local_login
    snapshot = read_local_login(expected_account_id=expected_account_id, refresh=True)
    kind, agent_id = 'local', ''
    if not snapshot.get('access_token'):
        agents = _agents(owner)
        if len(agents) != 1:
            return None  # No device or ambiguous devices; never pick the latest.
        kind, agent_id = 'desktop', agents[0]['agent_id']
        snapshot = _remote_snapshot(owner, agent_id, expected_account_id, refresh=True)
    if not snapshot.get('access_token'):
        return None
    reference = {'owner_user_id': owner, 'kind': kind,
                 'account_id': snapshot['account_id']}
    if agent_id:
        reference['agent_id'] = agent_id
        _remote.set((owner, agent_id, reference['account_id']), snapshot)
    return reference


def reconcile_shared_login(*, owner_user_id=None, explicit: bool = False) -> None:
    """Discover once, pin account/device, or update the borrowed snapshot."""
    owner = local_owner(owner_user_id)
    if owner is None:
        return
    if explicit:
        from lib.oauth.codex_command_login import clear_command_context
        clear_command_context()
    from lib.oauth.token_store import load_stored_token, save_token, token_path
    from lib.json_store import locked_path, read_json
    # Same transaction lock as logout/refresh; no delayed discovery can undo logout.
    with locked_path(token_path('codex') + '.refresh'):
        if not explicit and read_json(token_path('codex') + '.shared-disabled', default=False):
            return
        stored = load_stored_token('codex')
        if stored is None and os.path.exists(token_path('codex')):
            return  # A corrupt existing login is not permission to change account.
        if stored is not None:
            reference = stored.get('codex_shared_reference')
            if isinstance(reference, dict):
                resolve_reference(reference, refresh=True)
                return
            # An explicit reconnect may replace an unusable independent login
            # only with the exact same account. Automatic discovery never does.
            expected = stored.get('account_id')
            expires = stored.get('expire')
            expired = type(expires) in (int, float) and expires <= time.time()
            if not (explicit and isinstance(expected, str) and expected
                    and (expired or stored.get('refresh_invalidated_at'))):
                return
            reference = discover_reference(owner_user_id=owner, expected_account_id=expected)
        else:
            reference = discover_reference(owner_user_id=owner)
        if reference:
            if save_token('codex', {'codex_shared_reference': reference}):
                disabled = token_path('codex') + '.shared-disabled'
                if os.path.exists(disabled):
                    os.remove(disabled)


def local_login_issue(*, owner_user_id: int) -> str:
    """Redacted local diagnosis after explicit reuse fails; no desktop command."""
    if local_owner(owner_user_id) is None:
        return 'owner_unavailable'
    from lib.oauth.codex_local_login import read_local_login
    return read_local_login().get('shared_login_issue', '')


def clear_shared_cache() -> None:
    """Discard borrowed bearer snapshots on Tofu logout."""
    _remote.clear()
    from lib.oauth.codex_command_login import clear_command_context
    clear_command_context()
