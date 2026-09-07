#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/daily_report.py
envelope migration (api-contract epic pt_931e16c4, batch 7).

9 ad-hoc sites, all dict payloads → api_ok({...}). Most bodies already
carried ok:True (empty-report / generation-status shapes), so the
conversion is byte-identical there; the analysis-result passthroughs
(``jsonify(result)``) gain only +ok when the lib result lacks one. The
``status`` body field (idle|generating|done|error) is a BODY field here —
api_ok has no kwarg collision (unlike api_error's), so these are plain
success conversions.

Layers: PARITY + SHIPPED-SOURCE, mirroring batches 1-6.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'daily_report.py')

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
    empty = {'ok': True, 'tasks': [], 'quote': 'q', 'persona': 'p',
             'stats': {'totalConversations': 0}}
    result = {'streams': [{'id': 's', 'status': 'done'}],
              'stats': {'totalConversations': 1}}
    inherited = {'ok': True, 'streams': [], 'tomorrow': [],
                 'today_todos': [], 'tasks': [],
                 'stats': {'totalConversations': 2}, '_inherited': True,
                 'quote': 'q'}
    backfill_empty = {'ok': True, 'tasks': [], 'quote': 'q', 'persona': 'p',
                      'stats': {'totalConversations': 0}}
    generating = {'ok': True, 'status': 'generating',
                  'progress': {'stage': 'starting'}}
    inherited_done = {'ok': True, 'status': 'done',
                      'report': {'streams': [], '_inherited': True}}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('post-empty', dict(empty), 200, lambda: api_ok(dict(empty)), False),
        ('post-result', dict(result), 200, lambda: api_ok(dict(result)),
         False),
        ('get-inherited', dict(inherited), 200,
         lambda: api_ok(dict(inherited)), False),
        ('backfill-empty', dict(backfill_empty), 200,
         lambda: api_ok(dict(backfill_empty)), False),
        ('backfill-result', dict(result), 200, lambda: api_ok(dict(result)),
         False),
        ('generate-already', dict(generating), 200,
         lambda: api_ok(dict(generating)), False),
        ('generate-launch', dict(generating), 200,
         lambda: api_ok(dict(generating)), False),
        ('status-inherited', dict(inherited_done), 200,
         lambda: api_ok(dict(inherited_done)), False),
        ('status-generating', dict(generating), 200,
         lambda: api_ok(dict(generating)), False),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; additions ⊆
    {ok, request_id}; ok flag follows the legacy body when it carried one."""
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
    """routes/api_v1/daily_report.py carries no ad-hoc jsonify( and no
    flask jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/daily_report.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/daily_report.py still imports jsonify')
    assert 'api_ok(' in src, (
        'expected api_ok( CALLS in daily_report.py — paren needle so the '
        'bare import line cannot satisfy the guard')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
