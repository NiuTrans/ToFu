#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/artifacts.py
envelope migration (api-contract epic pt_931e16c4, batch 12).

7 ad-hoc sites, all dict payloads → api_ok({...}). None of the bodies
carried ``ok``, so +ok is purely additive; the binary/HTML carve-outs
(routes/artifacts.py raw/view/export) are contract §4 and untouched.
Layers: PARITY + SHIPPED-SOURCE, mirroring batches 1-11.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'artifacts.py')

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
    from lib.api_response import api_ok
    meta = {'id': 'a1', 'kind': 'markdown', 'title': 't', 'pinned': False}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('list-conv', {'conv_id': 'c1', 'count': 1,
                       'artifacts': [dict(meta)]}, 200,
         lambda: api_ok({'conv_id': 'c1', 'count': 1,
                         'artifacts': [dict(meta)]}), False),
        ('list-recent', {'count': 1, 'artifacts': [dict(meta)]}, 200,
         lambda: api_ok({'count': 1, 'artifacts': [dict(meta)]}), False),
        ('get-meta', dict(meta), 200, lambda: api_ok(dict(meta)), False),
        ('versions', {'count': 2, 'versions': [dict(meta), dict(meta)]},
         200, lambda: api_ok({'count': 2,
                              'versions': [dict(meta), dict(meta)]}), False),
        ('pin', dict(meta, pinned=True), 200,
         lambda: api_ok(dict(meta, pinned=True)), False),
        ('delete', {'deleted': True}, 200,
         lambda: api_ok({'deleted': True}), False),
        ('scan', {'conv_id': 'c1', 'scanned': 3, 'created': 1,
                  'artifacts': [dict(meta)]}, 200,
         lambda: api_ok({'conv_id': 'c1', 'scanned': 3, 'created': 1,
                         'artifacts': [dict(meta)]}), False),
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


def test_shipped_source_converted():
    """routes/api_v1/artifacts.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/artifacts.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/artifacts.py still imports jsonify')
    assert 'api_ok(' in src, (
        'expected api_ok( CALLS in artifacts.py — paren needle')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
