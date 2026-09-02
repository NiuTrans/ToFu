#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the batch-21 final-three sweep
(api-contract epic pt_931e16c4): push.py (3) + common.py (14)
+ chat_queue.py (3).

  * push.py (3) — debug-presence dicts already carrying ok:True → api_ok
    (byte-identical).
  * common.py (14) — read/aggregate dicts → api_ok; the four dispatch
    aggregates (quota / endpoint-metrics / model-health / key-stats) were
    VERIFIED to nest their name-keyed maps one level down (``{models:
    {…}}`` / ``{endpoints: {…}}`` / ``{providers: {…}}``), so a top-level
    ok cannot mint a fake model/provider entry (batch-15 criterion). The
    ``_db_safe`` 503 literal → api_error('database_busy', status=503,
    message=…, retryAfter=2).
  * chat_queue.py (3) — the queue GET is a bare top-level ARRAY (plus an
    empty-degraded twin); its consumer main_send_pipeline guards
    ``!Array.isArray(serverQueue)`` (batch-4 ``|| []`` list-UI contract).
    Coordinated: backend wraps {ok, items}; the seam unwraps with a
    fallback. queueClear's {cleared} dict → api_ok.

Layers: PARITY + COORDINATION + SHIPPED-SOURCE.
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
_API_JS = os.path.join(_ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')

pytestmark = pytest.mark.unit

_CONVERTED = ('routes/push.py', 'routes/common.py', 'routes/chat_queue.py')


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
    from lib.api_response import api_error, api_ok
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('push-clear', {'ok': True, 'action': 'clear', 'root': '/p'}, 200,
         lambda: api_ok({'action': 'clear', 'root': '/p'}), False),
        ('push-subagents', {'ok': True, 'action': 'subagents',
                            'root': '/p', 'activePeers': 1}, 200,
         lambda: api_ok({'action': 'subagents', 'root': '/p',
                         'activePeers': 1}), False),
        ('push-scenario', {'ok': True, 'action': 'scenario', 'root': '/p',
                           'activePeers': 2}, 200,
         lambda: api_ok({'action': 'scenario', 'root': '/p',
                         'activePeers': 2}), False),
        ('db-safe-503', {'error': 'database_busy',
                         'message': 'Database temporarily busy, please retry.',
                         'retryAfter': 2}, 503,
         lambda: api_error('database_busy', status=503,
                           message='Database temporarily busy, please retry.',
                           retryAfter=2), True),
        ('log-compress', {'compressed': 'c', 'usage': {'tokens': 1}}, 200,
         lambda: api_ok({'compressed': 'c', 'usage': {'tokens': 1}}),
         False),
        ('pricing', {'models': {'m': {'input': 1}}, 'updated': 1}, 200,
         lambda: api_ok({'models': {'m': {'input': 1}}, 'updated': 1}),
         False),
        ('quota', {'models': {'m': {'requests_5h': 3}},
                   'total_requests_5h': 3, 'total_requests_all': 9}, 200,
         lambda: api_ok({'models': {'m': {'requests_5h': 3}},
                         'total_requests_5h': 3, 'total_requests_all': 9}),
         False),
        ('endpoint-metrics', {'endpoints': {'http://x': {'ema': 1}},
                              'ts': 1.0}, 200,
         lambda: api_ok({'endpoints': {'http://x': {'ema': 1}}, 'ts': 1.0}),
         False),
        ('model-health', {'providers': {'p': {'m': {'success_rate': 1.0}}},
                          'ts': 1.0}, 200,
         lambda: api_ok({'providers': {'p': {'m': {'success_rate': 1.0}}},
                         'ts': 1.0}), False),
        ('key-stats', {'day': '2026-08-01',
                       'providers': {'p': {'k': {'ok': 1}}}}, 200,
         lambda: api_ok({'day': '2026-08-01',
                         'providers': {'p': {'k': {'ok': 1}}}}), False),
        ('features', {'paper': True, 'trading_enabled': False}, 200,
         lambda: api_ok({'paper': True, 'trading_enabled': False}), False),
        ('health', {'status': 'ok', 'version': '0.16.0',
                    'storage': {'ready': True}}, 200,
         lambda: api_ok({'status': 'ok', 'version': '0.16.0',
                         'storage': {'ready': True}}), False),
        ('queue-clear', {'cleared': 3}, 200,
         lambda: api_ok({'cleared': 3}), False),
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


def test_queue_bare_array_coordination():
    """backend wraps BOTH array sites (the degraded empty and the live
    queue); the api.js queueGet seam unwraps with a fallback."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'id': 'q1', 'text': 'hello'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200 and body['items'] == rows

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'queueGet:\s*async[^=]*=>\s*\{(?P<body>.*?)\n\s*\},',
                  src, re.DOTALL)
    assert m, 'Api.chat.queueGet is not the unwrapped async form'
    block = m.group('body')
    assert '.items' in block, 'queueGet must unwrap .items'
    assert re.search(r'Array\.isArray\(d\)', block), (
        'queueGet must keep an Array.isArray(d) fallback')


def test_shipped_source_converted():
    """All three files carry no ad-hoc jsonify( and no flask jsonify
    import (RED-first tripwire)."""
    for rel in _CONVERTED:
        path = os.path.join(_ROOT, rel)
        with open(path, encoding='utf-8') as f:
            src = f.read()
        assert 'jsonify(' not in src, (
            f'{rel} still builds responses with bare jsonify(')
        assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
            f'{rel} still imports jsonify')


if __name__ == '__main__':
    for fn in (test_envelope_parity,
               test_queue_bare_array_coordination,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
