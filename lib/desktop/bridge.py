"""Desktop-agent bridge — in-process command queue + result formatting.

The server queues commands here; the desktop agent long-polls
``POST /api/desktop/poll`` (in ``routes/desktop.py``) to pick them up and
return results. This module owns the queue state and the blocking
``send_desktop_command`` RPC so that lib-layer tool handlers can drive the
agent without importing the routes package.
"""

import asyncio
import threading
import time
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

# ══════════════════════════════════════════════════════════
#  Command Queue (mirrors lib/browser.py pattern)
# ══════════════════════════════════════════════════════════

command_queue: dict = {}
command_queue_lock = threading.Lock()

# Async-waiter registry for the async def /api/desktop/poll route. Mirrors
# lib/browser/queue.py: the agent's long-poll awaits an asyncio.Event so the
# worker thread is released; the SYNC send_desktop_command enqueue path wakes
# it via loop.call_soon_threadsafe. Each waiter {'loop':, 'event':} removes
# ITSELF in a finally (timeout / success / disconnect) so nothing leaks.
_async_waiters: list = []
_async_waiters_lock = threading.Lock()


def _wake_async_waiters() -> None:
    """Wake desktop async poll waiters after a command was enqueued (sync)."""
    with _async_waiters_lock:
        waiters = list(_async_waiters)
    for w in waiters:
        loop, event = w.get('loop'), w.get('event')
        if loop is None or event is None:
            continue
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError as e:
            logger.debug('[Desktop] async waiter wake skipped (loop closed): %s', e)

# Wrapped in a single-element list so route modules and this module share
# one mutable cell — a bare module int can't be rebound across a
# ``from ... import`` alias.
_last_poll = [0.0]

# Agent registry. Every poll carries a stable agent identity frame; anonymous
# agents are rejected at the HTTP boundary.
_agents: dict = {}

# Stream frame store (RWA P2 §3.4): cmd_id -> {'chunks': {seq: (stream,
# data)}, 'done': bool, 'updated_at': float}. The agent may re-send frames
# after a connection error (outbox prefix redeliver), so reassembly
# DEDUPES by seq; entries expire with the command TTL.
_streams: dict = {}

# Connection window: the agent is "connected" if it polled within this many
# seconds.
_CONNECTED_WINDOW_S = 15
# Pending commands older than this are expired (agent never picked them up).
_COMMAND_TTL_S = 90
# Long-poll wait window (seconds) for the async poll route. Env-overridable
# (tests set a small value) to match the browser bridge knob.
import os as _os
try:
    POLL_WAIT_TIMEOUT = float(_os.environ.get('TOFU_DESKTOP_POLL_WAIT', '8'))
except (ValueError, TypeError) as _e:
    logger.debug('[Desktop] bad TOFU_DESKTOP_POLL_WAIT %r (%s) — using 8.0s default',
                 _os.environ.get('TOFU_DESKTOP_POLL_WAIT'), _e)
    POLL_WAIT_TIMEOUT = 8.0


def last_poll_time() -> float:
    """Epoch seconds of the agent's most recent poll (0 if never)."""
    return _last_poll[0]


def record_poll() -> None:
    """Mark the agent as having just polled (called by the poll endpoint)."""
    _last_poll[0] = time.time()


def is_desktop_agent_connected() -> bool:
    """Check if the desktop agent has polled recently."""
    return time.time() - _last_poll[0] < _CONNECTED_WINDOW_S


def _sweep_streams_locked(now):
    # Per-command TTL: an egress stream (ttl=1800s) must survive a long
    # upstream silence (LLM thinking blocks produce NO frames for minutes);
    # default commands keep the 90s window. The command row carries the ttl.
    stale = []
    for cid, e in _streams.items():
        cmd = command_queue.get(cid)
        ttl = (cmd or {}).get('ttl') or _COMMAND_TTL_S
        if now - e['updated_at'] > ttl:
            stale.append(cid)
    for cid in stale:
        del _streams[cid]


def _command_owned_by_poller(cmd, agent_id: str, user_id: str) -> bool:
    """Return whether one authenticated poller owns a queued command."""
    if not isinstance(cmd, dict):
        return False
    if (cmd.get('user_id') or '') != user_id:
        return False
    claimed_agent_id = cmd.get('claimed_agent_id') or ''
    return bool(agent_id) and claimed_agent_id == agent_id


def resolve_streams(frames, *, agent_id: str, user_id: str) -> int:
    """Ingest stream frames from a poll body. Returns new-chunk count.

    Frames are ``{cmd_id, seq, stream, data, done}``; re-sent frames are
    deduped by seq so an agent reconnect never double-counts output.
    """
    count = 0
    now = time.time()
    with command_queue_lock:
        _sweep_streams_locked(now)
        for f in frames or []:
            if not isinstance(f, dict):
                continue
            cmd_id = f.get('cmd_id', '')
            seq = f.get('seq')
            if not cmd_id or not isinstance(seq, int):
                continue
            cmd = command_queue.get(cmd_id)
            if not _command_owned_by_poller(cmd, agent_id, user_id):
                logger.warning(
                    '[Desktop] rejected stream frame for unowned command %s '
                    'from agent %s', cmd_id[:8], agent_id[:8])
                continue
            entry = _streams.setdefault(
                cmd_id, {'chunks': {}, 'done': False, 'updated_at': now})
            entry['updated_at'] = now
            if seq not in entry['chunks']:
                entry['chunks'][seq] = (
                    str(f.get('stream') or 'stdout'),
                    str(f.get('data') or ''),
                )
                count += 1
            if f.get('done'):
                entry['done'] = True
    return count


def get_frames(cmd_id, since_seq=0):
    """Ordered RAW frames for one streamed command (S3 desktop-egress).

    Returns ``(frames, done)`` where frames is ``[(seq, stream, data), …]``
    ascending by seq (only seq > ``since_seq``), or ``None`` when the entry
    is unknown/expired — callers MUST treat None-before-done as an aborted
    stream (design §4.3: never wait forever for a swept entry).
    ``get_command_stream`` keeps its stdout/stderr contract on top of this.
    """
    now = time.time()
    with command_queue_lock:
        _sweep_streams_locked(now)
        entry = _streams.get(cmd_id)
        if entry is None:
            return None
        ordered = sorted((s, v) for s, v in entry['chunks'].items()
                         if s > since_seq)
        return ([(s, st, d) for s, (st, d) in ordered], entry['done'])


def get_command_stream(cmd_id, since_seq=0):
    """Reassembled stream for one command, or None when unknown/expired.

    Returns ``{'stdout', 'stderr', 'done', 'last_seq'}`` — pass
    ``since_seq=last_seq`` for an incremental read.
    """
    got = get_frames(cmd_id, since_seq)
    if got is None:
        return None
    frames, done = got
    text = {'stdout': [], 'stderr': []}
    last_seq = 0
    for seq, stream, data in frames:
        if stream in text:
            text[stream].append(data)
        last_seq = max(last_seq, seq)
    return {
        'stdout': ''.join(text['stdout']),
        'stderr': ''.join(text['stderr']),
        'done': done,
        'last_seq': last_seq,
    }


def register_agent(agent_id, meta=None, user_id='', key_id='') -> None:
    """Upsert a v2 agent in the registry and heartbeat it.

    ``meta`` is the agent frame from the poll body (name / platform /
    capabilities). ``user_id`` / ``key_id`` identify the bridge caller the
    poll authenticated as. Registration doubles as
    the liveness heartbeat: :func:`online_agents` only returns agents seen
    within the connection window, and a registered agent counts toward
    :func:`is_desktop_agent_connected`.
    """
    meta = meta if isinstance(meta, dict) else {}
    with command_queue_lock:
        prev = _agents.get(agent_id) or {}
        caps = meta.get('capabilities')
        _agents[agent_id] = {
            'agent_id': agent_id,
            'name': str(meta.get('name') or prev.get('name') or ''),
            'platform': str(meta.get('platform') or prev.get('platform') or ''),
            # The drift signal (owner amendment ②): which build the agent
            # runs, so the server can flag a protocol-mismatched endpoint.
            # A frame without it (older agent) keeps the previous value.
            'version': str(meta.get('version') or prev.get('version') or ''),
            'capabilities': (dict(caps) if isinstance(caps, dict)
                             else prev.get('capabilities') or {}),
            'share_roots': (list(meta['share_roots'])
                            if isinstance(meta.get('share_roots'), list)
                            else prev.get('share_roots') or []),
            'user_id': str(user_id or ''),
            'key_id': str(key_id or ''),
            'registered_at': prev.get('registered_at') or time.time(),
            'last_seen': time.time(),
        }
        _last_poll[0] = time.time()


def online_agents() -> list:
    """Registry agents whose heartbeat is inside the liveness window."""
    now = time.time()
    with command_queue_lock:
        return [dict(a) for a in _agents.values()
                if now - a['last_seen'] < _CONNECTED_WINDOW_S]


def list_agents(user_id=None) -> list:
    """All known agents with an ``online`` flag (status endpoint).

    ``user_id`` (RWA P4a): when given, only agents registered by that
    bridge caller are returned — a tenant must never see another tenant's
    machines on a relay deployment. ``None`` = unfiltered (operator view).
    """
    now = time.time()
    with command_queue_lock:
        out = [dict(a, online=(now - a['last_seen']) < _CONNECTED_WINDOW_S)
               for a in _agents.values()]
    if user_id is not None:
        out = [a for a in out if (a.get('user_id') or '') == (user_id or '')]
    return out


def _online_ids_locked(user_id=None) -> set:
    """Online registry ids — optionally scoped to one bridge user.

    The single-agent fallback counts only the CALLER's own endpoints
    (RWA P4a): other tenants' agents must not make an unaddressed command
    look multi-target, nor make it look deliverable.
    """
    now = time.time()
    return {aid for aid, a in _agents.items()
            if now - a['last_seen'] < _CONNECTED_WINDOW_S
            and (user_id is None
                 or (a.get('user_id') or '') == (user_id or ''))}


def _deliverable(cmd, agent_id, online_ids, poller_user='') -> bool:
    """Route one owner-scoped command to its target or sole online agent."""
    if (cmd.get('user_id') or '') != (poller_user or ''):
        return False
    claimed = cmd.get('claimed_agent_id')
    if claimed:
        return claimed == agent_id
    target = cmd.get('target_agent_id')
    if target:
        return target == agent_id
    return len(online_ids) == 1 and agent_id in online_ids


def _addressing_enqueue_error(target_agent_id, user_id=''):
    """Validate a to-be-enqueued command against the online-agent set.

    Returns an error string when the command must NOT be queued, else None:
    addressed → the target agent must be online AND belong to the caller's
    bridge user; unaddressed with more than one of the caller's endpoints
    online is refused. Other owners' agents are invisible.
    """
    user_id = user_id or ''
    online = [a for a in online_agents()
              if (a.get('user_id') or '') == user_id]
    if target_agent_id:
        if not any(a['agent_id'] == target_agent_id for a in online):
            return (f'target desktop agent {target_agent_id!r} is not online '
                    f'for this bridge user ({len(online)} own agent(s) online)')
        return None
    n = len(online)
    if n > 1:
        names = [a.get('name') or a['agent_id'] for a in online]
        return (f'{n} desktop agents are online ({", ".join(names)}); '
                'unaddressed command refused — it must name a '
                'target_agent_id instead of guessing')
    return None


def send_desktop_command(cmd_type, params=None, timeout=30, target_agent_id=None,
                         user_id='', cmd_id=None, ttl=None):
    """Queue a command for the desktop agent. Blocks until result or timeout.

    ``target_agent_id`` routes the command to one registered agent; when
    omitted, the single-agent selection applies and with
    several agents online the command is REFUSED up front — never
    delivered to a lucky poller. ``user_id`` (RWA P4a) scopes the command
    to agents registered by the same bridge user; it stays INTERNAL (never
    projected onto the wire). ``ttl`` overrides the default 90s pickup
    expiry (egress streams run far longer than 90s — design §4.3).
    """
    err = _addressing_enqueue_error(target_agent_id, user_id=user_id)
    if err:
        logger.warning('[Desktop] refusing %s: %s', cmd_type, err)
        return None, err
    cmd_id = cmd_id or str(uuid.uuid4())
    event = threading.Event()
    cmd = {
        'id': cmd_id,
        'type': cmd_type,
        'params': params or {},
        'created_at': time.time(),
        'event': event,
        'result': None,
        'error': None,
    }
    if target_agent_id:
        cmd['target_agent_id'] = target_agent_id
    cmd['user_id'] = str(user_id or '')
    if ttl:
        cmd['ttl'] = float(ttl)

    with command_queue_lock:
        command_queue[cmd_id] = cmd
    _wake_async_waiters()

    event.wait(timeout=timeout)

    with command_queue_lock:
        cmd = command_queue.pop(cmd_id, cmd)

    if not event.is_set():
        return None, 'Desktop agent timeout — is the agent running?'

    return cmd.get('result'), cmd.get('error')


def enqueue_desktop_command(cmd_type, params=None, target_agent_id=None,
                            user_id='', cmd_id=None, ttl=None):
    """Non-blocking enqueue: queue a command and return its id IMMEDIATELY.

    ``send_desktop_command`` blocks for the final result; streamed commands
    (egress_http_stream) and fire-and-forget cancels (egress_cancel) need
    the command on the wire NOW, with the caller consuming stream frames /
    ignoring the result instead of waiting. Returns ``(cmd_id, error)`` —
    error is the addressing refusal (same rules as send_desktop_command).
    """
    err = _addressing_enqueue_error(target_agent_id, user_id=user_id)
    if err:
        logger.warning('[Desktop] refusing %s: %s', cmd_type, err)
        return None, err
    cmd_id = cmd_id or str(uuid.uuid4())
    cmd = {
        'id': cmd_id,
        'type': cmd_type,
        'params': params or {},
        'created_at': time.time(),
        'event': threading.Event(),
        'result': None,
        'error': None,
    }
    if target_agent_id:
        cmd['target_agent_id'] = target_agent_id
    cmd['user_id'] = str(user_id or '')
    if ttl:
        cmd['ttl'] = float(ttl)
    with command_queue_lock:
        command_queue[cmd_id] = cmd
    _wake_async_waiters()
    return cmd_id, None


def resolve_results(results, *, agent_id: str, user_id: str) -> int:
    """Resolve agent-returned command results into the queue. Returns count."""
    resolved = 0
    for r in results or []:
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        with command_queue_lock:
            cmd = command_queue.get(cmd_id)
        if _command_owned_by_poller(cmd, agent_id, user_id):
            cmd['result'] = r.get('result')
            cmd['error'] = r.get('error')
            cmd['event'].set()
            resolved += 1
        elif cmd is not None:
            logger.warning(
                '[Desktop] rejected result for unowned command %s from '
                'agent %s', cmd_id[:8], agent_id[:8])
    return resolved


def take_pending_commands(*, agent_id: str, user_id: str) -> list:
    """Collect commands awaiting THIS poller, expiring stale ones.

    ``agent_id`` and ``user_id`` identify the authenticated poller. Commands
    are claimed before projection so only that agent may settle them.
    """
    pending = []
    now = time.time()
    with command_queue_lock:
        online_ids = _online_ids_locked(user_id)
        for cmd_id, cmd in list(command_queue.items()):
            if cmd['event'].is_set():
                continue  # already resolved
            if now - cmd['created_at'] > (cmd.get('ttl') or _COMMAND_TTL_S):
                cmd['error'] = 'Command expired (stale cleanup)'
                cmd['event'].set()
                continue
            if not _deliverable(cmd, agent_id, online_ids, user_id):
                continue
            cmd['claimed_agent_id'] = agent_id
            wire = {
                'id': cmd_id,
                'type': cmd['type'],
                'params': cmd['params'],
            }
            if cmd.get('target_agent_id'):
                wire['target_agent_id'] = cmd['target_agent_id']
            pending.append(wire)
    return pending


async def take_pending_commands_async(
    *, agent_id: str, user_id: str, timeout: float | None = None,
) -> list:
    """Async long-poll variant of take_pending_commands for the async route.

    Awaits an asyncio.Event (woken cross-thread by send_desktop_command)
    instead of returning immediately, so the agent picks up a command the
    instant it is queued — without pinning the worker thread for the wait.
    Poller identity is threaded through every re-check.
    """
    if timeout is None:
        timeout = POLL_WAIT_TIMEOUT
    pending = take_pending_commands(agent_id=agent_id, user_id=user_id)
    if pending:
        return pending

    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    waiter = {'loop': loop, 'event': event}
    with _async_waiters_lock:
        _async_waiters.append(waiter)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            event.clear()
            pending = take_pending_commands(
                agent_id=agent_id, user_id=user_id)
            if pending:
                return pending
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(event.wait(), timeout=min(remaining, 1.0))
            except asyncio.TimeoutError as e:
                logger.debug('[Desktop] async poll slice elapsed, re-checking queue: %s', e)
                pass
        return take_pending_commands(agent_id=agent_id, user_id=user_id)
    finally:
        with _async_waiters_lock:
            try:
                _async_waiters.remove(waiter)
            except ValueError as e:
                logger.debug('[Desktop] async waiter already deregistered: %s', e)


def pending_commands_count() -> int:
    """Number of queued commands not yet resolved."""
    with command_queue_lock:
        return sum(1 for c in command_queue.values() if not c['event'].is_set())


def format_desktop_result(cmd_type, result):
    """Format a desktop agent result for the LLM tool response."""
    if result is None:
        return '(no output)'
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        if result.get('error'):
            return f"Error: {result['error']}"
        if cmd_type == 'project_find_files' \
                and isinstance(result.get('files'), str):
            return result['files']
        if cmd_type == 'project_list_dir' \
                and isinstance(result.get('entries'), list):
            lines = [f"Directory: {result.get('path') or '.'}"]
            output_chars = len(lines[0])
            shown = 0
            for entry in result['entries']:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('name') or '')
                if not name:
                    continue
                entry_type = entry.get('type')
                is_dir = entry_type == 'dir'
                size = entry.get('size')
                rendered = f'  {name}/' if is_dir else f'  {name}'
                if entry_type in {'symlink', 'special'}:
                    rendered += f' [{entry_type}]'
                elif not is_dir and isinstance(size, int):
                    rendered += f' ({size} bytes)'
                if output_chars + len(rendered) + 1 > 64_000:
                    break
                lines.append(rendered)
                output_chars += len(rendered) + 1
                shown += 1
            if result.get('truncated') or shown < len(result['entries']):
                lines.append(
                    f'… [listing truncated after {shown} entries; '
                    'use a narrower relative path]')
            return '\n'.join(lines)
        # Screenshot results come as { "image_base64": "...", "width": ..., "height": ... }
        if 'image_base64' in result:
            w = result.get('width', '?')
            h = result.get('height', '?')
            return f'Screenshot captured ({w}x{h})'
        # System info, process list, etc.
        parts = []
        for k, v in result.items():
            if isinstance(v, list) and len(v) > 20:
                parts.append(f'{k}: [{len(v)} items]')
            else:
                parts.append(f'{k}: {v}')
        return '\n'.join(parts)
    if isinstance(result, list):
        if len(result) == 0:
            return '(empty list)'
        # File listings
        lines = []
        for item in result[:200]:
            if isinstance(item, dict):
                name = item.get('name', str(item))
                is_dir = item.get('is_dir', False)
                size = item.get('size', '')
                prefix = '[DIR] ' if is_dir else '[FILE] '
                suffix = f'  ({size} bytes)' if size and not is_dir else ''
                lines.append(f'{prefix}{name}{suffix}')
            else:
                lines.append(str(item))
        if len(result) > 200:
            lines.append(f'... and {len(result) - 200} more items')
        return '\n'.join(lines)
    return str(result)


__all__ = [
    'command_queue',
    'command_queue_lock',
    'format_desktop_result',
    'is_desktop_agent_connected',
    'last_poll_time',
    'get_command_stream',
    'list_agents',
    'online_agents',
    'pending_commands_count',
    'record_poll',
    'register_agent',
    'resolve_results',
    'resolve_streams',
    'send_desktop_command',
    'take_pending_commands',
    'take_pending_commands_async',
]
