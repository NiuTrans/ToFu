#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/chat.py
envelope migration (api-contract epic pt_931e16c4, batch 11).

11 ad-hoc sites. One of them — ``GET /api/v1/chat/active`` — is a bare
top-level ARRAY with first-party consumers, so it takes the two-sided
coordinated path, with one extra subtlety the earlier bare-array batches
did not have:

  * ``Api.chat.active()`` is a PROBE-shaped consumer: its callers
    (cross_tab_sync stale-adopt, send-pipeline reconnect) distinguish
    "server answered ZERO tasks" ([]) from "probe failed" (null). The
    seam's unwrap must therefore PRESERVE null — turning a failed probe
    into [] would let a network blip masquerade as "no tasks running"
    and trigger static-adopt decisions on false evidence.
  * ``Api.chat.activeResponse()`` deliberately returns the raw Response
    (the caller inspects .ok) — its sole caller (main_init_tasks.js)
    unwraps ``.items`` with an ``Array.isArray`` fallback itself.

The other 10: payload passthroughs → api_ok; the admission-control 503
and SSE-cap 429 both use the shared closed-kind ``api_typed_error`` boundary.
The 429 site keeps its manual Retry-After header — guarded by the
shipped-source needle.

Layers: PARITY + COORDINATION + SHIPPED-SOURCE, mirroring batches 1-10.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart
sys.modules.setdefault('flask', _quart)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'routes', 'chat.py')
_API_JS = os.path.join(_ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')
_INIT_JS = _API_JS

pytestmark = pytest.mark.unit


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


async def _resolve(resp):
    response, status = resp
    body = await response.get_data(as_text=True)
    return status, (json.loads(body) if body else {})


def _sites():
    from lib.api_response import api_ok, api_typed_error
    from lib.error_envelope import make_envelope
    capacity_env = make_envelope(
        'server_busy', detail='Server is at task capacity. Retry shortly.',
        source='routes.chat.admission', extensions={'retry_after_s': 3})
    sse_env = make_envelope(
        'ratelimit', detail='Too many concurrent streams for this principal.',
        source='routes.chat.stream_admission',
        extensions={'retry_after_s': 5})
    send_ok = {'taskId': 't1', 'convId': 'c1', 'title': 't',
               'userMessage': {'role': 'user'}, 'isNew': False, 'msgCount': 3}
    regen_ok = {'taskId': 't1', 'convId': 'c1', 'title': 't', 'msgCount': 3,
                'userMessage': None}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('continue-outcome', {'taskId': 't1', 'resumed': True}, 200,
         lambda: api_ok({'taskId': 't1', 'resumed': True}), False),
        ('admission-503', {'ok': False, 'error': dict(capacity_env)}, 503,
         lambda: api_typed_error(
             'server_busy', status=503,
             detail='Server is at task capacity. Retry shortly.',
             source='routes.chat.admission',
             extensions={'retry_after_s': 3}), True),
        ('start-taskid', {'taskId': 't1'}, 200,
         lambda: api_ok({'taskId': 't1'}), False),
        ('translate-status', {'statusMessage': 'm', 'statusKind': 'retry'},
         200, lambda: api_ok({'statusMessage': 'm',
                              'statusKind': 'retry'}), False),
        ('send-intent', {'queued': True, 'queueId': 'q1'}, 200,
         lambda: api_ok({'queued': True, 'queueId': 'q1'}), False),
        ('send-ok', dict(send_ok), 200, lambda: api_ok(dict(send_ok)),
         False),
        ('branch-taskid', {'taskId': 't1'}, 200,
         lambda: api_ok({'taskId': 't1'}), False),
        ('regen-ok', dict(regen_ok), 200, lambda: api_ok(dict(regen_ok)),
         False),
        ('ct-outcome', {'taskId': 't1', 'resumed': True}, 200,
         lambda: api_ok({'taskId': 't1', 'resumed': True}), False),
        ('sse-429', {'ok': False, 'error': dict(sse_env)}, 429,
         lambda: api_typed_error(
             'ratelimit', status=429,
             detail='Too many concurrent streams for this principal.',
             source='routes.chat.stream_admission',
             extensions={'retry_after_s': 5}), True),
    ]


def test_envelope_parity():
    from quart import jsonify
    app = _make_app()

    async def _t():
        async with app.test_request_context('/test'):
            for label, legacy_body, legacy_status, new, is_error in _sites():
                leg_status, leg_body = await _resolve(
                    (jsonify(legacy_body), legacy_status))
                new_status, new_body = await _resolve(new())

                assert new_status == leg_status, (
                    f'{label}: status {new_status} != legacy {leg_status}')
                new_body.pop('request_id', None)
                for k, v in leg_body.items():
                    assert k in new_body and new_body[k] == v, (
                        f'{label}: legacy key {k!r} lost/changed')
                added = set(new_body) - set(leg_body)
                allowed = {'ok', 'error'} if is_error else {'ok'}
                assert added <= allowed, (
                    f'{label}: unexpected added keys {added}')
                expected_ok = leg_body.get('ok', not is_error)
                assert new_body.get('ok') is expected_ok, (
                    f'{label}: ok flag wrong')

    asyncio.run(_t())


def test_active_bare_array_coordination():
    """backend wraps; Api.chat.active() unwraps PRESERVING null (probe
    semantics); the activeResponse caller unwraps with a fallback."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'id': 't1', 'convId': 'c1', 'status': 'running'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200 and body['ok'] is True
            assert body['items'] == rows

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'const chat\s*=\s*\{(?P<body>.*?)\n\s*\};', src, re.DOTALL)
    assert m, 'could not locate Api.chat in api.js'
    block = m.group('body')
    active_seg = block[block.find('active:'):block.find('activeResponse:')]
    assert '.items' in active_seg, (
        'Api.chat.active must unwrap .items (backend no longer returns a '
        'bare array)')
    assert re.search(r'Array\.isArray\(d\)', active_seg), (
        'Api.chat.active must keep an Array.isArray(d) fallback for skew')
    assert re.search(r'd\s*(?:===|==)\s*null|d\s*&&\s*Array', active_seg), (
        'Api.chat.active must PRESERVE null — its callers distinguish '
        '"zero tasks" from "probe failed"; null→[] would fake the former')

    with open(_INIT_JS, encoding='utf-8') as f:
        init_src = f.read()
    seg = init_src[init_src.find('activeResp'):]
    seg = seg[:seg.find('const toRecon')]
    assert '.items' in seg, (
        'main_init_tasks must unwrap .items from the activeResponse body '
        '(backend wraps the array now)')
    assert re.search(r'Array\.isArray\(', seg), (
        'main_init_tasks must keep an Array.isArray fallback for a '
        'pre-migration server')


def test_shipped_source_converted():
    """routes/chat.py carries no ad-hoc jsonify( and no flask jsonify
    import; the SSE 429 keeps its manual Retry-After header."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/chat.py still builds responses with bare jsonify( — '
        'convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/chat.py still imports jsonify')
    assert "'items': result" in src, (
        'expected the /chat/active bare-array to wrap as '
        "api_ok({'items': result}) — batch-11 coordinated migration")
    assert "resp.headers['Retry-After'] = '5'" in src, (
        'the SSE 429 must keep its manual Retry-After: 5 header — '
        'api_error does not set it (that is api_service_unavailable '
        'territory, and this is a 429)')
    assert 'api_typed_error(' in src
    assert "'server_busy'" in src and "'ratelimit'" in src


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_active_bare_array_coordination,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
