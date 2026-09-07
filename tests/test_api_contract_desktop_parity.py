#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/desktop.py
envelope migration (api-contract epic pt_931e16c4, batch 5).

11 ad-hoc sites, all dict payloads (zero bare arrays):

  * big read dicts           jsonify({...})          → api_ok({...})
  * builder-state 202s       jsonify(st), 202        → api_payload(st, 202)
                             (non-standard status; st keys preserved top-level)
  * token mint 201           jsonify({...}), 201     → api_created({...})
  * not-found literals       jsonify({'error': 'not_found', 'message': m}), 404
                             → api_not_found('not_found', message=m)
                             (error + message keys survive; +ok:False additive)

The binary download route (send_file) is a contract §4 binary carve-out —
untouched. Layers: PARITY + SHIPPED-SOURCE, mirroring batches 1-4.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'desktop.py')

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
    from lib.api_response import (
        api_created, api_not_found, api_ok, api_payload)
    status_body = {'connected': True, 'last_poll': 1.0, 'secondsAgo': 2.0,
                   'pending_commands': 0, 'agents': [],
                   'setup_state': 'connected', 'download_url': 'u',
                   'downloads': [], 'server_url': 'http://x'}
    build_state = {'state': 'building', 'version': '0.16.0', 'pid': 42}
    stream = {'cmd_id': 'c1', 'lines': ['a'], 'done': False}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('status', dict(status_body), 200,
         lambda: api_ok(dict(status_body)), False),
        ('build-win-202', dict(build_state), 202,
         lambda: api_payload(dict(build_state), 202), False),
        ('build-linux-202', dict(build_state), 202,
         lambda: api_payload(dict(build_state), 202), False),
        ('build-get', {'linux': dict(build_state),
                       'windows': dict(build_state)}, 200,
         lambda: api_ok({'linux': dict(build_state),
                         'windows': dict(build_state)}), False),
        ('download-404', {'error': 'not_found',
                          'message': 'no such artifact'}, 404,
         lambda: api_not_found('not_found', message='no such artifact'),
         True),
        ('stream-404', {'error': 'not_found',
                        'message': 'unknown or expired command stream'}, 404,
         lambda: api_not_found(
             'not_found', message='unknown or expired command stream'), True),
        ('stream-ok', dict(stream), 200, lambda: api_ok(dict(stream)), False),
        ('devices', {'agents': [{'id': 'a'}],
                     'tokens': [{'id': 'k', 'scopes': ['agents:bridge']}]},
         200, lambda: api_ok({'agents': [{'id': 'a'}],
                              'tokens': [{'id': 'k',
                                          'scopes': ['agents:bridge']}]}),
         False),
        ('token-mint-201', {'id': 'k1', 'name': 'n', 'token': 't',
                            'scopes': ['agents:bridge']}, 201,
         lambda: api_created({'id': 'k1', 'name': 'n', 'token': 't',
                              'scopes': ['agents:bridge']}), False),
        ('revoke-404', {'error': 'not_found',
                        'message': 'bridge token not found'}, 404,
         lambda: api_not_found('not_found',
                               message='bridge token not found'), True),
        ('revoke-ok', {'revoked': 'k1'}, 200,
         lambda: api_ok({'revoked': 'k1'}), False),
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
                assert added <= {'ok'}, (
                    f'{label}: unexpected added keys {added}')
                expected_ok = leg_body.get('ok', not is_error)
                assert new_body.get('ok') is expected_ok, (
                    f'{label}: ok flag wrong')

    asyncio.run(_t())


def test_shipped_source_converted():
    """routes/api_v1/desktop.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/desktop.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/desktop.py still imports jsonify')
    assert 'api_payload(' in src, (
        'expected api_payload( CALLS in desktop.py (the two 202 builder-'
        'state sites) — paren needle so the import line cannot satisfy')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
