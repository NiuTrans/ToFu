"""Owner/device-addressed browser command dispatch and settlement.

The core queue verbs: enqueue-and-block (``send_browser_command``), delivery
(``get_pending_commands``), the SYNC and ASYNC long-poll waits
(``wait_for_commands`` / ``wait_for_commands_async``), and result resolution
(``resolve_command`` / ``resolve_batch``).

All shared state is owned by ``_state``. Commands are never unaddressed:
enqueue, claim, and result settlement all require the same owner/device pair.
"""

import asyncio
import threading
import time
import uuid

from lib.log import get_logger

from ..log_safety import text_for_log, url_for_log

from ._state import (
    _async_waiters, _async_waiters_lock, _commands, _commands_lock, _notify,
    _wake_async_waiters, POLL_WAIT_TIMEOUT,
)
from ._registry import _cleanup_stale, is_extension_connected
from ._limits import (
    BrowserPollCapacityExceeded,
    MAX_COMMANDS_PER_POLL,
    waiter_limits,
)

logger = get_logger(__name__)
_POLL_WAITER_MAX, _POLL_WAITER_PER_OWNER = waiter_limits()


def _normalize_route(client_id, owner_user_id) -> tuple[str, str]:
    client_id = str(client_id or '').strip()
    owner_user_id = str(owner_user_id or '').strip()
    if not client_id:
        raise ValueError('client_id is required')
    if not owner_user_id.isdigit() or int(owner_user_id) < 1:
        raise ValueError('owner_user_id must be a positive integer')
    return client_id, owner_user_id


def send_browser_command(
    cmd_type,
    params=None,
    timeout=30,
    *,
    client_id,
    owner_user_id,
):
    """Send a command to a specific browser extension client and block until result.

    Args:
        cmd_type: Command type string.
        params: Command parameters dict.
        timeout: Max seconds to wait for result.
        client_id: Target device ID.
        owner_user_id: Authenticated repository owner.
    """
    client_id, owner_user_id = _normalize_route(
        client_id, owner_user_id)
    logger.info('[Browser] Sending command %s (timeout=%ds, owner=%s, client=%s)',
                cmd_type, timeout, owner_user_id, client_id[:12])
    if not is_extension_connected(
            client_id, owner_user_id=owner_user_id):
        logger.warning('[Browser] Target owner/device is not connected: %s/%s',
                       owner_user_id, client_id[:12])
        return None, (
            f'Browser extension client {client_id[:8]} is not connected '
            'for this user.')

    _cleanup_stale()

    cmd_id = str(uuid.uuid4())
    event = threading.Event()
    cmd = {
        'id': cmd_id,
        'type': cmd_type,
        'params': params or {},
        'event': event,
        'result': None,
        'error': None,
        'created_at': time.time(),
        'picked_up': False,
        'target_client': client_id,
        'claimed_client_id': '',
        'claimed_owner_user_id': '',
        'timeout': timeout,           # caller's wait budget; delivery cutoff
        'cancelled': False,           # set when the caller gives up (see below)
        'owner_user_id': owner_user_id,
    }
    with _commands_lock:
        _commands[cmd_id] = cmd
    _notify.set()
    _wake_async_waiters(client_id, owner_user_id)

    if not event.wait(timeout=timeout):
        # Caller gave up: mark cancelled (so an in-flight get_pending_commands
        # that raced to pick it up won't hand it to the extension) and remove it.
        with _commands_lock:
            stale = _commands.get(cmd_id)
            if stale is not None:
                stale['cancelled'] = True
            timed_out_cmd = _commands.pop(cmd_id, None)
        picked = timed_out_cmd.get('picked_up', False) if timed_out_cmd else False
        url_hint = ''
        if timed_out_cmd:
            p = timed_out_cmd.get('params') or {}
            url_hint = url_for_log(p.get('url', ''))
        with _commands_lock:
            pending_count = sum(1 for c in _commands.values() if not c.get('picked_up'))
            total_count = len(_commands)
        # 2026-05-05 noise-reduction: command-level timeout is routinely
        # triggered by slow pages / idle extensions; the CALLER (e.g.
        # try_browser_fetch) already logs its own WARNING / INFO on the
        # final giveup path. Log at INFO so error.log isn't flooded with
        # duplicate timeout notices (114+114/day under normal load).
        logger.info('[Browser] Command %s timed out after %ds (client=%s, picked_up=%s, '
                    'pending_queue=%d, total_inflight=%d, url=%s) '
                    '— extension may be overloaded or disconnected',
                    cmd_type, timeout, client_id[:12], picked,
                    pending_count, total_count, url_hint)
        return None, f"Browser command '{cmd_type}' timed out after {timeout}s. The extension may be busy or disconnected."

    with _commands_lock:
        cmd = _commands.pop(cmd_id, cmd)

    if cmd.get('error'):
        logger.warning('[Browser] Command %s returned error: %s', cmd_type,
                       text_for_log(cmd['error'], max_chars=200))
        return None, cmd['error']
    return cmd['result'], None


def _deliverable_to(cmd, client_id, owner_user_id):
    """True when ``cmd`` may be handed to this polling client.

    Mirrors the desktop bridge's ``_deliverable`` (``lib/desktop/bridge.py``):
    the USER check is the FIRST gate and is fail-closed. A browser command can
    read the cookie jar and attach the DevTools debugger, so handing one to the
    wrong tenant is a session-takeover primitive, not a routing nit.
    """
    return (
        (cmd.get('owner_user_id') or '') == owner_user_id
        and (cmd.get('target_client') or '') == client_id
    )


def get_pending_commands(*, client_id, owner_user_id):
    """Atomically claim commands for one exact owner/device poller.

    A command is eligible for a client if:
      - it belongs to the same authenticated owner, and
      - target_client exactly matches the polling device.
    """
    client_id, owner_user_id = _normalize_route(
        client_id, owner_user_id)
    now = time.time()
    with _commands_lock:
        pending = []
        for cmd_id, cmd in list(_commands.items()):
            if cmd.get('picked_up') or cmd.get('cancelled'):
                continue
            # Never deliver a command the caller has already given up on: the
            # delivery cutoff is the caller's OWN timeout, not a magic 60s. A
            # command picked up after this would fire a stray click/navigate
            # 30-60s after the model moved on, with its result silently dropped.
            if now - cmd['created_at'] > cmd.get('timeout', 30):
                continue
            # User scope + per-client routing (user check first, fail-closed).
            if not _deliverable_to(cmd, client_id, owner_user_id):
                continue
            cmd['picked_up'] = True
            cmd['claimed_client_id'] = client_id
            cmd['claimed_owner_user_id'] = owner_user_id
            pending.append({
                'id': cmd['id'],
                'type': cmd['type'],
                'params': cmd['params'],
            })
            if len(pending) >= MAX_COMMANDS_PER_POLL:
                break
    return pending


def wait_for_commands(*, timeout=8, client_id, owner_user_id):
    """Block until commands are available for this client, or timeout."""
    client_id, owner_user_id = _normalize_route(
        client_id, owner_user_id)
    _cleanup_stale()

    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = get_pending_commands(
            client_id=client_id, owner_user_id=owner_user_id)
        if pending:
            return pending
        _notify.clear()
        remaining = deadline - time.time()
        if remaining > 0:
            _notify.wait(timeout=min(remaining, 1.0))
    return []


async def wait_for_commands_async(
    *,
    timeout=None,
    client_id,
    owner_user_id,
):
    """Async-native variant of wait_for_commands for ``async def`` poll routes.

    Awaits on an asyncio.Event instead of blocking a thread on the
    threading.Event, so the Hypercorn worker thread is RELEASED for the
    entire (up-to-``timeout``) wait. Commands enqueued from sync tool threads
    wake this via ``_wake_async_waiters`` → ``loop.call_soon_threadsafe``.

    Preserves the exact semantics of the sync path: per-client routing and
    the §3 TTL delivery cutoff both live in ``get_pending_commands``, which
    this calls unchanged. Returns a list of command dicts (possibly empty).
    """
    if timeout is None:
        timeout = POLL_WAIT_TIMEOUT
    client_id, owner_user_id = _normalize_route(
        client_id, owner_user_id)
    _cleanup_stale()

    # Fast path: something is already queued for us.
    pending = get_pending_commands(
        client_id=client_id, owner_user_id=owner_user_id)
    if pending:
        return pending

    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    waiter = {
        'loop': loop,
        'event': event,
        'client_id': client_id,
        'owner_user_id': owner_user_id,
        'superseded': False,
    }
    superseded_waiters = []
    with _async_waiters_lock:
        duplicates = [
            existing for existing in _async_waiters
            if existing.get('client_id') == client_id
            and existing.get('owner_user_id') == owner_user_id
        ]
        duplicate_ids = {id(existing) for existing in duplicates}
        effective_total = len(_async_waiters) - len(duplicates)
        effective_owner = sum(
            1 for existing in _async_waiters
            if existing.get('owner_user_id') == owner_user_id
            and id(existing) not in duplicate_ids
        )
        if effective_total >= _POLL_WAITER_MAX:
            raise BrowserPollCapacityExceeded(
                'browser_poll_waiter_capacity',
                'The browser long-poll registry is at capacity.',
            )
        if effective_owner >= _POLL_WAITER_PER_OWNER:
            raise BrowserPollCapacityExceeded(
                'browser_poll_owner_waiter_capacity',
                'This owner has too many active browser long-polls.',
            )
        # A network retry can briefly overlap the request it replaces.  Keep
        # the wire seamless: the older poll returns an ordinary empty 200 and
        # only the newest poll remains eligible to wait for commands.
        for existing in duplicates:
            existing['superseded'] = True
            _async_waiters.remove(existing)
            superseded_waiters.append(existing)
        _async_waiters.append(waiter)
    for existing in superseded_waiters:
        try:
            existing['loop'].call_soon_threadsafe(existing['event'].set)
        except RuntimeError as exc:
            logger.debug('[Browser] superseded poll loop already closed: %s', exc)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if waiter.get('superseded'):
                return []
            event.clear()
            pending = get_pending_commands(
                client_id=client_id, owner_user_id=owner_user_id)
            if pending:
                return pending
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Cap each await slice so a missed wake (e.g. command enqueued in
            # the tiny window between get_pending_commands and event.clear())
            # still gets re-checked promptly, mirroring the sync 1.0s cap.
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError as e:
                logger.debug('[Browser] async poll slice elapsed, re-checking queue: %s', e)
                pass  # slice elapsed — loop re-checks the queue
        # Final check after the loop in case a command landed at the deadline.
        return get_pending_commands(
            client_id=client_id, owner_user_id=owner_user_id)
    finally:
        # ALWAYS deregister — covers timeout, success, and CancelledError
        # (client disconnected mid-wait). Without this the registry leaks a
        # dead loop/event on every disconnect.
        with _async_waiters_lock:
            try:
                _async_waiters.remove(waiter)
            except ValueError as e:
                logger.debug('[Browser] async waiter already deregistered: %s', e)


def resolve_command(
    cmd_id,
    *,
    client_id,
    owner_user_id,
    result=None,
    error=None,
):
    """Settle a command only for the owner/device that claimed it."""
    try:
        client_id, owner_user_id = _normalize_route(
            client_id, owner_user_id)
    except ValueError:
        return False
    with _commands_lock:
        cmd = _commands.get(cmd_id)
        if not cmd:
            return False
        if (
            cmd.get('claimed_client_id') != client_id
            or cmd.get('claimed_owner_user_id') != owner_user_id
        ):
            return False
        cmd['result'] = result
        cmd['error'] = error
        cmd['event'].set()
    return True


def resolve_batch(results, *, client_id, owner_user_id):
    """Resolve multiple command results at once. Returns count of resolved."""
    resolved = 0
    for r in (results or []):
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        if resolve_command(
            cmd_id,
            client_id=client_id,
            owner_user_id=owner_user_id,
            result=r.get('result'),
            error=r.get('error'),
        ):
            resolved += 1
    return resolved
