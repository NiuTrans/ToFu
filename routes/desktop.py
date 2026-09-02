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
# object (B0 §5.3 / ). Auth order (RWA P4a 约束②第三条):
# A remote caller must present an owner-scoped agents:bridge credential. The
# packaged app's in-process capability is accepted only on this desktop poll.

# The queue lives below routes so tool handlers never import delivery code.
from lib.desktop import (  # noqa: E402
    register_agent,
    resolve_results,
    resolve_streams,
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
    body = await async_parse_body()
    agent_frame = body.get('agent')
    if not isinstance(agent_frame, dict) or not agent_frame.get('agent_id'):
        return jsonify({
            'error': 'desktop_agent_identity_required',
            'hint': 'pair this device again with a current agent',
        }), 400
    agent_id = str(agent_frame['agent_id'])
    register_agent(
        agent_id,
        agent_frame,
        user_id=_bridge_user,
        key_id=_bridge_key,
    )

    # Settle results only after binding the poll to its authenticated owner
    # and stable device identity.
    resolved = resolve_results(
        body.get('results', []), agent_id=agent_id, user_id=_bridge_user)
    if resolved:
        logger.info('[Desktop] resolved %d command results', resolved)
    # 1a) RWA P2: streamed-command output frames (reassembly dedupes by seq)
    stream_frames = resolve_streams(
        body.get('streams', []), agent_id=agent_id, user_id=_bridge_user)
    if stream_frames:
        logger.debug('[Desktop] ingested %d stream chunks', stream_frames)

    # 2) Long-poll for pending commands. Async-native wait releases the worker
    #    thread for the window (see lib.desktop.bridge.take_pending_commands_async)
    #    and hands the agent a command the instant it is queued.
    pending = await take_pending_commands_async(
        agent_id=agent_id, user_id=_bridge_user)
    if pending:
        logger.info('[Desktop] sending %d commands to agent %s: %s',
                    len(pending), agent_id,
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
