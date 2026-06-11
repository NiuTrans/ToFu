"""Desktop-agent bridge — in-process command queue + result formatting.

The server queues commands here; the desktop agent long-polls
``POST /api/desktop/poll`` (in ``routes/desktop.py``) to pick them up and
return results. This module owns the queue state and the blocking
``send_desktop_command`` RPC so that lib-layer tool handlers can drive the
agent without importing the routes package.
"""

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

# Wrapped in a single-element list so route modules and this module share
# one mutable cell — a bare module int can't be rebound across a
# ``from ... import`` alias.
_last_poll = [0.0]

# Connection window: the agent is "connected" if it polled within this many
# seconds.
_CONNECTED_WINDOW_S = 15
# Pending commands older than this are expired (agent never picked them up).
_COMMAND_TTL_S = 90


def last_poll_time() -> float:
    """Epoch seconds of the agent's most recent poll (0 if never)."""
    return _last_poll[0]


def record_poll() -> None:
    """Mark the agent as having just polled (called by the poll endpoint)."""
    _last_poll[0] = time.time()


def is_desktop_agent_connected() -> bool:
    """Check if the desktop agent has polled recently."""
    return time.time() - _last_poll[0] < _CONNECTED_WINDOW_S


def send_desktop_command(cmd_type, params=None, timeout=30):
    """Queue a command for the desktop agent. Blocks until result or timeout."""
    cmd_id = str(uuid.uuid4())
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

    with command_queue_lock:
        command_queue[cmd_id] = cmd

    event.wait(timeout=timeout)

    with command_queue_lock:
        cmd = command_queue.pop(cmd_id, cmd)

    if not event.is_set():
        return None, 'Desktop agent timeout — is the agent running?'

    return cmd.get('result'), cmd.get('error')


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


def take_pending_commands() -> list:
    """Collect commands awaiting the agent, expiring stale ones."""
    pending = []
    now = time.time()
    with command_queue_lock:
        for cmd_id, cmd in list(command_queue.items()):
            if cmd['event'].is_set():
                continue  # already resolved
            if now - cmd['created_at'] > _COMMAND_TTL_S:
                cmd['error'] = 'Command expired (stale cleanup)'
                cmd['event'].set()
                continue
            pending.append({
                'id': cmd_id,
                'type': cmd['type'],
                'params': cmd['params'],
            })
    return pending


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
    'pending_commands_count',
    'record_poll',
    'resolve_results',
    'send_desktop_command',
    'take_pending_commands',
]
