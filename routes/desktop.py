"""
Desktop Agent Bridge — Server-side endpoint for local machine control.

Mirrors the architecture of routes/browser.py:
  - LLM calls tool → command queued
  - Desktop Agent polls /api/desktop/poll → picks up commands, returns results
"""

import hmac
import json
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger
from lib.request_parser import parse_body

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
#  Command Queue (mirrors lib/browser.py pattern)
# ══════════════════════════════════════════════════════════

_commands = {}
_commands_lock = threading.Lock()
_last_poll_time = 0


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

    with _commands_lock:
        _commands[cmd_id] = cmd

    event.wait(timeout=timeout)

    with _commands_lock:
        cmd = _commands.pop(cmd_id, cmd)

    if not event.is_set():
        return None, 'Desktop agent timeout — is the agent running?'

    return cmd.get('result'), cmd.get('error')


def is_desktop_agent_connected():
    """Check if the desktop agent has polled recently."""
    return time.time() - _last_poll_time < 15


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


# ══════════════════════════════════════════════════════════
#  Poll Endpoint — Desktop Agent calls this
# ══════════════════════════════════════════════════════════

@desktop_bp.route('/api/desktop/poll', methods=['POST'])
def desktop_poll():
    global _last_poll_time
    if not _check_bridge_auth('desktop'):
        return _bridge_unauthorized()
    _last_poll_time = time.time()

    # 1) Resolve any results from the agent
    body = parse_body()
    results = body.get('results', [])
    resolved = 0

    for r in results:
        cmd_id = r.get('id', '')
        if not cmd_id:
            continue
        with _commands_lock:
            cmd = _commands.get(cmd_id)
        if cmd:
            cmd['result'] = r.get('result')
            cmd['error'] = r.get('error')
            cmd['event'].set()
            resolved += 1

    if resolved:
        logger.info('[Desktop] resolved %d command results', resolved)

    # 2) Collect pending commands for the agent
    pending = []
    now = time.time()
    with _commands_lock:
        for cmd_id, cmd in list(_commands.items()):
            if cmd['event'].is_set():
                continue  # already resolved
            if now - cmd['created_at'] > 90:
                cmd['error'] = 'Command expired (stale cleanup)'
                cmd['event'].set()
                continue
            pending.append({
                'id': cmd_id,
                'type': cmd['type'],
                'params': cmd['params'],
            })

    if pending:
        logger.info('[Desktop] sending %d commands to agent: %s',
                    len(pending), [c['type'] for c in pending])
    return jsonify({'commands': pending})


# Status endpoint moved to routes/api_v1/desktop.py — read state via the
# module-level `_commands`, `_last_poll_time`, and `is_desktop_agent_connected`
# helpers above.


# ══════════════════════════════════════════════════════════
#  Tool Execution — Called by LLM orchestrator
# ══════════════════════════════════════════════════════════

def execute_desktop_tool(fn_name, fn_args):
    """Execute a desktop tool call. Returns string result for LLM."""

    if not is_desktop_agent_connected():
        logger.warning('[Desktop] tool %s called but agent not connected', fn_name)
        return 'Error: Desktop Agent not connected. Start it with: python lib/desktop_agent.py --server http://your-server:5000'

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
