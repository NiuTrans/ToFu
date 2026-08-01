"""lib.desktop — desktop-agent bridge command queue + result helpers.

Relocated here (2026-06) from ``routes/desktop.py`` to break the
``lib → routes`` circular-import coupling: ``lib/tools/registry`` and
``lib/tasks_pkg/handlers/misc`` reached UP into ``routes.desktop`` for
``send_desktop_command`` / ``format_desktop_result`` /
``is_desktop_agent_connected``. The command queue is plain in-process
state with no Flask dependency, so it belongs in lib.

Dependencies now flow ``routes → lib`` only. ``routes/desktop.py`` keeps
the HTTP poll/tool-exec endpoints and re-exports these names (and the
mutable ``_commands`` / ``_last_poll_time`` state) for backward
compatibility.
"""

from lib.desktop.bridge import (
    command_queue,
    command_queue_lock,
    enqueue_desktop_command,
    format_desktop_result,
    get_command_stream,
    get_frames,
    is_desktop_agent_connected,
    last_poll_time,
    list_agents,
    note_v1_poll,
    online_agents,
    pending_commands_count,
    record_poll,
    register_agent,
    resolve_results,
    resolve_streams,
    send_desktop_command,
    take_pending_commands,
    take_pending_commands_async,
)

__all__ = [
    'command_queue',
    'command_queue_lock',
    'enqueue_desktop_command',
    'format_desktop_result',
    'get_command_stream',
    'get_frames',
    'is_desktop_agent_connected',
    'last_poll_time',
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
