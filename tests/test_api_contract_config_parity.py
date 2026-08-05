#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/config.py
envelope migration (api-contract epic pt_931e16c4, batch 10).

8 ad-hoc sites. Two of them are bare top-level ARRAYS — the
``GET /api/v1/providers/templates`` early-return (empty dir) and main
return. This one HAS a first-party consumer (unlike batch 9's
backend-only case), so it is the batch-4 two-sided path, extended to a
THREE-file coordination:

  * backend  routes/config.py            → api_ok({'items': result})
  * seam     api.js providers.templates  → parse + unwrap .items with an
                                           Array.isArray(d) fallback
  * caller   settings/provider_templates.js
                                           → consumes the array directly
                                           (no r.ok / r.json() — the seam
                                           no longer returns a Response)

The other 6: big read dicts + probe passthroughs → api_ok; the
probe-bulk 50-cap 400 literal → api_error(msg, status=400).

Layers: PARITY + FRONT/BACK COORDINATION + SHIPPED-SOURCE.
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
_TARGET = os.path.join(_ROOT, 'routes', 'config.py')
_API_JS = os.path.join(_ROOT, 'static', 'js', 'api.js')
_CALLER_JS = os.path.join(_ROOT, 'static', 'js', 'settings',
                          'provider_templates.js')

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
    server_config = {'providers': [], 'presets': [], 'models': [],
                     'server_info': {}, 'capability_taxonomy': {}}
    feishu = {'ok': True, 'enabled': False, 'connected': False,
              'app_id_masked': '', 'has_secret': False, 'active_users': 0,
              'allowed_users': [], 'default_project': '',
              'workspace_root': ''}
    tpl_update = {'ok': True, 'updated_files': ['a.js'], 'model_count': 2}
    probe_result = {'ok': True, 'name': 'vllm', 'models': ['m1'],
                    'balance_url': ''}
    bulk_ok = {'ok': True, 'count': 2, 'ok_count': 1, 'results': []}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('server-config', dict(server_config), 200,
         lambda: api_ok(dict(server_config)), False),
        ('feishu', dict(feishu), 200, lambda: api_ok(dict(feishu)), False),
        ('template-update', dict(tpl_update), 200,
         lambda: api_ok(dict(tpl_update)), False),
        ('probe', dict(probe_result), 200,
         lambda: api_ok(dict(probe_result)), False),
        ('probe-bulk-cap400',
         {'ok': False, 'error': '单次最多探测 50 个端点（收到 51 个）'}, 400,
         lambda: api_error('单次最多探测 50 个端点（收到 51 个）',
                           status=400), True),
        ('probe-bulk-ok', dict(bulk_ok), 200,
         lambda: api_ok(dict(bulk_ok)), False),
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


def test_templates_bare_array_three_way_coordination():
    """backend wraps; the api.js seam unwraps with a fallback; the caller
    consumes the array directly (no Response surface)."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'key': 'local', 'models': []}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200
            assert body['ok'] is True
            assert body['items'] == rows

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'const providers\s*=\s*\{(?P<body>.*?)\n\s*\};', src,
                  re.DOTALL)
    assert m, 'could not locate Api.providers in api.js'
    block = m.group('body')
    assert '.items' in block, (
        'Api.providers.templates must unwrap .items (backend no longer '
        'returns a bare array)')
    assert re.search(r'Array\.isArray\(d\)', block), (
        'Api.providers.templates must keep an Array.isArray(d) fallback '
        'for rolling-deploy skew')

    with open(_CALLER_JS, encoding='utf-8') as f:
        caller = f.read()
    seg = caller[caller.find('_loadExternalProviderTemplates'):]
    seg = seg[:seg.find('async function', 10) or len(seg)]
    assert 'r.json()' not in seg, (
        'provider_templates.js still parses a Response — the seam now '
        'returns the unwrapped array directly')
    assert re.search(r'Array\.isArray\(templates\)', seg), (
        'provider_templates.js must keep its Array.isArray(templates) '
        'guard on the unwrapped value')


def test_shipped_source_converted():
    """routes/config.py carries no ad-hoc jsonify( and no flask jsonify
    import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/config.py still builds responses with bare jsonify( — '
        'convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/config.py still imports jsonify')
    assert "'items': result" in src, (
        'expected the templates bare-array branches to wrap as '
        "api_ok({'items': result}) — batch-10 coordinated migration")


if __name__ == '__main__':
    for fn in (test_envelope_parity,
               test_templates_bare_array_three_way_coordination,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
