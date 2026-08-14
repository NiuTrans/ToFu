#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/oauth.py
envelope migration (api-contract epic pt_931e16c4, batch 13).

7 ad-hoc sites, all the result-passthrough idiom (lib.oauth.manager
returns ``{auth_url, status, …}`` or ``{'error': …}``):

  * error branch     jsonify(result), 400  → api_payload(result, 400)
                     (top-level shape preserved; +ok:False only when the
                     result lacks one)
  * success branch   jsonify(result)       → api_ok(result)

The frontend oauth domain reads HTTP status + named fields (auth_url /
error) — additive ok is safe. Layers: PARITY + SHIPPED-SOURCE.
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
_TARGET = os.path.join(_ROOT, 'routes', 'oauth.py')

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
    login_ok = {'auth_url': 'https://x', 'status': 'started',
                'provider': 'claude', 'callback_port': 1455}
    exchange_ok = {'status': 'ok', 'provider': 'claude'}
    logout_ok = {'status': 'logged_out', 'provider': 'codex'}
    err = {'error': 'geo_blocked', 'hint': 'use browser exchange'}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('login-err', dict(err), 400, lambda: api_payload(dict(err), 400),
         True),
        ('login-ok', dict(login_ok), 200, lambda: api_ok(dict(login_ok)),
         False),
        ('callback-err', dict(err), 400,
         lambda: api_payload(dict(err), 400), True),
        ('callback-ok', dict(exchange_ok), 200,
         lambda: api_ok(dict(exchange_ok)), False),
        ('store-err', dict(err), 400, lambda: api_payload(dict(err), 400),
         True),
        ('store-ok', dict(exchange_ok), 200,
         lambda: api_ok(dict(exchange_ok)), False),
        ('logout-ok', dict(logout_ok), 200,
         lambda: api_ok(dict(logout_ok)), False),
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
    """routes/oauth.py carries no ad-hoc jsonify( and no flask jsonify
    import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/oauth.py still builds responses with bare jsonify( — '
        'convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/oauth.py still imports jsonify')
    assert 'api_payload(' in src, (
        'expected api_payload( CALLS in oauth.py (the three error '
        'passthroughs) — paren needle')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
