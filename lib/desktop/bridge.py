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

# Agent registry (RWA P0 — docs/REMOTE_WORKTREE_DESIGN.md §3.2). v2 agents
# announce themselves via the 'agent' frame in the poll body; v1 agents
# never do and stay anonymous — their last poll is tracked separately so
# the fallback logic can tell "one legacy agent online" from "nobody".
_agents: dict = {}
_v1_last_poll = 0.0

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


def _addressing_enabled() -> bool:
    """Kill switch: TOFU_DESKTOP_ADDRESSING=0 restores legacy no-filtering."""
    return (_os.environ.get('TOFU_DESKTOP_ADDRESSING', '1') or '1').strip() != '0'


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


def resolve_streams(frames) -> int:
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
    poll authenticated as (per-user token — RWA P4a 约束②第三条; the
    legacy global secret registers unscoped ''). Registration doubles as
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


def note_v1_poll() -> None:
    """Record a poll from an UNREGISTERED (v1) agent.

    v1 agents carry no identity frame; this timestamp is all the bridge
    knows about them. Kept separate from _last_poll so the fallback logic
    can distinguish "one legacy agent online" from "v2 agent online".
    """
    global _v1_last_poll
    with command_queue_lock:
        _v1_last_poll = time.time()


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


def _v1_online_locked() -> bool:
    return bool(_v1_last_poll) and (time.time() - _v1_last_poll) < _CONNECTED_WINDOW_S


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


def _v1_online() -> bool:
    with command_queue_lock:
        return _v1_online_locked()


def _deliverable(cmd, agent_id, v1, online_ids, v1_on, poller_user='') -> bool:
    """Routing predicate (RWA P0 ②A + P4a 用户作用域):

    * user scope FIRST (fail-closed): a command only ever reaches a poller
      whose authenticated bridge user matches the command's user — on a
      relay deployment tenant A's agent can never pick up tenant B's
      command. Both empty = legacy single-user world (byte-identical);
    * ``target_agent_id`` set → only that v2 agent's poll;
    * unaddressed, v1 poller → only while NO v2 agent is online;
    * unaddressed, v2 poller → only when it is the SOLE online endpoint.
    """
    if (cmd.get('user_id') or '') != (poller_user or ''):
        return False
    target = cmd.get('target_agent_id')
    if target:
        return (not v1) and target == agent_id
    if v1:
        return not online_ids
    return len(online_ids) == 1 and agent_id in online_ids and not v1_on


def _addressing_enqueue_error(target_agent_id, user_id=''):
    """Validate a to-be-enqueued command against the online-agent set.

    Returns an error string when the command must NOT be queued, else None:
    addressed → the target agent must be online AND belong to the caller's
    bridge user; unaddressed with >1 of the CALLER's endpoints online →
    refused (hold, never deliver to a lucky poller); 0/1 online → allowed
    (legacy / single-agent fallback). Other users' agents are invisible
    here (RWA P4a 用户作用域).
    """
    user_id = user_id or ''
    online = [a for a in online_agents()
              if (a.get('user_id') or '') == user_id]
    if target_agent_id:
        if not any(a['agent_id'] == target_agent_id for a in online):
            return (f'target desktop agent {target_agent_id!r} is not online '
                    f'for this bridge user ({len(online)} own agent(s) online)')
        return None
    # v1 legacy agents are unscoped — only a legacy ('' user) caller sees them.
    v1 = _v1_online() and not user_id
    n = len(online) + (1 if v1 else 0)
    if n > 1:
        names = [a.get('name') or a['agent_id'] for a in online]
        if v1:
            names.append('legacy-agent(unregistered)')
        return (f'{n} desktop agents are online ({", ".join(names)}); '
                'unaddressed command refused — it must name a '
                'target_agent_id instead of guessing')
    return None


def send_desktop_command(cmd_type, params=None, timeout=30, target_agent_id=None,
                         user_id='', cmd_id=None, ttl=None):
    """Queue a command for the desktop agent. Blocks until result or timeout.

    ``target_agent_id`` (RWA P0) routes the command to one registered
    agent; when omitted, the single-agent fallback applies and with
    several agents online the command is REFUSED up front — never
    delivered to a lucky poller. ``user_id`` (RWA P4a) scopes the command
    to agents registered by the same bridge user; it stays INTERNAL (never
    projected onto the wire). ``ttl`` overrides the default 90s pickup
    expiry (egress streams run far longer than 90s — design §4.3).
    """
    if _addressing_enabled():
        err = _addressing_enqueue_error(target_agent_id, user_id=user_id)
        if err:
            logger.warning('[Desktop] refusing %s: %s', cmd_type, err)
            return None, err
    elif target_agent_id:
        return None, ('desktop addressing disabled '
                      '(TOFU_DESKTOP_ADDRESSING=0) — cannot target an agent')
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
    if user_id:
        cmd['user_id'] = str(user_id)
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
    if _addressing_enabled():
        err = _addressing_enqueue_error(target_agent_id, user_id=user_id)
        if err:
            logger.warning('[Desktop] refusing %s: %s', cmd_type, err)
            return None, err
    elif target_agent_id:
        return None, ('desktop addressing disabled '
                      '(TOFU_DESKTOP_ADDRESSING=0) — cannot target an agent')
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
    if user_id:
        cmd['user_id'] = str(user_id)
    if ttl:
        cmd['ttl'] = float(ttl)
    with command_queue_lock:
        command_queue[cmd_id] = cmd
    _wake_async_waiters()
    return cmd_id, None


def resolve_results(results) -> int:
    """Resolve agent-returned command results into the queue. Returns count."""
    resolved = 0
    for r in results or []:
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        with command_queue_lock:
            cmd = command_queue.get(cmd_id)
        if cmd:
            cmd['result'] = r.get('result')
            cmd['error'] = r.get('error')
            cmd['event'].set()
            resolved += 1
    return resolved


def take_pending_commands(agent_id=None, v1=True, user_id='') -> list:
    """Collect commands awaiting THIS poller, expiring stale ones.

    ``agent_id`` / ``v1`` / ``user_id`` identify the poller (v2 frame vs
    legacy agent; bridge user from the authenticated poll — RWA P4a).
    With addressing enabled the projection is filtered by
    :func:`_deliverable`; with the kill switch off it is the legacy
    unfiltered projection.
    """
    pending = []
    now = time.time()
    addressing = _addressing_enabled()
    with command_queue_lock:
        online_ids = _online_ids_locked(user_id) if addressing else set()
        v1_on = _v1_online_locked() if addressing else False
        for cmd_id, cmd in list(command_queue.items()):
            if cmd['event'].is_set():
                continue  # already resolved
            if now - cmd['created_at'] > (cmd.get('ttl') or _COMMAND_TTL_S):
                cmd['error'] = 'Command expired (stale cleanup)'
                cmd['event'].set()
                continue
            if addressing and not _deliverable(cmd, agent_id, v1,
                                               online_ids, v1_on, user_id):
                continue
            wire = {
                'id': cmd_id,
                'type': cmd['type'],
                'params': cmd['params'],
            }
            if cmd.get('target_agent_id'):
                wire['target_agent_id'] = cmd['target_agent_id']
            pending.append(wire)
    return pending


async def take_pending_commands_async(timeout: float = None, agent_id=None,
                                      v1: bool = True, user_id: str = '') -> list:
    """Async long-poll variant of take_pending_commands for the async route.

    Awaits an asyncio.Event (woken cross-thread by send_desktop_command)
    instead of returning immediately, so the agent picks up a command the
    instant it is queued — without pinning the worker thread for the wait.
    ``agent_id`` / ``v1`` identify the poller and are threaded through to
    :func:`take_pending_commands` on every re-check.
    """
    if timeout is None:
        timeout = POLL_WAIT_TIMEOUT
    pending = take_pending_commands(agent_id=agent_id, v1=v1, user_id=user_id)
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
            pending = take_pending_commands(agent_id=agent_id, v1=v1,
                                            user_id=user_id)
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
        return take_pending_commands(agent_id=agent_id, v1=v1,
                                     user_id=user_id)
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
    'note_v1_poll',
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
