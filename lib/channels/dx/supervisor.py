"""大象 bridge supervisor: start, stop and restart sessions from configuration.

Responsibility: runs where the scheduled-jobs capability runs. On the
scheduler's existing maintenance tick (and when this process changes an
account) it confirms the identity of an enabled account whose login MIS has
none bound yet, starts a ``DxAdapter`` logged in as that MIS, stops it for a
disabled or switched account, and restarts it after an exit, reporting that
exit's outcome until the new session connects. Confirmation
(``whoami``, which may first wait for a 大象 login approval) runs on its own
thread, so neither the tick nor an HTTP request waits for it; it reports
``starting`` or, for a MIS without a reusable SSO session, ``authorizing``.
There is no timer, backoff schedule or retry count of its own: a restart
happens at the next tick. Launcher exit 42 (``SSO_SESSION_REQUIRED``) and an
engine kick-off stop restarts and surface ``needs_login`` / ``kicked`` until
the owner re-enables.

Entry points: ``reconcile_channel_bridges(principal)`` (scheduler tick),
``reconcile_owner(owner)``, ``notify_account_changed(owner)`` (HTTP handlers),
``start_channel_bridges()`` / ``stop_channel_bridges(timeout)`` (process lifecycle).
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
_WORKERS: set[threading.Thread] = set()
_LOCK = threading.RLock()
_STOPPING = threading.Event()
# Set once the scheduler tick has called in: only then does this process host bridges.
_HOSTING = threading.Event()
_TERMINAL_STATES = frozenset({'needs_login', 'kicked'})


def start_channel_bridges() -> None:
    """Open a new hosting lifecycle only after the previous one joined."""
    with _LOCK:
        if _STOPPING.is_set():
            if _ADAPTERS or _WORKERS:
                raise RuntimeError('大象 channel workers have not quiesced')
            _STOPPING.clear()


def _start_worker(target, *, name: str, args: tuple, kwargs: dict | None = None) -> None:
    """Called under _LOCK; retain physical thread ownership until exit."""
    _WORKERS.difference_update([worker for worker in _WORKERS if not worker.is_alive()])
    worker = threading.Thread(target=target, name=name, daemon=True, args=args, kwargs=kwargs or {})
    _WORKERS.add(worker)
    try:
        worker.start()
    except BaseException:
        _WORKERS.discard(worker)
        raise


def reconcile_channel_bridges(principal) -> None:
    """Scheduler-tick entry. TODO(enterprise): a distributed scheduler has no
    implicit owner; it needs an index of enabled channel accounts (and a
    per-account lease) before it can host bridges for every owner."""
    with _LOCK:
        if _STOPPING.is_set():
            return
        _HOSTING.set()
        owner = getattr(principal, 'owner_user_id', None)
        if owner is None:
            return
        store = ChannelStore(int(owner))
        reconcile_owner(int(owner), store=store)
        # One Router Watchdog signal per state since the last connection (R7).
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
    with _LOCK:
        if _STOPPING.is_set():
            return 'stopped'
        store = store or ChannelStore(owner)
        account = store.account(CHANNEL)
        current = _ADAPTERS.get(owner)
        if not account or not account.get('enabled'):
            if current is not None:
                if not current.stop(stop_timeout):
                    return 'stopping'
                _ADAPTERS.pop(owner, None)
                return 'stopped'
            return 'idle'
        login_mis = account_login_mis(account)
        bound = identity_bound(account)
        if current is not None and current.alive() and bound and current.login_mis == login_mis:
            current.tick()
            return 'running'
        restarting = False
        if current is not None:
            if not current.stop(stop_timeout):
                return 'stopping'
            _ADAPTERS.pop(owner, None)
            if bound and current.login_mis == login_mis:
                # The session of this login exited; one replaced by an account
                # switch has no outcome for the new login. Its restart keeps
                # reporting this outcome (Settings, Router Watchdog) until the
                # new session connects.
                state, detail = current.outcome()
                reported = account.get('status') or {}
                if (reported.get('state'), reported.get('detail')) != (state, detail):
                    logger.warning('[Channels] dx session exited owner=%s: %s: %s', owner, state, detail)
                store.set_status(CHANNEL, state, detail)
                if state in _TERMINAL_STATES:
                    return state
                restarting = True
        elif ((account.get('status') or {}).get('state')) in _TERMINAL_STATES:
            return account['status']['state']
        if not launch_spec(owner).installed:
            store.set_status(CHANNEL, 'error', 'The dx-pura-api skill is not installed')
            return 'error'
        if not bound:
            confirmation = (owner, account.get('mis'))
            if confirmation not in _CONFIRMING:
                _CONFIRMING.add(confirmation)
                try:
                    _start_worker(_confirm_identity, name=f'dx-identity-{owner}',
                                  args=(owner, store, account),
                                  kwargs={'adapter_factory': adapter_factory, 'stop_timeout': stop_timeout,
                                          'identity_resolver': identity_resolver,
                                          'session_factory': session_factory,
                                          'approval_check': approval_check})
                except BaseException:
                    _CONFIRMING.discard(confirmation)
                    raise
            return 'confirming'
        try:
            global_router_binding(owner, create=True)
        except RouterUnavailable as error:
            store.set_status(CHANNEL, 'error', str(error))
            return 'error'
        adapter = adapter_factory(owner, store=store, login_mis=login_mis)
        if not restarting:
            store.set_status(CHANNEL, 'starting', '')
        _ADAPTERS[owner] = adapter
        try:
            adapter.start()
        except Exception as error:
            logger.warning('[Channels] dx bridge start failed owner=%s', owner, exc_info=True)
            try:
                if adapter.stop(stop_timeout):
                    _ADAPTERS.pop(owner, None)
            except Exception:
                logger.warning('[Channels] dx partial start cleanup failed owner=%s', owner, exc_info=True)
            store.set_status(CHANNEL, 'error', f'bridge start failed: {getattr(error, "code", type(error).__name__)}')
            return 'error'
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
        with _LOCK:
            if _STOPPING.is_set():
                return
        session = session_factory(owner, account_login_mis(wanted))
        approval = session.mis is not None and approval_check(owner, session)
        with _LOCK:
            if _STOPPING.is_set():
                return
            if session.mis is None:
                store.set_status(CHANNEL, 'needs_login',
                                 'No 大象 account to log in with: enter your MIS in Settings', when=serves)
                return
            if approval:
                store.set_status(CHANNEL, 'authorizing', '', when=serves)
        identity = identity_resolver(owner, session)

        def bind(account):
            nonlocal bound
            if serves(account):
                account['identity'] = identity
                bound = True
            return account
        with _LOCK:
            if _STOPPING.is_set():
                return
            store.update_account(CHANNEL, bind)
    except ChannelError as error:
        _confirmation_failure(owner, store, 'needs_login' if error.code == 'needs_login' else 'error',
                              str(error), serves)
    except Exception as error:
        logger.warning('[Channels] dx identity confirmation failed owner=%s', owner, exc_info=True)
        _confirmation_failure(owner, store, 'error', f'identity confirmation failed: {type(error).__name__}',
                              serves)
    finally:
        with _LOCK:
            _CONFIRMING.discard((owner, chosen))
    if bound:
        try:
            reconcile_owner(owner, store=store, identity_resolver=identity_resolver,
                            session_factory=session_factory, approval_check=approval_check, **reconcile)
        except Exception:
            logger.warning('[Channels] dx reconcile failed owner=%s', owner, exc_info=True)


def _confirmation_failure(owner: int, store: ChannelStore, state: str, detail: str, serves) -> None:
    with _LOCK:
        if _STOPPING.is_set():
            return
        try:
            store.set_status(CHANNEL, state, detail, when=serves)
        except Exception:
            logger.warning('[Channels] dx identity status unavailable owner=%s', owner, exc_info=True)


def notify_account_changed(owner: int) -> None:
    """Apply a configuration change now when this process hosts bridges."""
    with _LOCK:
        if _STOPPING.is_set() or not _HOSTING.is_set():
            return
        _start_worker(_reconcile_quietly, args=(int(owner),), name=f'dx-reconcile-{owner}')


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
        _STOPPING.set()
        _HOSTING.clear()
        adapters = list(_ADAPTERS.items())
        workers = list(_WORKERS)
    for owner, adapter in adapters:
        try:
            if adapter.stop(timeout):
                with _LOCK:
                    if _ADAPTERS.get(owner) is adapter:
                        _ADAPTERS.pop(owner)
        except Exception:
            logger.warning('[Channels] dx bridge stop failed owner=%s', owner, exc_info=True)
    for worker in workers:
        if worker is not threading.current_thread():
            worker.join(timeout)
    with _LOCK:
        _WORKERS.difference_update([worker for worker in workers if not worker.is_alive()])
        return not _ADAPTERS and not _WORKERS


__all__ = ['bridge_state', 'notify_account_changed', 'reconcile_channel_bridges', 'reconcile_owner',
           'running_adapter', 'start_channel_bridges', 'stop_channel_bridges']
