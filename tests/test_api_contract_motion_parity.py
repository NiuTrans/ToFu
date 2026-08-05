#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/motion.py
envelope migration (api-contract epic pt_931e16c4, batch 16).

5 ad-hoc sites, all dict payloads → api_ok({...}). The MP4/SRT send_file
routes are contract §4 binary carve-outs — untouched. Layers:
PARITY + SHIPPED-SOURCE, mirroring batches 1-15.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'motion.py')

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
    env = {'node': '22.1', 'hyperframes': True, 'ffmpeg': True,
           'chrome': True, 'tts_available': False}
    scenes = {'ok': True, 'task_id': 't1', 'status': 'done',
              'scenes': [{'scene_id': 's1', 'has_video': True}]}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('env', dict(env), 200, lambda: api_ok(dict(env)), False),
        ('dedup-join', {'ok': True, 'task_id': 't0', 'deduped': True}, 200,
         lambda: api_ok({'task_id': 't0', 'deduped': True}), False),
        ('start', {'ok': True, 'task_id': 't1', 'deduped': False}, 200,
         lambda: api_ok({'task_id': 't1', 'deduped': False}), False),
        ('scenes', dict(scenes), 200, lambda: api_ok(dict(scenes)), False),
        ('regen', {'ok': True, 'task_id': 'r1', 'regen_of': 't1',
                   'scene_id': 's1'}, 200,
         lambda: api_ok({'task_id': 'r1', 'regen_of': 't1',
                         'scene_id': 's1'}), False),
    ]


def test_envelope_parity():
    from flask import jsonify
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
    """routes/api_v1/motion.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/motion.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/motion.py still imports jsonify')
    assert 'api_ok(' in src, (
        'expected api_ok( CALLS in motion.py — paren needle')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
