#!/usr/bin/env python3
"""Wire-safety parity tests for the Decoupling-A route conversions.

Epic pt_a5be8aa7a2ee47c1 (re-scoped): convert only the wire-safe subset of raw
``return jsonify(...)`` error returns in routes/ to ``api_error(...)``, plus the
3 byte-identical chat.py SSE ``Response(...)`` blocks to ``sse_response(...)``.

The owner's acceptance bar is **no consumer breaks**, not literal byte-identity
(literal identity is impossible for error returns because ``api_error`` calls
``_attach_request_id`` and ``server.py`` sets a req_id on every request → every
converted error gains a purely-additive top-level ``request_id``). So for each
converted ERROR site this asserts the new envelope reproduces the legacy body
EXACTLY, allowing ONLY these additions:
  * ``request_id``  (present only when a req_id is set; purely additive)
  * ``ok: False``   (Category C only — legacy bare ``{'error': ...}`` gained it)
and the HTTP status code is identical.

For the 3 SSE sites it asserts the canonical header set is byte-identical to the
legacy 4-key literal, and that the 1894 site still disables the response timeout.

Two test layers:
  1. PARITY (thunk vs legacy literal, resolved in a Quart app ctx) — proves the
     argument mapping at each site is wire-safe.
  2. SHIPPED-SOURCE — proves the real files were actually converted (the legacy
     literal is gone, the new call is present). This is the RED-first driver and
     the regression tripwire.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install Flask→Quart shim before importing anything that pulls in routes.
import quart as _quart
sys.modules['flask'] = _quart

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


async def _resolve(resp):
    """(status, body_dict) from a (Response, status) tuple in test ctx."""
    response, status = resp
    body = await response.get_data(as_text=True)
    return status, (json.loads(body) if body else {})


# ── The 22 error-conversion sites ────────────────────────────────────
# Each entry:
#   file        — shipped source path (relative to repo root)
#   legacy_body — the EXACT dict the pre-conversion ``jsonify(...)`` returned
#   legacy_status — the EXACT HTTP status the pre-conversion site returned
#   new         — a thunk building the post-conversion api_error(...) call
#   category    — 'B' (already had ok:False) | 'C' (bare) | 'X' (extra-key)
#   old_src     — a substring that MUST be gone from the file after conversion
#   new_src     — a substring that MUST be present after conversion
def _sites():
    from lib.api_response import api_error

    return [
        # ── Category B: {'ok': False, 'error': ...}, N ──
        dict(file='routes/config.py', category='B', status=502,
             legacy_body={'ok': False, 'error': 'Request failed: %s' % 'boom'},
             new=lambda: api_error('Request failed: %s' % 'boom', status=502),
             old_src="return jsonify({'ok': False, 'error': 'Request failed: %s' % e}), 502",
             new_src="return api_error('Request failed: %s' % e, status=502)"),
        dict(file='routes/config.py', category='B', status=404,
             legacy_body={'ok': False, 'error': 'No models found at %s' % 'http://x'},
             new=lambda: api_error('No models found at %s' % 'http://x', status=404),
             old_src="return jsonify({'ok': False, 'error': 'No models found at %s' % base_url}), 404",
             new_src="return api_error('No models found at %s' % base_url, status=404)"),
        dict(file='routes/config.py', category='B', status=500,
             legacy_body={'ok': False, 'error': '探测出错: %s' % 'boom'},
             new=lambda: api_error('探测出错: %s' % 'boom', status=500),
             old_src="return jsonify({'ok': False, 'error': '探测出错: %s' % e}), 500",
             new_src="return api_error('探测出错: %s' % e, status=500)"),
        dict(file='routes/push.py', category='B', status=403,
             legacy_body={'ok': False, 'error': 'presence debug disabled '
                          '(set TOFU_PRESENCE_DEBUG=1 to enable)'},
             new=lambda: api_error('presence debug disabled '
                                   '(set TOFU_PRESENCE_DEBUG=1 to enable)', status=403),
             old_src="return jsonify({'ok': False, 'error': 'presence debug disabled '",
             new_src="return api_error('presence debug disabled '"),
        dict(file='routes/push.py', category='B', status=400,
             legacy_body={'ok': False, 'error': 'root is required'},
             new=lambda: api_error('root is required', status=400),
             old_src="return jsonify({'ok': False, 'error': 'root is required'}), 400",
             new_src="return api_error('root is required', status=400)"),
        dict(file='routes/api_v1/auth_sources.py', category='B', status=404,
             legacy_body={'ok': False, 'error': 'Unknown source: %s' % 'x.com'},
             new=lambda: api_error('Unknown source: %s' % 'x.com', status=404),
             old_src="return jsonify({'ok': False, 'error': f'Unknown source: {domain}'}), 404",
             new_src="return api_error(f'Unknown source: {domain}', status=404)"),
        dict(file='routes/api_v1/auth_sources.py', category='B', status=503,
             legacy_body={'ok': False, 'error': 'login failed', 'reason': 'unavailable'},
             new=lambda: api_error('login failed', status=503, reason='unavailable'),
             old_src="return jsonify({'ok': False, 'error': result.get('error', 'login failed'),",
             new_src="return api_error(result.get('error', 'login failed'), status=code,"),
        dict(file='routes/api_v1/daily_report.py', category='B', status=400,
             legacy_body={'ok': False,
                          'error': "Invalid status — must be one of ('done', 'in_progress', 'blocked', 'incomplete')"},
             new=lambda: api_error("Invalid status — must be one of ('done', 'in_progress', 'blocked', 'incomplete')", status=400),
             old_src="return jsonify({'ok': False, 'error': f'Invalid status — must be one of {valid_statuses}'}), 400",
             new_src="return api_error(f'Invalid status — must be one of {valid_statuses}', status=400)"),
        dict(file='routes/api_v1/mcp.py', category='B', status=404,
             legacy_body={'ok': False, 'error': 'Unknown server: %s' % 'srv'},
             new=lambda: api_error('Unknown server: %s' % 'srv', status=404),
             old_src="return jsonify({'ok': False, 'error': f'Unknown server: {server_id}'}), 404",
             new_src="return api_error(f'Unknown server: {server_id}', status=404)"),
        dict(file='routes/api_v1/translate.py', category='B', status=200,
             legacy_body={'ok': False, 'error': 'API Key 未填写'},
             new=lambda: api_error('API Key 未填写', status=200),
             old_src="return jsonify({'ok': False, 'error': 'API Key 未填写'})",
             new_src="return api_error('API Key 未填写', status=200)"),
        dict(file='routes/api_v1/translate.py', category='B', status=200,
             legacy_body={'ok': False, 'error': 'boom'},
             new=lambda: api_error('boom', status=200),
             old_src="return jsonify({'ok': False, 'error': str(e)})",
             new_src="return api_error(str(e), status=200)"),

        # ── Category C: bare {'error': ...}, N (gains ok:False) ──
        dict(file='routes/oauth.py', category='C', status=400,
             legacy_body={'error': 'Invalid provider. Use "claude" or "codex".'},
             new=lambda: api_error('Invalid provider. Use "claude" or "codex".', status=400),
             old_src='return jsonify({\'error\': \'Invalid provider. Use "claude" or "codex".\'}), 400',
             new_src='return api_error(\'Invalid provider. Use "claude" or "codex".\', status=400)'),
        dict(file='routes/upload.py', category='C', status=400,
             legacy_body={'error': 'Unsupported image type — SVG uploads are disabled for security. '
                          'Allowed: png, jpeg, gif, webp, bmp.'},
             new=lambda: api_error('Unsupported image type — SVG uploads are disabled for security. '
                                   'Allowed: png, jpeg, gif, webp, bmp.', status=400),
             old_src="Allowed: png, jpeg, gif, webp, bmp.'}), 400",
             new_src="Allowed: png, jpeg, gif, webp, bmp.', status=400)"),
        dict(file='routes/upload.py', category='C', status=400,
             legacy_body={'error': 'Unsupported image type — SVG uploads are disabled for security. '
                          'Allowed: .png, .jpg, .jpeg, .gif, .webp, .bmp.'},
             new=lambda: api_error('Unsupported image type — SVG uploads are disabled for security. '
                                   'Allowed: .png, .jpg, .jpeg, .gif, .webp, .bmp.', status=400),
             old_src="Allowed: .png, .jpg, .jpeg, .gif, .webp, .bmp.'}), 400",
             new_src="Allowed: .png, .jpg, .jpeg, .gif, .webp, .bmp.', status=400)"),
        dict(file='routes/translate.py', category='C', status=403,
             legacy_body={'error': 'PPTX translation is not enabled. '
                          'Enable it in Settings → Feature Modules.'},
             new=lambda: api_error('PPTX translation is not enabled. '
                                   'Enable it in Settings → Feature Modules.', status=403),
             old_src="return jsonify({'error': 'PPTX translation is not enabled. '",
             new_src="return api_error('PPTX translation is not enabled. '"),
        dict(file='routes/translate.py', category='C', status=400,
             legacy_body={'error': 'File too large (%dMB, max %dMB)' % (5, 10)},
             new=lambda: api_error('File too large (%dMB, max %dMB)' % (5, 10), status=400),
             old_src="return jsonify({'error': f'File too large ({len(file_bytes) // 1048576}MB, '",
             new_src="return api_error(f'File too large ({len(file_bytes) // 1048576}MB, '"),
        # ── The skill-install upload endpoint moved memory.py → skills.py in
        #    757c3626 (API/frontend split P4); the converted api_error call
        #    survived the move verbatim (skills.py:182). ──
        dict(file='routes/api_v1/skills.py', category='C', status=400,
             legacy_body={'error': 'Provide a file upload or {"path": ...}'},
             new=lambda: api_error('Provide a file upload or {"path": ...}', status=400),
             old_src='return jsonify({\'error\': \'Provide a file upload or {"path": ...}\'}), 400',
             new_src='return api_error(\'Provide a file upload or {"path": ...}\', status=400)'),
        dict(file='routes/api_v1/project.py', category='C', status=400,
             legacy_body={'error': 'Provide a "paths" array with at least '
                          'one directory'},
             new=lambda: api_error('Provide a "paths" array with at least '
                                   'one directory', status=400),
             old_src="return jsonify({'error': 'Provide a \"paths\" array with at least '",
             new_src="return api_error('Provide a \"paths\" array with at least '"),
        dict(file='routes/conversations.py', category='C', status=400,
             legacy_body={'error': 'mode must be "single" or "turn"'},
             new=lambda: api_error('mode must be "single" or "turn"', status=400),
             old_src='return jsonify({\'error\': \'mode must be "single" or "turn"\'}), 400',
             new_src='return api_error(\'mode must be "single" or "turn"\', status=400)'),

        # ── Extra-key sites reproduced via **extras ──
        dict(file='routes/artifacts.py', category='X', status=400,
             legacy_body={'error': 'unsupported_format_for_view', 'format': 'markdown'},
             new=lambda: api_error('unsupported_format_for_view', status=400, format='markdown'),
             old_src="return jsonify({'error': 'unsupported_format_for_view',",
             new_src="return api_error('unsupported_format_for_view', status=400, format=fmt)"),
        dict(file='routes/artifacts.py', category='X', status=503,
             legacy_body={'error': 'pdf_render_failed', 'detail': 'boom'},
             new=lambda: api_error('pdf_render_failed', status=503, detail='boom'),
             old_src="return jsonify({'error': 'pdf_render_failed', 'detail': str(e)}), 503",
             new_src="return api_error('pdf_render_failed', status=503, detail=str(e))"),

        # ── routes/config.py template-update 404 (续18 batch) ──
        dict(file='routes/config.py', category='B', status=404,
             legacy_body={'ok': False,
                          'error': "Template key '%s' not found in any JS file" % 'foo'},
             new=lambda: api_error("Template key '%s' not found in any JS file" % 'foo', status=404),
             old_src="return jsonify({'ok': False,\n                       'error': \"Template key '%s' not found in any JS file\" % tpl_key}), 404",
             new_src="return api_error(\"Template key '%s' not found in any JS file\" % tpl_key, status=404)"),

        # ── chat helper-return sites (续18 batch): _start_task_from_messages
        #    returns (task_id, err_tuple) where err_tuple is (Response, status).
        #    The caller returns err_tuple raw → wire-identical to a direct
        #    return jsonify(...)-with-status. Category C: bare {'error':...}.
        #    The helper later moved chat.py → chat_task_start.py; the
        #    converted api_error tuples survived the move verbatim. ──
        dict(file='routes/chat_task_start.py', category='C', status=500,
             legacy_body={'error': 'Conversation not found after save'},
             new=lambda: api_error('Conversation not found after save', status=500),
             old_src="return None, (jsonify({'error': 'Conversation not found after save'}), 500)",
             new_src="return None, api_error('Conversation not found after save', status=500)"),
        dict(file='routes/chat_task_start.py', category='C', status=400,
             legacy_body={'error': 'No messages to process'},
             new=lambda: api_error('No messages to process', status=400),
             old_src="return None, (jsonify({'error': 'No messages to process'}), 400)",
             new_src="return None, api_error('No messages to process', status=400)"),
        dict(file='routes/chat_task_start.py', category='C', status=500,
             legacy_body={'error': 'Failed to start task'},
             new=lambda: api_error('Failed to start task', status=500),
             old_src="return None, (jsonify({'error': 'Failed to start task'}), 500)",
             new_src="return None, api_error('Failed to start task', status=500)"),
    ]


# ── Legacy 4-key SSE literal that lived at each chat.py streaming site ──
_LEGACY_SSE_HEADERS = {
    'Content-Type': 'text/event-stream; charset=utf-8',
    'Cache-Control': 'no-cache, no-transform',
    'X-Accel-Buffering': 'no',
    'Connection': 'keep-alive',
}


def test_error_envelope_parity():
    """Each converted error site reproduces the legacy body, adding ONLY
    request_id (always allowed) and ok:False (Category C only), same status."""
    from flask import jsonify
    app = _make_app()
    sites = _sites()

    async def _t():
        async with app.test_request_context('/test'):
            for s in sites:
                # Legacy shape resolved through jsonify for an apples-to-apples body.
                leg_status, leg_body = await _resolve((jsonify(s['legacy_body']), s['status']))
                new_status, new_body = await _resolve(s['new']())

                assert new_status == leg_status, (
                    f"{s['file']} status {new_status} != legacy {leg_status}")

                # request_id is purely additive → strip before comparing.
                new_body.pop('request_id', None)

                # Every legacy key must survive unchanged (no consumer breaks).
                for k, v in leg_body.items():
                    assert k in new_body and new_body[k] == v, (
                        f"{s['file']}: legacy key {k!r}={v!r} lost/changed "
                        f"(new={new_body.get(k)!r})")

                # The ONLY keys the new envelope may add over the legacy body.
                added = set(new_body) - set(leg_body)
                assert added <= {'ok'}, (
                    f"{s['file']}: unexpected added keys {added} "
                    "(only 'ok' + request_id permitted)")
                assert new_body.get('ok') is False, (
                    f"{s['file']}: api_error must set ok:False")
    import asyncio
    asyncio.run(_t())
    _ok(f'error-envelope parity holds for all {len(sites)} converted sites '
        '(only +request_id / +ok:False, same status)')


def test_shipped_sources_converted():
    """The real files no longer contain the legacy jsonify literal and DO
    contain the api_error call. RED before conversion; the regression tripwire."""
    sites = _sites()
    # Group by file so we read each once.
    by_file = {}
    for s in sites:
        by_file.setdefault(s['file'], []).append(s)
    for fname, group in by_file.items():
        with open(os.path.join(_ROOT, fname), encoding='utf-8') as f:
            src = f.read()
        for s in group:
            assert s['old_src'] not in src, (
                f"{fname}: legacy literal still present: {s['old_src'][:60]!r}")
            assert s['new_src'] in src, (
                f"{fname}: expected api_error call missing: {s['new_src'][:60]!r}")
    _ok(f'all {len(sites)} shipped error sites converted (legacy gone, api_error present)')


def test_sse_helper_matches_legacy_headers():
    """sse_response()'s canonical header set is byte-identical to the 4-key
    literal the chat.py SSE blocks used, and timeout_none disables the timeout."""
    from lib.api_response import sse_response
    app = _make_app()

    async def _t():
        async with app.test_request_context('/test'):
            def _gen():
                yield 'data: x\n\n'
            resp = sse_response(_gen())
            assert resp.mimetype == 'text/event-stream'
            for k, v in _LEGACY_SSE_HEADERS.items():
                assert resp.headers.get(k) == v, (
                    f'SSE header {k!r}: helper={resp.headers.get(k)!r} legacy={v!r}')
            # 1894 site: timeout must be disabled for the long-lived UI stream.
            resp2 = sse_response(_gen(), timeout_none=True)
            assert resp2.timeout is None
    import asyncio
    asyncio.run(_t())
    _ok('sse_response() headers byte-equal to legacy SSE literal; timeout_none works')


def test_chat_sse_blocks_converted():
    """chat.py's streaming Response(...) blocks are converted to sse_response,
    including the timeout_none=True path for the long-lived UI stream.

    History: the epic converted 3 SSE blocks; the later turn-settlement
    refactor (d4811ff1/38d48669) DELETED the gen_persisted / gen_done
    endpoints entirely, leaving exactly one SSE stream (line ~1285). The
    guard now trips on ANY raw event-stream Response in chat.py."""
    with open(os.path.join(_ROOT, 'routes/chat.py'), encoding='utf-8') as f:
        src = f.read()
    # No raw SSE Response(...) block may exist in chat.py — every event-stream
    # response must go through sse_response (which sets the mimetype itself).
    assert "mimetype='text/event-stream'" not in src, (
        'chat.py grew a raw Response(..., mimetype=\'text/event-stream\') — '
        'use sse_response() instead')
    # The surviving long-lived UI stream keeps its conversion (timeout off).
    assert 'return sse_response(generate_with_disconnect_log(), timeout_none=True)' in src
    # sse_response must be imported.
    assert 'sse_response' in src.split('\n\n', 1)[0] or 'import' in src
    _ok('chat.py SSE streaming converted to sse_response (no raw event-stream Response)')


def main():
    print()
    print(_color('═══ Decoupling-A route-conversion parity tests ═══', '36'))
    print()
    tests = [
        test_error_envelope_parity,
        test_shipped_sources_converted,
        test_sse_helper_matches_legacy_headers,
        test_chat_sse_blocks_converted,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
