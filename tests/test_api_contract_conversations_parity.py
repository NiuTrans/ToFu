#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/conversations.py
envelope migration (api-contract epic pt_931e16c4, batch 9).

10 ad-hoc sites. Two of them are bare top-level ARRAYS — the
``GET /api/v1/conversations`` default (metadata) and ``?full=1`` branches.
Unlike the orchestrations list (batch 4, which had a first-party api.js
consumer to coordinate with), these branches have NO first-party caller:
the UI lists conversations via ``?meta=1`` (ETag/Response path, not
jsonify) or the ``?before`` envelope branch, and HEADLESS_API.md does not
document this shape (which already evolved once — full bodies → metadata
default — without a version bump). So the coordinated migration is
backend-only: ``api_ok({'items': convs})`` — called out in the commit
message as a deliberate breaking change for hypothetical external
bare-array readers, per contract §4 (elimination beats preservation).

The other 8: served-conv dicts and the folder envelope → api_ok; the two
``unsupported_keys`` 400 literals → api_error(msg, status=400, keys=…)
(legacy keys error+keys survive; +ok +request_id additive).

Layers: PARITY + SHIPPED-SOURCE, mirroring batches 1-8.
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
_TARGET = os.path.join(_ROOT, 'routes', 'conversations.py')

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
    from lib.api_response import api_error, api_ok
    envelope = {'conversations': [{'id': 'c1'}], 'hasMore': False,
                'nextBefore': 1, 'nextBeforeId': 'c1'}
    served = {'id': 'c1', 'title': 't', 'messages': [{'role': 'user'}],
              'rev': 3}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('folder-envelope', dict(envelope), 200,
         lambda: api_ok(dict(envelope)), False),
        ('served-windowed', dict(served), 200,
         lambda: api_ok(dict(served)), False),
        ('served-livetask', dict(served), 200,
         lambda: api_ok(dict(served)), False),
        ('served-reconciled', dict(served), 200,
         lambda: api_ok(dict(served)), False),
        ('served-failopen', dict(served), 200,
         lambda: api_ok(dict(served)), False),
        ('debug-messages', {'messages': [{'role': 'user'}], 'count': 1,
                            'approx': True}, 200,
         lambda: api_ok({'messages': [{'role': 'user'}], 'count': 1,
                         'approx': True}), False),
        ('patch-idx-400', {'error': 'unsupported_keys', 'keys': ['x']}, 400,
         lambda: api_error('unsupported_keys', status=400, keys=['x']),
         True),
        ('patch-id-400', {'error': 'unsupported_keys', 'keys': ['y']}, 400,
         lambda: api_error('unsupported_keys', status=400, keys=['y']),
         True),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; additions ⊆
    {ok, request_id} (+error on error sites); ok flag follows legacy body."""
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


def test_bare_array_branches_wrapped():
    """The default + ?full=1 list branches move their array under
    ``items`` verbatim (+ok) — the deliberate, documented shape change."""
    from lib.api_response import api_ok
    app = _make_app()
    convs = [{'id': 'c1', 'title': 't'}, {'id': 'c2', 'title': 'u'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': convs}))
            assert s == 200
            assert body['ok'] is True
            assert body['items'] == convs, 'the array must move verbatim'

    asyncio.run(_t())


def test_shipped_source_converted():
    """routes/conversations.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/conversations.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/conversations.py still imports jsonify')
    assert '"items": page_items' in src and 'api_ok(payload)' in src, (
        'expected the metadata page to wrap items in the canonical payload')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_bare_array_branches_wrapped,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
