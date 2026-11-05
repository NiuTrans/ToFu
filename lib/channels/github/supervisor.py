"""GitHub session supervisor: start, stop and restart pollers from configuration (C8).

Responsibility: runs where the scheduled-jobs capability runs. On the
scheduler's existing maintenance tick (and when this process changes the
account) it starts a ``GitHubAdapter`` for an enabled account, stops it for a
disabled one, restarts it after an exit or when its identity, credential or
API base changed, and passes the tick on (the adapter polls only when
GitHub's ``X-Poll-Interval`` says it may). No timer, backoff schedule or
retry count of its own: a restart happens at the next tick, and a refused
token (``needs_login``) stops restarts until the owner updates the account.
Same policy as the 大象 supervisor (C6).

Entry points: ``reconcile_github_sessions(principal)`` (scheduler tick),
``reconcile_owner(owner)``, ``notify_account_changed(owner)`` (account
commands), ``running_adapter(owner)``, ``stop_github_sessions(timeout)``
(route background-service shutdown). Dependencies: ``ChannelStore``,
``GitHubAdapter``, ``delivery.global_router_binding``, the credential vault.
"""
from __future__ import annotations

import threading

from lib.channels.delivery import RouterUnavailable, global_router_binding
from lib.channels.github.adapter import CHANNEL, GitHubAdapter, account_binding
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

_ADAPTERS: dict[int, GitHubAdapter] = {}
_LOCK = threading.RLock()
# Set once the scheduler tick has called in: only then does this process host sessions.
_HOSTING = threading.Event()
_TERMINAL_STATES = frozenset({'needs_login'})


def reconcile_github_sessions(principal) -> None:
    """Scheduler-tick entry. TODO(enterprise): like the 大象 supervisor, a
    distributed scheduler needs an enabled-account index and a per-account
    lease before it can host sessions for every owner."""
    _HOSTING.set()
    owner = getattr(principal, 'owner_user_id', None)
    if owner is None:
        return
    store = ChannelStore(int(owner))
    reconcile_owner(int(owner), store=store)
    # Same Router Watchdog report as the 大象 supervisor (docs/ROUTER.md R7).
    from lib.router.watchdog.signals import report_channel_status
    report_channel_status(int(owner), CHANNEL, store.account(CHANNEL))


def bridge_state(owner: int) -> str:
    """``running``/``stopped`` where this process hosts sessions, else ``not_hosted``."""
    if not _HOSTING.is_set():
        return 'not_hosted'
    return 'running' if running_adapter(owner) is not None else 'stopped'


def _credential_present(account: dict) -> bool:
    name = str((account.get('credential') or {}).get('name') or '')
    if not name:
        return False
    from lib.credentials_vault import list_entries
    return any(entry.get('name') == name for entry in list_entries())


def reconcile_owner(owner: int, *, store: ChannelStore | None = None,
                    adapter_factory=GitHubAdapter, stop_timeout: float = 2.0) -> str:
    """Converge one owner's GitHub session with its account; returns the action taken."""
    store = store or ChannelStore(owner)
    with _LOCK:
        account = store.account(CHANNEL)
        current = _ADAPTERS.get(owner)
        if not account or not account.get('enabled'):
            if current is not None:
                _ADAPTERS.pop(owner, None)
                current.stop(stop_timeout)
                return 'stopped'
            return 'idle'
        if current is not None and current.alive() and current.binding == account_binding(account):
            current.tick()
            return 'running'
        if current is not None:
            # An exited session already recorded its outcome; a newer owner
            # decision (re-enable, new token) has replaced it with 'starting'.
            _ADAPTERS.pop(owner, None)
            current.stop(stop_timeout)
        if ((account.get('status') or {}).get('state')) in _TERMINAL_STATES:
            return account['status']['state']
        if not (account.get('identity') or {}).get('uid'):
            store.set_status(CHANNEL, 'error', 'GitHub identity is unresolved; enable the account again')
            return 'error'
        if not _credential_present(account):
            store.set_status(CHANNEL, 'needs_login', 'The GitHub token is missing; add it in Settings')
            return 'needs_login'
        try:
            global_router_binding(owner, create=True)
        except RouterUnavailable as error:
            store.set_status(CHANNEL, 'error', str(error))
            return 'error'
        adapter = adapter_factory(owner, store=store)
        store.set_status(CHANNEL, 'starting', '')
        try:
            adapter.start()
        except Exception as error:
            logger.warning('[Channels] github session start failed owner=%s', owner, exc_info=True)
            store.set_status(CHANNEL, 'error',
                             f'session start failed: {getattr(error, "code", type(error).__name__)}')
            return 'error'
        _ADAPTERS[owner] = adapter
        return 'started'


def notify_account_changed(owner: int) -> None:
    """Apply a configuration change now when this process hosts sessions."""
    if not _HOSTING.is_set():
        return
    threading.Thread(target=_reconcile_quietly, args=(int(owner),),
                     name=f'github-reconcile-{owner}', daemon=True).start()


def _reconcile_quietly(owner: int) -> None:
    try:
        reconcile_owner(owner)
    except Exception:
        logger.warning('[Channels] github reconcile failed owner=%s', owner, exc_info=True)


def running_adapter(owner: int) -> GitHubAdapter | None:
    with _LOCK:
        adapter = _ADAPTERS.get(int(owner))
    return adapter if adapter is not None and adapter.alive() else None


def stop_github_sessions(timeout: float = 2.0) -> bool:
    with _LOCK:
        adapters = list(_ADAPTERS.items())
        _ADAPTERS.clear()
    stopped = True
    for owner, adapter in adapters:
        try:
            stopped = adapter.stop(timeout) and stopped
        except Exception:
            stopped = False
            logger.warning('[Channels] github session stop failed owner=%s', owner, exc_info=True)
    _HOSTING.clear()
    return stopped


__all__ = ['bridge_state', 'notify_account_changed', 'reconcile_github_sessions', 'reconcile_owner',
           'running_adapter', 'stop_github_sessions']
