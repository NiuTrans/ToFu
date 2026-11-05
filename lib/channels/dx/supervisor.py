"""大象 bridge supervisor: start, stop and restart sessions from configuration.

Responsibility: runs where the scheduled-jobs capability runs. On the
scheduler's existing maintenance tick (and when this process changes an
account) it starts a ``DxAdapter`` for an enabled account, stops it for a
disabled one, and restarts it after an exit. There is no timer, backoff
schedule or retry count of its own: a restart happens at the next tick.
Launcher exit 42 (``SSO_SESSION_REQUIRED``) and an engine kick-off stop
restarts and surface ``needs_login`` / ``kicked`` until the owner re-enables.

Entry points: ``reconcile_channel_bridges(principal)`` (scheduler tick),
``reconcile_owner(owner)``, ``notify_account_changed(owner)`` (HTTP handlers),
``stop_channel_bridges(timeout)`` (route background-service shutdown).
Dependencies: ``ChannelStore``, ``DxAdapter``, ``delivery.global_router_binding``.
"""
from __future__ import annotations

import threading

from lib.channels.delivery import RouterUnavailable, global_router_binding
from lib.channels.dx.adapter import DxAdapter, launch_spec
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'dx'
_ADAPTERS: dict[int, DxAdapter] = {}
_LOCK = threading.RLock()
# Set once the scheduler tick has called in: only then does this process host bridges.
_HOSTING = threading.Event()
_TERMINAL_STATES = frozenset({'needs_login', 'kicked'})


def reconcile_channel_bridges(principal) -> None:
    """Scheduler-tick entry. TODO(enterprise): a distributed scheduler has no
    implicit owner; it needs an index of enabled channel accounts (and a
    per-account lease) before it can host bridges for every owner."""
    _HOSTING.set()
    owner = getattr(principal, 'owner_user_id', None)
    if owner is None:
        return
    store = ChannelStore(int(owner))
    reconcile_owner(int(owner), store=store)
    # Whichever writer recorded it, an unhealthy account becomes one Router
    # Watchdog signal per state since its last connection (docs/ROUTER.md R7).
    from lib.router.watchdog.signals import report_channel_status
    report_channel_status(int(owner), CHANNEL, store.account(CHANNEL))


def bridge_state(owner: int) -> str:
    """``running``/``stopped`` where this process hosts bridges, else ``not_hosted``."""
    if not _HOSTING.is_set():
        return 'not_hosted'
    return 'running' if running_adapter(owner) is not None else 'stopped'


def reconcile_owner(owner: int, *, store: ChannelStore | None = None,
                    adapter_factory=DxAdapter, stop_timeout: float = 2.0) -> str:
    """Converge one owner's bridge with its account; returns the action taken."""
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
        if current is not None and current.alive():
            current.tick()
            return 'running'
        if current is not None:
            _ADAPTERS.pop(owner, None)
            current.stop(stop_timeout)
            state, detail = current.outcome()
            store.set_status(CHANNEL, state, detail)
            if state in _TERMINAL_STATES:
                return state
        elif ((account.get('status') or {}).get('state')) in _TERMINAL_STATES:
            return account['status']['state']
        if not account.get('identity'):
            store.set_status(CHANNEL, 'error', 'Owner identity is unresolved; re-enable the account')
            return 'error'
        if not launch_spec(owner).installed:
            store.set_status(CHANNEL, 'error', 'The dx-pura-api skill is not installed')
            return 'error'
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
            logger.warning('[Channels] dx bridge start failed owner=%s', owner, exc_info=True)
            store.set_status(CHANNEL, 'error', f'bridge start failed: {getattr(error, "code", type(error).__name__)}')
            return 'error'
        _ADAPTERS[owner] = adapter
        return 'started'


def notify_account_changed(owner: int) -> None:
    """Apply a configuration change now when this process hosts bridges."""
    if not _HOSTING.is_set():
        return
    threading.Thread(target=_reconcile_quietly, args=(int(owner),),
                     name=f'dx-reconcile-{owner}', daemon=True).start()


def _reconcile_quietly(owner: int) -> None:
    try:
        reconcile_owner(owner)
    except Exception:
        logger.warning('[Channels] dx reconcile failed owner=%s', owner, exc_info=True)


def running_adapter(owner: int) -> DxAdapter | None:
    with _LOCK:
        adapter = _ADAPTERS.get(int(owner))
    return adapter if adapter is not None and adapter.alive() else None


def stop_channel_bridges(timeout: float = 2.0) -> bool:
    with _LOCK:
        adapters = list(_ADAPTERS.items())
        _ADAPTERS.clear()
    stopped = True
    for owner, adapter in adapters:
        try:
            stopped = adapter.stop(timeout) and stopped
        except Exception:
            stopped = False
            logger.warning('[Channels] dx bridge stop failed owner=%s', owner, exc_info=True)
    _HOSTING.clear()
    return stopped


__all__ = ['bridge_state', 'notify_account_changed', 'reconcile_channel_bridges', 'reconcile_owner',
           'running_adapter', 'stop_channel_bridges']
