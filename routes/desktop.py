"""
Desktop Agent Bridge — Server-side endpoint for local machine control.

Mirrors the architecture of routes/browser.py:
  - LLM calls tool → command queued
  - Desktop Agent polls /api/desktop/poll → picks up commands, returns results
"""

import hmac
import threading

from flask import Blueprint, jsonify, request

from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger
from lib.request_parser import async_parse_body

logger = get_logger(__name__)

desktop_bp = Blueprint('desktop', __name__)


# ── One-time startup audit when bridge auth is configured ──
_AUDIT_LOCK = threading.Lock()
_AUDIT_LOGGED = False


def _maybe_audit_enforcement_on() -> None:
    global _AUDIT_LOGGED
    if _AUDIT_LOGGED:
        return
    with _AUDIT_LOCK:
        if _AUDIT_LOGGED:
            return
        _AUDIT_LOGGED = True
        try:
            audit_log('config_change',
                      param='bridge_auth_enforcement',
                      old='permissive (Phase A)',
                      new='enforcing (Phase C)',
                      reason='TOFU_BRIDGE_SECRET configured — desktop bridge '
                             'now rejects requests without a matching X-Bridge-Secret',
                      approved_by='user')
        except Exception as e:
            logger.debug('[Desktop] startup audit_log failed: %s', e)


def _resolve_bridge_caller(kind: str = 'desktop'):
    """Resolve the poll caller → ``(ok, user_id, key_id)``.

    Auth order (RWA P4a 约束②第三条):
      * TOFU_BRIDGE_SECRET unset → open legacy ``(True, '', '')``;
      * header matches the global secret → legacy super-user
        ``(True, '', '')``;
      * else the header is tried as a per-user API key carrying the
        ``agents:bridge`` scope → ``(True, user_id, key_id)`` — the
        agent's commands are then scoped to that user;
      * otherwise rejected (callers must 401).
    """
    expected = (getenv_compat('TOFU_BRIDGE_SECRET') or '').strip()
    if not expected:
        return True, '', ''
    _maybe_audit_enforcement_on()
    provided = request.headers.get('X-Bridge-Secret', '')
    if provided and hmac.compare_digest(provided, expected):
        return True, '', ''
    if provided:
        try:
            from lib.api_keys import validate_token
            ctx = validate_token(provided)
        except Exception as e:
            logger.debug('[Desktop] bridge token validation failed: %s', e)
            ctx = None
        if ctx is not None and 'agents:bridge' in getattr(ctx, 'scopes', ()): 
            return True, (getattr(ctx, 'user_id', '') or ''), ctx.key_id
    try:
        audit_log('bridge_auth_fail',
                  kind=kind,
                  path=request.path,
                  ip=request.remote_addr,
                  has_header=bool(provided),
                  ua=(request.user_agent.string or '')[:120])
    except Exception as _aerr:
        logger.debug('[Desktop] audit_log bridge_auth_fail failed: %s', _aerr)
    logger.warning('[Desktop] bridge auth rejected from %s on %s (header=%s)',
                   request.remote_addr, request.path, 'present' if provided else 'missing')
    return False, '', ''


def _check_bridge_auth(kind: str = 'desktop') -> bool:
    """Back-compat wrapper — see :func:`_resolve_bridge_caller`."""
    ok, _uid, _kid = _resolve_bridge_caller(kind)
    return ok


def _bridge_unauthorized():
    """Return a uniform 401 JSON envelope for bridge auth failures."""
    return jsonify({
        'error': 'bridge_auth_required',
        'hint': 'set X-Bridge-Secret header to match TOFU_BRIDGE_SECRET',
    }), 401

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
