#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the FINAL api-contract batch
(epic pt_931e16c4): routes/paper.py — the last 47 ad-hoc jsonify sites
(the split-roadmap hold lifted 2026-08-01: no sibling is splitting
paper.py; migrate-first order — the envelope helpers travel with their
handlers when the monolith eventually splits).

Site shapes (census 2026-08-01, all 47 classified):
  * ~20 ok:True dicts (report/qa/translate/recommend/pdf/reparse spawn
    + podcast/video/tts lookups) → api_ok(payload-minus-ok)
  * 9 resp passthroughs (poll/lookup builders that already carry ok)
    → api_payload(resp, 200) — top-level shape verbatim
  * 4 bare {'ok': False} 200s (lookup miss) → api_payload(x, 200) —
    byte-identical (request_id only attaches on >=400)
  * 3 custom 200 error dicts (report_required family + the 200|409
    report status) → api_payload(..., N) — verbatim, no forced error key
  * 4 explicit 400s (bad hash / bad mode / bad lang) → api_bad_request
  * 5 explicit 404s (task / podcast / podcast-audio) → api_not_found
    (additive request_id only — popped by the parity harness below)

Layers: PARITY + SHIPPED-SOURCE.
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
_PAPER = os.path.join(_ROOT, 'routes', 'paper.py')

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
    from lib.api_response import (api_bad_request, api_not_found, api_ok,
                                  api_payload)
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('spawn-report',
         {'ok': True, 'task_id': 't1', 'paper_hash': 'h',
          'running': True, 'existed': False}, 200,
         lambda: api_ok({'task_id': 't1', 'paper_hash': 'h',
                         'running': True, 'existed': False}), False),
        ('venues', {'ok': True, 'venues': ['v1', 'v2']}, 200,
         lambda: api_ok({'venues': ['v1', 'v2']}), False),
        ('qa-spawn',
         {'ok': True, 'task_id': 't1', 'paper_hash': 'h', 'running': True,
          'reportPresent': True}, 200,
         lambda: api_ok({'task_id': 't1', 'paper_hash': 'h',
                         'running': True, 'reportPresent': True}), False),
        ('podcast-lookup-cached',
         {'ok': True, 'found': True, 'cached': True, 'status': 'done',
          'script': {}, 'meta': {}, 'scriptOnly': False}, 200,
         lambda: api_ok({'found': True, 'cached': True, 'status': 'done',
                         'script': {}, 'meta': {}, 'scriptOnly': False}),
         False),
        ('podcast-404', {'ok': False, 'error': 'Podcast not found'}, 404,
         lambda: api_not_found('Podcast not found'), True),
        ('bad-hash-400', {'ok': False, 'error': 'invalid paper_hash'}, 400,
         lambda: api_bad_request('invalid paper_hash'), True),
        ('report-required-200',
         {'ok': False, 'report_required': True, 'report_lang': 'zh',
          'error': 'a report is required before a podcast can be generated'},
         200,
         lambda: api_payload(
             {'ok': False, 'report_required': True, 'report_lang': 'zh',
              'error': 'a report is required before a podcast can be '
                       'generated'}, 200), True),
        ('lookup-miss-bare', {'ok': False}, 200,
         lambda: api_payload({'ok': False}, 200), True),
        ('poll-passthrough',
         {'ok': True, 'status': 'done', 'content': 'c', 'error': ''}, 200,
         lambda: api_payload({'ok': True, 'status': 'done', 'content': 'c',
                              'error': ''}, 200), False),
        ('report-status-409',
         {'ok': False, 'stage': 'submit', 'filled': []}, 409,
         lambda: api_payload({'ok': False, 'stage': 'submit', 'filled': []},
                             409), True),
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
    """routes/paper.py carries no ad-hoc jsonify( and no flask jsonify
    import (RED-first tripwire)."""
    with open(_PAPER, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/paper.py still builds responses with bare jsonify(')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/paper.py still imports jsonify')
    assert 'api_payload(' in src, (
        'routes/paper.py must use api_payload for the passthrough/'
        'custom-status sites (9 resp passthroughs + 4 bare ok:False + '
        '3 custom error dicts)')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
