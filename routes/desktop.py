"""
Desktop Agent Bridge — Server-side endpoint for local machine control.

Mirrors the architecture of routes/browser.py:
  - LLM calls tool → command queued
  - Desktop Agent polls /api/desktop/poll → picks up commands, returns results
"""

from quart import Blueprint, jsonify

from lib.log import get_logger
from lib.request_parser import async_parse_body
from routes._bridge_caller import (
    bridge_unauthorized as _bridge_unauthorized,
    resolve_bridge_caller as _resolve_bridge_caller,
)

logger = get_logger(__name__)

desktop_bp = Blueprint('desktop', __name__)

# Bridge caller resolution lives in routes/_bridge_caller.py, shared with
# the browser bridge so the two identity layers are literally the same
# object (B0 §5.3 / pt_3ba97339b4024fb4). Auth order (RWA P4a 约束②第三条):
# TOFU_BRIDGE_SECRET unset → open legacy (True, '', ''); global-secret match
# → legacy super-user (True, '', ''); per-user agents:bridge token →
# (True, user_id, key_id) with commands scoped to that user; else 401.

# ══════════════════════════════════════════════════════════
#  Command Queue (moved to lib/desktop/bridge.py, 2026-06)
# ══════════════════════════════════════════════════════════
# The queue + RPC helpers moved DOWN into lib so tool handlers can drive the
# agent without importing the routes package (lib→routes circular break).
# Re-exported here for back-compat: external callers still do
# ``from routes.desktop import send_desktop_command, format_desktop_result,
# is_desktop_agent_connected``.
from lib.desktop import (  # noqa: F401,E402
    command_queue as _commands,
    format_desktop_result,
    is_desktop_agent_connected,
    note_v1_poll,
    pending_commands_count,
    record_poll,
    register_agent,
    resolve_results,
    resolve_streams,
    send_desktop_command,
    take_pending_commands,
    take_pending_commands_async,
)


# ══════════════════════════════════════════════════════════
#  Poll Endpoint — Desktop Agent calls this
# ══════════════════════════════════════════════════════════

@desktop_bp.route('/api/desktop/poll', methods=['POST'])
async def desktop_poll():
    _auth_ok, _bridge_user, _bridge_key = _resolve_bridge_caller('desktop')
    if not _auth_ok:
        return _bridge_unauthorized()
    record_poll()

    # 1) Resolve any results from the agent
    body = await async_parse_body()
    resolved = resolve_results(body.get('results', []))
    if resolved:
        logger.info('[Desktop] resolved %d command results', resolved)
    # 1a) RWA P2: streamed-command output frames (reassembly dedupes by seq)
    stream_frames = resolve_streams(body.get('streams', []))
    if stream_frames:
        logger.debug('[Desktop] ingested %d stream chunks', stream_frames)

    # 1b) v2 registration frame (RWA P0): the agent announces its stable
    #     agent_id + machine meta; v1 agents send no frame and stay on the
    #     anonymous legacy fallback.
    agent_frame = body.get('agent')
    agent_id = None
    v1 = True
    if isinstance(agent_frame, dict) and agent_frame.get('agent_id'):
        agent_id = str(agent_frame['agent_id'])
        register_agent(agent_id, agent_frame,
                       user_id=_bridge_user, key_id=_bridge_key)
        v1 = False
    else:
        note_v1_poll()

    # 2) Long-poll for pending commands. Async-native wait releases the worker
    #    thread for the window (see lib.desktop.bridge.take_pending_commands_async)
    #    and hands the agent a command the instant it is queued.
    pending = await take_pending_commands_async(
        agent_id=agent_id, v1=v1, user_id=_bridge_user)
    if pending:
        logger.info('[Desktop] sending %d commands to agent %s: %s',
                    len(pending), agent_id or 'v1(legacy)',
                    [c['type'] for c in pending])
    return jsonify({'commands': pending})


# Status endpoint moved to routes/api_v1/desktop.py — read state via the
# lib.desktop helpers (last_poll_time / pending_commands_count /
# is_desktop_agent_connected).
#
# Tool execution lives with the other task-loop handlers:
# lib/tasks_pkg/handlers/misc/_agents.py::_handle_desktop_tool (registered
# against DESKTOP_TOOL_NAMES via tool_registry). The wire contract is that
# the command ``type`` IS the full tool name — see
# tests/test_desktop_cmdtype_parity.py.
