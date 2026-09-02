#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/auth.py
envelope migration (api-contract epic pt_931e16c4, batch 18).

4 ad-hoc sites — the global auth GATE's rejection envelopes (middleware,
not routes), consumed by every client class including external SDKs:

  * bridge 401 literal       jsonify({'error': 'bridge_auth_required', 'hint': h}), 401
                              → api_unauthorized('bridge_auth_required', hint=h)
  * typed-envelope 401s      jsonify({'ok': False, 'error': {kind…}}), 401
                              → api_typed_error('permission', status=401)
  * rate-limit 429           resp = jsonify({…}); apply_headers(resp, decision)
                              → resp, _st = api_typed_error('ratelimit', ...);
                              apply_headers(resp, decision); return resp, _st
                              (the X-RateLimit-* headers are applied by the
                              caller AFTER the envelope is built — preserved)

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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'auth.py')

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
    from lib.api_response import api_typed_error, api_unauthorized
    from lib.error_envelope import make_envelope
    unauth_env = make_envelope(
        'permission', detail='Invalid or expired API key.',
        source='api_v1.auth.token')
    required_env = make_envelope(
        'permission', detail='Authentication required.',
        source='api_v1.auth.required')
    rate_env = make_envelope(
        'ratelimit', detail='Rate limit exceeded (rpm)',
        source='api_v1.auth.rate_limit', extensions={'retry_after_s': 1.5})
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('bridge-401', {'error': 'bridge_auth_required',
                        'hint': 'set X-Bridge-Secret'}, 401,
         lambda: api_unauthorized('bridge_auth_required',
                                  hint='set X-Bridge-Secret'), True),
        ('bad-token-401', {'ok': False, 'error': dict(unauth_env)}, 401,
         lambda: api_typed_error(
             'permission', status=401, detail='Invalid or expired API key.',
             source='api_v1.auth.token'), True),
        ('no-cred-401', {'ok': False, 'error': dict(required_env)}, 401,
         lambda: api_typed_error(
             'permission', status=401, detail='Authentication required.',
             source='api_v1.auth.required'), True),
        ('rate-429', {'ok': False, 'error': dict(rate_env)}, 429,
         lambda: api_typed_error(
             'ratelimit', status=429, detail='Rate limit exceeded (rpm)',
             source='api_v1.auth.rate_limit',
             extensions={'retry_after_s': 1.5}), True),
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
    """routes/api_v1/auth.py carries no ad-hoc jsonify( and no flask
    jsonify import; the 429 still applies rate headers after the envelope
    is built (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/auth.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/auth.py still imports jsonify')
    assert 'apply_headers(resp, decision)' in src, (
        'the 429 must keep apply_headers(resp, decision) AFTER the '
        'envelope is built — the X-RateLimit-* headers ride the same resp')
    assert src.count('api_typed_error(') == 4
    assert "'permission'" in src and "'ratelimit'" in src


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
