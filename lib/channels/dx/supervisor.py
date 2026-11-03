"""大象 bridge supervisor: start, stop and restart sessions from configuration.

Responsibility: runs where the scheduled-jobs capability runs. On the
scheduler's existing maintenance tick (and when this process changes an
account) it confirms the identity of an enabled account whose login MIS has
none bound yet, starts a ``DxAdapter`` logged in as that MIS, stops it for a
disabled or switched account, and restarts it after an exit. Confirmation
(``whoami``, which may first wait for a 大象 login approval) runs on its own
thread, so neither the tick nor an HTTP request waits for it; it reports
``starting`` or, for a MIS without a reusable SSO session, ``authorizing``.
There is no timer, backoff schedule or retry count of its own: a restart
happens at the next tick. Launcher exit 42 (``SSO_SESSION_REQUIRED``) and an
engine kick-off stop restarts and surface ``needs_login`` / ``kicked`` until
the owner re-enables.

Entry points: ``reconcile_channel_bridges(principal)`` (scheduler tick),
``reconcile_owner(owner)``, ``notify_account_changed(owner)`` (HTTP handlers),
``stop_channel_bridges(timeout)`` (route background-service shutdown).
Dependencies: ``ChannelStore``, ``DxAdapter`` and the adapter's launcher
commands, ``lib.channels.dx.login``, ``delivery.global_router_binding``.
"""
from __future__ import annotations

import threading

from lib.channels.delivery import RouterUnavailable, global_router_binding
from lib.channels.dx.adapter import DxAdapter, launch_spec, launcher_session, resolve_identity
from lib.channels.dx.login import account_login_mis, approval_required, identity_bound
from lib.channels.model import ChannelError
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

CHANNEL = 'dx'
_ADAPTERS: dict[int, DxAdapter] = {}
# (owner, chosen login MIS) of each running identity confirmation thread (guarded
# by _LOCK). A newer choice starts its own at once instead of waiting for an
# older one that may sit in a 大象 login approval; the older one's result is
# then discarded.
_CONFIRMING: set[tuple[int, str | None]] = set()
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
                    adapter_factory=DxAdapter, stop_timeout: float = 2.0,
                    identity_resolver=resolve_identity, session_factory=launcher_session,
                    approval_check=approval_required) -> str:
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
        login_mis = account_login_mis(account)
        bound = identity_bound(account)
        if current is not None and current.alive() and bound and current.login_mis == login_mis:
            current.tick()
            return 'running'
        if current is not None:
            _ADAPTERS.pop(owner, None)
            current.stop(stop_timeout)
            if bound and current.login_mis == login_mis:
                # The session of this login exited; one replaced by an account
                # switch has no outcome for the new login.
                state, detail = current.outcome()
                store.set_status(CHANNEL, state, detail)
                if state in _TERMINAL_STATES:
                    return state
        elif ((account.get('status') or {}).get('state')) in _TERMINAL_STATES:
            return account['status']['state']
        if not launch_spec(owner).installed:
            store.set_status(CHANNEL, 'error', 'The dx-pura-api skill is not installed')
            return 'error'
        if not bound:
            confirmation = (owner, account.get('mis'))
            if confirmation not in _CONFIRMING:
                _CONFIRMING.add(confirmation)
                threading.Thread(target=_confirm_identity, name=f'dx-identity-{owner}', daemon=True,
                                 args=(owner, store, account),
                                 kwargs={'adapter_factory': adapter_factory, 'stop_timeout': stop_timeout,
                                         'identity_resolver': identity_resolver,
                                         'session_factory': session_factory,
                                         'approval_check': approval_check}).start()
            return 'confirming'
        try:
            global_router_binding(owner, create=True)
        except RouterUnavailable as error:
            store.set_status(CHANNEL, 'error', str(error))
            return 'error'
        adapter = adapter_factory(owner, store=store, login_mis=login_mis)
        store.set_status(CHANNEL, 'starting', '')
        try:
            adapter.start()
        except Exception as error:
            logger.warning('[Channels] dx bridge start failed owner=%s', owner, exc_info=True)
            store.set_status(CHANNEL, 'error', f'bridge start failed: {getattr(error, "code", type(error).__name__)}')
            return 'error'
        _ADAPTERS[owner] = adapter
        return 'started'


def _confirm_identity(owner: int, store: ChannelStore, wanted: dict, *, identity_resolver,
                      session_factory, approval_check, **reconcile) -> None:
    """Bind the identity of ``wanted``'s login MIS, then start its session.

    ``wanted`` is the account this confirmation serves: once it is disabled or
    switched to another MIS, neither the identity nor a status is written.
    """
    chosen = wanted.get('mis')

    def serves(account) -> bool:
        return bool(account.get('enabled')) and account.get('mis') == chosen and not identity_bound(account)

    bound = False
    try:
        session = session_factory(owner, account_login_mis(wanted))
        if session.mis is None:
            store.set_status(CHANNEL, 'needs_login',
                             'No 大象 account to log in with: enter your MIS in Settings', when=serves)
            return
        if approval_check(owner, session):
            store.set_status(CHANNEL, 'authorizing', '', when=serves)
        identity = identity_resolver(owner, session)

        def bind(account):
            nonlocal bound
            if serves(account):
                account['identity'] = identity
                bound = True
            return account
        store.update_account(CHANNEL, bind)
    except ChannelError as error:
        store.set_status(CHANNEL, 'needs_login' if error.code == 'needs_login' else 'error', str(error),
                         when=serves)
    except Exception as error:
        logger.warning('[Channels] dx identity confirmation failed owner=%s', owner, exc_info=True)
        store.set_status(CHANNEL, 'error', f'identity confirmation failed: {type(error).__name__}', when=serves)
    finally:
        with _LOCK:
            _CONFIRMING.discard((owner, chosen))
    if bound:
        try:
            reconcile_owner(owner, store=store, identity_resolver=identity_resolver,
                            session_factory=session_factory, approval_check=approval_check, **reconcile)
        except Exception:
            logger.warning('[Channels] dx reconcile failed owner=%s', owner, exc_info=True)


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
