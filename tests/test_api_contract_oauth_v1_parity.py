#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/oauth.py
envelope migration (api-contract epic pt_931e16c4, batch 15).

5 ad-hoc sites, all dict payloads → api_ok({...}). The status surface is
keyed BY PROVIDER NAME ({claude: {...}, codex: {...}}) — verified that the
sole consumer (settings/oauth.js::_loadOAuthStatus) reads data.claude /
data.codex BY NAME and never enumerates keys, so the additive top-level
ok cannot appear as a fake provider. Layers: PARITY + SHIPPED-SOURCE.
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

from tests._runtime_sections import runtime_section_path

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'oauth.py')
_CONSUMER_JS = runtime_section_path('settings/oauth.js')

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
    one = {'logged_in': True, 'expires_at': 123,
           'egress': {'state': 'agent'}}
    all_status = {'claude': {'logged_in': True},
                  'codex': {'logged_in': False}}
    probe = {'claude_token': {'url': 'u', 'status': 200, 'reachable': True,
                              'blocked': False, 'detail': ''}}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('status-one', dict(one), 200, lambda: api_ok(dict(one)), False),
        ('status-all', dict(all_status), 200,
         lambda: api_ok(dict(all_status)), False),
        ('test-probe', dict(probe), 200, lambda: api_ok(dict(probe)),
         False),
        ('egress-get', {'pinned': 'a1', 'agents': [{'agent_id': 'a1'}]},
         200, lambda: api_ok({'pinned': 'a1',
                              'agents': [{'agent_id': 'a1'}]}), False),
        ('egress-set', {'ok': True, 'pinned': 'a1'}, 200,
         lambda: api_ok({'pinned': 'a1'}), False),
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


def test_consumer_reads_providers_by_name():
    """The additive top-level ok must be invisible to the consumer: it may
    read data.claude / data.codex by name, NEVER enumerate the provider
    keys (an 'ok' entry would surface as a fake provider card)."""
    with open(_CONSUMER_JS, encoding='utf-8') as f:
        src = f.read()
    seg_start = src.find('function _loadOAuthStatus')
    assert seg_start > 0, 'consumer function missing'
    seg = src[seg_start:seg_start + 3000]
    assert 'data.claude' in seg and 'data.codex' in seg, (
        'consumer must keep reading providers by name')
    assert not re.search(r'Object\.keys\(data\)|for\s*\(\s*var\s+\w+\s+in\s+data\s*\)',
                         seg), (
        'consumer enumerates the status body keys — an additive top-level '
        'ok would surface as a fake provider; the api_ok conversion is '
        'NOT safe here and must be rethought')


def test_shipped_source_converted():
    """routes/api_v1/oauth.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/oauth.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/oauth.py still imports jsonify')
    assert 'api_ok(' in src, (
        'expected api_ok( CALLS in api_v1/oauth.py — paren needle')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_consumer_reads_providers_by_name,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
