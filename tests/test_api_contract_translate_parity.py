#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/translate.py
envelope migration (api-contract epic pt_931e16c4, batch 14).

7 ad-hoc sites. Two sharp shapes:

  * poll-404 carries a BODY field named ``status`` ('not_found') — the
    api_error kwarg-collision class (batch 3 shape D) → api_payload(body, 404)
  * poll-batch returns a bare ARRAY, consumed by translation.js whose
    ``!Array.isArray(data)`` branch doubles as the probe-failure fallback
    (it synthesizes per-id error rows). So the seam unwrap is
    NULL-PRESERVING (batch 11 contract): null keeps meaning "probe failed"
    and still routes into that fallback.

The mt-test ``api_error(..., status=200)`` logical-failure-with-200 idiom
is the contract's documented deliberate exception — untouched.

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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'translate.py')
_API_JS = os.path.join(_ROOT, 'frontend', 'src', 'runtime', 'app-runtime.js')

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
    from lib.api_response import api_error, api_ok, api_payload
    too_long = {'error': 'Text too long for sync (9 > 5). Use start.',
                'useAsync': True}
    poll_body = {'taskId': 't1', 'status': 'done', 'translated': 'x',
                 'model': 'm'}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('sync-413', dict(too_long), 413,
         lambda: api_error(too_long['error'], status=413, useAsync=True),
         True),
        ('notranslate-only', {'translated': 'raw'}, 200,
         lambda: api_ok({'translated': 'raw'}), False),
        ('sync-ok', {'translated': 'x', 'model': 'm', 'truncated': False},
         200, lambda: api_ok({'translated': 'x', 'model': 'm',
                              'truncated': False}), False),
        ('start', {'taskId': 't1'}, 200,
         lambda: api_ok({'taskId': 't1'}), False),
        ('poll-404', {'error': 'Task not found', 'status': 'not_found'},
         404, lambda: api_payload({'error': 'Task not found',
                                   'status': 'not_found'}, 404), True),
        ('poll-ok', dict(poll_body), 200,
         lambda: api_ok(dict(poll_body)), False),
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


def test_poll_batch_bare_array_coordination():
    """backend wraps; Api.translate.pollBatch unwraps NULL-PRESERVING —
    translation.js's !Array.isArray(data) branch is the probe-failure
    fallback and must keep firing on null."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'taskId': 't1', 'status': 'done', 'translated': 'x'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200 and body['ok'] is True
            assert body['items'] == rows

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'pollBatch:\s*async[^=]*=>\s*\{(?P<body>.*?)\n\s*\},',
                  src, re.DOTALL)
    assert m, 'Api.translate.pollBatch is not the unwrapped async form'
    block = m.group('body')
    assert '.items' in block, 'pollBatch must unwrap .items'
    assert re.search(r'Array\.isArray\(d\)', block), (
        'pollBatch must keep an Array.isArray(d) fallback')
    assert re.search(r'd\s*==\s*null|d\s*===\s*null', block), (
        'pollBatch must PRESERVE null (probe semantics — the caller '
        'synthesizes per-id error rows on non-arrays)')


def test_shipped_source_converted():
    """routes/api_v1/translate.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/translate.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/translate.py still imports jsonify')
    assert "'items': results" in src, (
        'expected the poll-batch bare array to wrap as '
        "api_ok({'items': results}) — batch-14 coordinated migration")


if __name__ == '__main__':
    for fn in (test_envelope_parity,
               test_poll_batch_bare_array_coordination,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
