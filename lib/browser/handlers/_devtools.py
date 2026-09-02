"""Model-facing DevTools Bridge boundary.

The extension owns CDP attachment and transient debugger handles.  This module
owns capability negotiation, argument bounds, owner-scoped URL re-authorization,
secret redaction and the model-context budget.  Raw console entries, object
handles and script bodies are never persisted here.
"""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from lib.browser.network_evidence import redact_url, redact_value
from lib.log import get_logger

logger = get_logger(__name__)

_ACTIONS = frozenset({
    'console_read', 'console_clear', 'context_list',
    'evaluate', 'inspect',
    'debug_start', 'debug_state', 'debug_stop',
    'breakpoint_set', 'breakpoint_remove',
    'pause', 'resume', 'step_over', 'step_into', 'step_out',
    'frame_evaluate', 'script_source',
})
_DEBUG_ACTIONS = frozenset({
    'debug_start', 'debug_state', 'debug_stop',
    'breakpoint_set', 'breakpoint_remove',
    'pause', 'resume', 'step_over', 'step_into', 'step_out',
    'frame_evaluate', 'script_source',
})
_MAX_OUTPUT_CHARS = 60_000


def _bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _url_is_visible(owner_user_id: str, value: str) -> bool:
    url = str(value or '').strip()
    if not url:
        return True
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        return False
    # DevTools uses these non-network source identifiers for bundles created
    # by the already-authorized page. They are not alternate fetch targets.
    if scheme in ('webpack', 'blob', 'data'):
        return True
    if scheme not in ('http', 'https'):
        return False
    try:
        from lib.browser.access import is_read_allowed
        return is_read_allowed(owner_user_id, url)
    except Exception as exc:
        logger.debug('[BrowserDevTools] URL policy failed closed: %s', exc)
        return False


def _redact_devtools_urls(value):
    """Redact URL query credentials at every nested CDP URL field."""
    if isinstance(value, dict):
        return {
            key: (
                redact_url(child)
                if str(key).lower() in ('url', 'sourceurl')
                and isinstance(child, str)
                else _redact_devtools_urls(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_devtools_urls(child) for child in value]
    return value


def sanitize_devtools_result(payload: dict, *, owner_user_id: str) -> dict:
    """Filter nested source URLs, then redact credential-shaped values."""
    value = dict(payload or {})
    for key in ('entries', 'contexts', 'scripts', 'targets'):
        rows = value.get(key)
        if isinstance(rows, list):
            value[key] = [
                row for row in rows
                if isinstance(row, dict)
                and _url_is_visible(
                    owner_user_id,
                    row.get('url') or row.get('origin') or '')
            ]
    paused = value.get('paused')
    if isinstance(paused, dict) and isinstance(paused.get('callFrames'), list):
        paused = dict(paused)
        paused['callFrames'] = [
            frame for frame in paused['callFrames']
            if isinstance(frame, dict)
            and _url_is_visible(owner_user_id, frame.get('url') or '')
        ]
        value['paused'] = paused
    state = value.get('state')
    if isinstance(state, dict):
        value['state'] = sanitize_devtools_result(
            state, owner_user_id=owner_user_id)
    script = value.get('script')
    if isinstance(script, dict) and not _url_is_visible(
            owner_user_id, script.get('url') or ''):
        value.pop('source', None)
        value['sourceDenied'] = True
    return _redact_devtools_urls(redact_value(value))


def _handle_devtools(fn_args, runtime):
    action = str(fn_args.get('action') or 'console_read').strip().lower()
    if action not in _ACTIONS:
        return 'Error: unknown DevTools action.'
    try:
        from lib.browser.protocol import (
            BrowserCapability,
            BrowserUpgradeRequired,
            require_capabilities,
        )
        required = [BrowserCapability.DEVTOOLS_CONSOLE]
        if action in _DEBUG_ACTIONS:
            required.append(BrowserCapability.JS_DEBUGGER)
        require_capabilities(runtime.client_id, required)
    except BrowserUpgradeRequired as exc:
        return (
            'Error: browser extension upgrade required for DevTools Bridge; '
            f'missing capabilities: {", ".join(exc.missing)}')

    params = dict(fn_args or {})
    params['action'] = action
    params['observeMs'] = _bounded_int(
        params.get('observeMs'), default=250, minimum=50, maximum=5_000)
    params['maxDepth'] = _bounded_int(
        params.get('maxDepth'), default=3, minimum=0, maximum=6)
    params['sessionTtlMs'] = _bounded_int(
        params.get('sessionTtlMs'), default=60_000,
        minimum=10_000, maximum=120_000)
    if 'expression' in params:
        params['expression'] = str(params.get('expression') or '')[:50_000]
    if 'condition' in params:
        params['condition'] = str(params.get('condition') or '')[:10_000]
    for key, limit in (
        ('sourceUrl', 4_000), ('sessionId', 256), ('scriptId', 256),
        ('breakpointId', 512), ('callFrameId', 512),
    ):
        if key in params:
            params[key] = str(params.get(key) or '')[:limit]
    for key in ('contextId', 'lineNumber', 'columnNumber'):
        if key in params:
            params[key] = _bounded_int(
                params.get(key), default=0, minimum=0, maximum=10_000_000)

    result, error = runtime.send('devtools', params, timeout=35)
    if error:
        return f'Error using DevTools Bridge: {error}'
    if not isinstance(result, dict):
        return 'Error: browser returned an invalid DevTools result.'
    final_url = str(result.get('url') or '')
    if not final_url or not _url_is_visible(runtime.owner_user_id, final_url):
        return 'Error: DevTools result was denied by domain policy.'
    sanitized = sanitize_devtools_result(
        result, owner_user_id=runtime.owner_user_id)
    rendered = json.dumps(sanitized, ensure_ascii=False, indent=2)
    if len(rendered) > _MAX_OUTPUT_CHARS:
        rendered = rendered[:_MAX_OUTPUT_CHARS] + '\n…[DevTools output truncated]'
    return (
        f'DevTools {action} · {redact_url(final_url)}\n'
        f'{rendered}'
    )


__all__ = ['_handle_devtools', 'sanitize_devtools_result']
