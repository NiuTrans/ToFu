"""
Desktop Agent Bridge — Server-side endpoint for local machine control.

Mirrors the architecture of routes/browser.py:
  - LLM calls tool → command queued
  - Desktop Agent polls /api/desktop/poll → picks up commands, returns results
"""

import hmac
import json
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


def _check_bridge_auth(kind: str = 'desktop') -> bool:
    """Verify the optional X-Bridge-Secret header.

    Behaviour mirrors routes.browser._check_bridge_auth:
      * If TOFU_BRIDGE_SECRET is unset →
        return True (auth disabled).
      * Otherwise return True only on a timing-safe header match;
        callers must abort(401) when this returns False.
    """
    expected = (getenv_compat('TOFU_BRIDGE_SECRET') or '').strip()
    if not expected:
        return True
    _maybe_audit_enforcement_on()
    provided = request.headers.get('X-Bridge-Secret', '')
    if provided and hmac.compare_digest(provided, expected):
        return True
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
    return False


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
    pending_commands_count,
    record_poll,
    resolve_results,
    send_desktop_command,
    take_pending_commands,
    take_pending_commands_async,
)


# ══════════════════════════════════════════════════════════
#  Poll Endpoint — Desktop Agent calls this
# ══════════════════════════════════════════════════════════

@desktop_bp.route('/api/desktop/poll', methods=['POST'])
async def desktop_poll():
    if not _check_bridge_auth('desktop'):
        return _bridge_unauthorized()
    record_poll()

    # 1) Resolve any results from the agent
    body = await async_parse_body()
    resolved = resolve_results(body.get('results', []))
    if resolved:
        logger.info('[Desktop] resolved %d command results', resolved)

    # 2) Long-poll for pending commands. Async-native wait releases the worker
    #    thread for the window (see lib.desktop.bridge.take_pending_commands_async)
    #    and hands the agent a command the instant it is queued.
    pending = await take_pending_commands_async()
    if pending:
        logger.info('[Desktop] sending %d commands to agent: %s',
                    len(pending), [c['type'] for c in pending])
    return jsonify({'commands': pending})


# Status endpoint moved to routes/api_v1/desktop.py — read state via the
# lib.desktop helpers (last_poll_time / pending_commands_count /
# is_desktop_agent_connected).


# ══════════════════════════════════════════════════════════
#  Tool Execution — Called by LLM orchestrator
# ══════════════════════════════════════════════════════════

def execute_desktop_tool(fn_name, fn_args):
    """Execute a desktop tool call. Returns string result for LLM."""

    if not is_desktop_agent_connected():
        logger.warning('[Desktop] tool %s called but agent not connected', fn_name)
        return ('Error: Desktop Agent not connected. In the Tofu desktop app, '
                'enable "Computer Control" from the tray menu — or run the agent '
                'manually: python -m lib.desktop_agent --server http://your-server:5000 '
                '--allow-gui')

    # Map LLM tool names to agent command types
    cmd_type = fn_name  # e.g. "desktop_list_files"
    timeout = fn_args.pop('_timeout', 30)

    logger.info('[Desktop] executing tool %s (timeout=%ds)', fn_name, timeout)
    result, error = send_desktop_command(cmd_type, fn_args, timeout=timeout)

    if error:
        logger.error('[Desktop] tool %s error: %s', fn_name, error)
        return f'Error: Desktop Agent error: {error}'

    if result is None:
        return 'Error: Desktop Agent returned empty result'

    if isinstance(result, dict):
        if result.get('error'):
            return f'Error: {result["error"]}'

        # Special formatting for common results
        if 'entries' in result:
            # File listing
            lines = [f'{result.get("path", "")} ({result.get("total", 0)} items):\n']
            for e in result['entries'][:100]:
                tag = '[DIR]' if e['type'] == 'dir' else '[FILE]'
                size = f' ({e["size"]:,}B)' if e.get('size') is not None else ''
                lines.append(f'  {tag} {e["name"]}{size}  {e.get("modified", "")}')
            return '\n'.join(lines)

        if 'content' in result and 'path' in result:
            # File content
            return f'{result["path"]} ({result.get("size", 0):,} bytes):\n\n{result["content"]}'

        if 'base64' in result:
            # Screenshot — return metadata, actual image handled separately
            return f'Desktop screenshot: {result.get("width")}x{result.get("height")} ({result.get("size_bytes", 0):,} bytes JPEG)'

        if 'stdout' in result:
            # Command output
            out = result['stdout']
            err = result.get('stderr', '')
            code = result.get('exit_code', 0)
            parts = []
            if out:
                parts.append(out)
            if err:
                parts.append(f'\n[stderr]\n{err}')
            if code != 0:
                parts.append(f'\n[exit code: {code}]')
            return ''.join(parts) if parts else '(no output)'

        if 'processes' in result:
            # Process list
            lines = ['PID     CPU%   MEM(MB)  STATUS    NAME']
            for p in result['processes']:
                lines.append(f'{p["pid"]:<8}{p["cpu"]:<7}{p["memory_mb"]:<9}{p["status"]:<10}{p["name"]}')
            return '\n'.join(lines)

    return json.dumps(result, ensure_ascii=False, indent=2)
