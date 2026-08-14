#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/browser.py
envelope migration (api-contract epic pt_931e16c4, batch 17).

5 ad-hoc sites. The bridge test route's error exits carry a BODY field
named ``status`` (the nested bridge-state snapshot) — the batch-3 shape-D
kwarg collision → api_payload(body, 503|502). The rest are plain api_ok.
The raw extension long-poll RPC routes stay legacy (bridge protocol,
contract §4). Layers: PARITY + SHIPPED-SOURCE.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'browser.py')

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
    from lib.api_response import api_ok, api_payload
    status_body = {'connected': True, 'lastPoll': 1.0, 'secondsAgo': 2.0,
                   'clients': [], 'pendingCommands': 0, 'totalCommands': 0,
                   'extensionPath': None, 'chromeMajor': 142,
                   'localBrowser': None}
    snap = {'connected': False, 'lastPoll': None, 'clients': [],
            'pendingCommands': 0, 'commandIds': []}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('status', dict(status_body), 200,
         lambda: api_ok(dict(status_body)), False),
        ('clients', {'clients': [{'client_id': 'x'}]}, 200,
         lambda: api_ok({'clients': [{'client_id': 'x'}]}), False),
        ('test-503', {'status': dict(snap),
                      'error': 'Extension not connected'}, 503,
         lambda: api_payload({'status': dict(snap),
                              'error': 'Extension not connected'}, 503),
         True),
        ('test-502', {'status': dict(snap), 'result': None,
                      'error': 'bridge boom'}, 502,
         lambda: api_payload({'status': dict(snap), 'result': None,
                              'error': 'bridge boom'}, 502), True),
        ('test-ok', {'status': dict(snap), 'result': {'tabs': []},
                     'error': None}, 200,
         lambda: api_ok({'status': dict(snap), 'result': {'tabs': []},
                         'error': None}), False),
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
    """routes/api_v1/browser.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/browser.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/browser.py still imports jsonify')
    assert 'api_payload(' in src, (
        'expected api_payload( CALLS in api_v1/browser.py (the two '
        'body-status-collision exits) — paren needle')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
