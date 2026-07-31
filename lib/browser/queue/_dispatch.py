"""lib/browser/queue/_dispatch.py — SYNC + ASYNC command dispatch & resolution.

The core queue verbs: enqueue-and-block (``send_browser_command``), delivery
(``get_pending_commands``), the SYNC and ASYNC long-poll waits
(``wait_for_commands`` / ``wait_for_commands_async``), and result resolution
(``resolve_command`` / ``resolve_batch``).

All shared state is owned by ``_state`` and touched through it (so the process
has a single queue). ``wait_for_commands`` rebinds the process-wide
``_last_poll_time`` as ``_state._last_poll_time`` to keep one binding.
"""

import asyncio
import threading
import time
import uuid

from lib.log import get_logger

from . import _state
from ._state import (
    _async_waiters, _async_waiters_lock, _commands, _commands_lock, _notify,
    _get_active_client, _wake_async_waiters, POLL_WAIT_TIMEOUT,
)
from ._registry import mark_poll, _cleanup_stale, client_user_id

logger = get_logger(__name__)


def send_browser_command(cmd_type, params=None, timeout=30, client_id=None):
    """Send a command to a specific browser extension client and block until result.

    Args:
        cmd_type: Command type string.
        params: Command parameters dict.
        timeout: Max seconds to wait for result.
        client_id: Target client ID. If None, falls back to thread-local active
                   client, then to any connected client.
    """
    # Auto-resolve client_id from thread-local if not explicitly provided
    if not client_id:
        client_id = _get_active_client()
    logger.info('[Browser] Sending command %s (timeout=%ds, target_client=%s)',
                cmd_type, timeout, (client_id or 'any')[:12])

    # Check if the target client (or any client) is connected
    if client_id:
        with _state._clients_lock:
            info = _state._clients.get(client_id)
        if not info or time.time() - info['last_poll'] > 30:
            logger.warning('[Browser] Target client %s not connected', client_id[:12])
            return None, (f"Browser extension client {client_id[:8]} is not connected. "
                          "Check that the extension is running on the correct device.")
    else:
        if time.time() - _state._last_poll_time > 30:
            logger.warning('[Browser] No extension connected (last poll %.0fs ago)',
                           time.time() - _state._last_poll_time)
            return None, ("Browser extension is not connected. "
                          "Install the extension and enable it.")

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
        'target_client': client_id,   # None = any client can pick it up
        'timeout': timeout,           # caller's wait budget; delivery cutoff
        'cancelled': False,           # set when the caller gives up (see below)
        # B0 user scoping: the command inherits the tenant of the client it is
        # aimed at, so a poll authenticated as a DIFFERENT tenant can never be
        # handed this command (see _deliverable_to). '' = unscoped.
        'user_id': client_user_id(client_id) if client_id else '',
    }
    with _commands_lock:
        _commands[cmd_id] = cmd
    _notify.set()
    _wake_async_waiters(client_id)

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
            url_hint = p.get('url', '')[:80]
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
                    cmd_type, timeout, (client_id or 'any')[:12], picked,
                    pending_count, total_count, url_hint)
        return None, f"Browser command '{cmd_type}' timed out after {timeout}s. The extension may be busy or disconnected."

    with _commands_lock:
        cmd = _commands.pop(cmd_id, cmd)

    if cmd.get('error'):
        logger.warning('[Browser] Command %s returned error: %s', cmd_type, str(cmd['error'])[:200])
        return None, cmd['error']
    return cmd['result'], None


def _deliverable_to(cmd, client_id, poller_user):
    """True when ``cmd`` may be handed to this polling client.

    Mirrors the desktop bridge's ``_deliverable`` (``lib/desktop/bridge.py``):
    the USER check is the FIRST gate and is fail-closed. A browser command can
    read the cookie jar and attach the DevTools debugger, so handing one to the
    wrong tenant is a session-takeover primitive, not a routing nit.
    """
    if (cmd.get('user_id') or '') != (poller_user or ''):
        return False
    target = cmd.get('target_client')
    if target and client_id and target != client_id:
        return False
    return True


def get_pending_commands(client_id=None, user_id=''):
    """Return list of commands for a specific client (or unrouted commands).

    A command is eligible for a client if:
      - it belongs to the SAME bridge user as this poll (fail-closed), AND
      - target_client is None (unrouted — any client of that user), OR
      - target_client matches the requesting client_id
    """
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
            if not _deliverable_to(cmd, client_id, user_id):
                continue
            cmd['picked_up'] = True
            pending.append({
                'id': cmd['id'],
                'type': cmd['type'],
                'params': cmd['params'],
            })
    return pending


def wait_for_commands(timeout=8, client_id=None, user_id=''):
    """Block until commands are available for this client, or timeout."""
    _state._last_poll_time = time.time()
    mark_poll(client_id, user_id=user_id)
    _cleanup_stale()

    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = get_pending_commands(client_id=client_id, user_id=user_id)
        if pending:
            return pending
        _notify.clear()
        remaining = deadline - time.time()
        if remaining > 0:
            _notify.wait(timeout=min(remaining, 1.0))
    return []


async def wait_for_commands_async(timeout=None, client_id=None, user_id=''):
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
    mark_poll(client_id, user_id=user_id)
    _cleanup_stale()

    # Fast path: something is already queued for us.
    pending = get_pending_commands(client_id=client_id, user_id=user_id)
    if pending:
        return pending

    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    waiter = {'loop': loop, 'event': event, 'client_id': client_id}
    with _async_waiters_lock:
        _async_waiters.append(waiter)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            event.clear()
            pending = get_pending_commands(client_id=client_id, user_id=user_id)
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
        return get_pending_commands(client_id=client_id, user_id=user_id)
    finally:
        # ALWAYS deregister — covers timeout, success, and CancelledError
        # (client disconnected mid-wait). Without this the registry leaks a
        # dead loop/event on every disconnect.
        with _async_waiters_lock:
            try:
                _async_waiters.remove(waiter)
            except ValueError as e:
                logger.debug('[Browser] async waiter already deregistered: %s', e)


def resolve_command(cmd_id, result=None, error=None):
    """Resolve a command result, unblocking the waiting send_browser_command."""
    with _commands_lock:
        cmd = _commands.get(cmd_id)
    if not cmd:
        return False
    cmd['result'] = result
    cmd['error'] = error
    cmd['event'].set()
    return True


def resolve_batch(results):
    """Resolve multiple command results at once. Returns count of resolved."""
    resolved = 0
    for r in (results or []):
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        if resolve_command(cmd_id, result=r.get('result'), error=r.get('error')):
            resolved += 1
    return resolved
