"""Bot platform session supervisor: start, stop and restart from configuration (C12).

Responsibility: runs where the scheduled-jobs capability runs. On the
scheduler's existing maintenance tick (and when this process changes an
account) it converges each owner's 企业微信, QQ and 微信 session with its
account: start an enabled account's session, stop a disabled one, restart a
session after it exited or when its app ID, credential or API base changed
(against the session's own ``binding``), and pass the tick on (the session
runs an outbox pass). Same policy as the
大象 and GitHub supervisors (C6, C8): no timer, backoff schedule or retry
count of its own; a refused credential (``needs_login``) or a connection taken
over by another client (``kicked``) stops restarts until the owner updates
the account. A pending 微信 QR login starts the session that completes it;
that session adopts the issued token and keeps polling, so it is not replaced.

Entry points: ``reconcile_bot_sessions(principal)`` (scheduler tick),
``reconcile_owner(owner, channel)``, ``notify_account_changed(owner,
channel)``, ``running_session``, ``bridge_state(owner, channel)``,
``stop_bot_sessions(timeout)`` (background-service shutdown). Dependencies:
``ChannelStore``, ``BotSession``, ``delivery.global_router_binding``, the
credential vault, the Router Watchdog channel signal.
"""
from __future__ import annotations

import threading

from lib.channels.bots.session import BotSession, account_binding
from lib.channels.delivery import RouterUnavailable, global_router_binding
from lib.channels.model import BOT_CHANNELS
from lib.channels.store import ChannelStore
from lib.log import get_logger

logger = get_logger(__name__)

_SESSIONS: dict[tuple[int, str], BotSession] = {}
_LOCK = threading.RLock()
# Set once the scheduler tick has called in: only then does this process host sessions.
_HOSTING = threading.Event()
_TERMINAL_STATES = frozenset({'needs_login', 'kicked'})
_LOGIN_STATES = frozenset({'waiting', 'scanned'})


def reconcile_bot_sessions(principal) -> None:
    """Scheduler-tick entry. TODO(enterprise): like the 大象 supervisor, a
    distributed scheduler needs an enabled-account index and a per-account
    lease before it can host sessions for every owner."""
    _HOSTING.set()
    owner = getattr(principal, 'owner_user_id', None)
    if owner is None:
        return
    store = ChannelStore(int(owner))
    from lib.router.watchdog.signals import report_channel_status
    for channel in BOT_CHANNELS:
        try:
            reconcile_owner(int(owner), channel, store=store)
        except Exception:
            logger.warning('[Channels] %s reconcile failed owner=%s', channel, owner, exc_info=True)
        report_channel_status(int(owner), channel, store.account(channel))


def bridge_state(owner: int, channel: str) -> str:
    """``running``/``stopped`` where this process hosts sessions, else ``not_hosted``."""
    if not _HOSTING.is_set():
        return 'not_hosted'
    return 'running' if running_session(owner, channel) is not None else 'stopped'


def _credential_present(account: dict) -> bool:
    name = str((account.get('credential') or {}).get('name') or '')
    if not name:
        return False
    from lib.credentials_vault import list_entries
    return any(entry.get('name') == name for entry in list_entries())


def reconcile_owner(owner: int, channel: str, *, store: ChannelStore | None = None,
                    session_factory=None, stop_timeout: float = 2.0) -> str:
    """Converge one owner's session of ``channel`` with its account; returns the action taken."""
    store = store or ChannelStore(owner)
    factory = session_factory or BotSession
    key = (int(owner), channel)
    with _LOCK:
        account = store.account(channel)
        current = _SESSIONS.get(key)
        if not account or not account.get('enabled'):
            if current is not None:
                _SESSIONS.pop(key, None)
                current.stop(stop_timeout)
                return 'stopped'
            return 'idle'
        wanted = account_binding(account)
        if current is not None and current.alive() and current.binding == wanted:
            current.tick()
            return 'running'
        if current is not None:
            # An exited session already recorded its outcome; a newer owner
            # decision (re-enable, new secret, refreshed QR code) replaced it.
            _SESSIONS.pop(key, None)
            current.stop(stop_timeout)
        state = (account.get('status') or {}).get('state')
        if state in _TERMINAL_STATES:
            return state
        logging_in = channel == 'wechat' and (account.get('login') or {}).get('state') in _LOGIN_STATES
        if not logging_in and not _credential_present(account):
            store.set_status(channel, 'needs_login', 'The credential is missing; add it in Settings')
            return 'needs_login'
        if channel != 'wechat' and not account.get('appId'):
            store.set_status(channel, 'error', 'The app ID is missing; enter it in Settings')
            return 'error'
        try:
            global_router_binding(owner, create=True)
        except RouterUnavailable as error:
            store.set_status(channel, 'error', str(error))
            return 'error'
        session = factory(owner, channel, store=store)
        if not logging_in:
            store.set_status(channel, 'starting', '')
        try:
            session.start()
        except Exception as error:
            logger.warning('[Channels] %s session start failed owner=%s', channel, owner, exc_info=True)
            store.set_status(channel, 'error', f'session start failed: {getattr(error, "code", type(error).__name__)}')
            return 'error'
        _SESSIONS[key] = session
        return 'started'


def notify_account_changed(owner: int, channel: str) -> None:
    """Apply a configuration change now when this process hosts sessions."""
    if not _HOSTING.is_set():
        return
    threading.Thread(target=_reconcile_quietly, args=(int(owner), channel),
                     name=f'{channel}-reconcile-{owner}', daemon=True).start()


def _reconcile_quietly(owner: int, channel: str) -> None:
    try:
        reconcile_owner(owner, channel)
    except Exception:
        logger.warning('[Channels] %s reconcile failed owner=%s', channel, owner, exc_info=True)


def running_session(owner: int, channel: str) -> BotSession | None:
    with _LOCK:
        session = _SESSIONS.get((int(owner), channel))
    return session if session is not None and session.alive() else None


def stop_bot_sessions(timeout: float = 2.0) -> bool:
    with _LOCK:
        sessions = list(_SESSIONS.items())
        _SESSIONS.clear()
    stopped = True
    for (owner, channel), session in sessions:
        try:
            stopped = session.stop(timeout) and stopped
        except Exception:
            stopped = False
            logger.warning('[Channels] %s session stop failed owner=%s', channel, owner, exc_info=True)
    _HOSTING.clear()
    return stopped


__all__ = ['bridge_state', 'notify_account_changed', 'reconcile_bot_sessions', 'reconcile_owner',
           'running_session', 'stop_bot_sessions']
