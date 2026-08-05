#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/folders.py +
routes/api_v1/paper_folders.py envelope migration (api-contract epic
pt_931e16c4, batch 19 — same-family twin batch).

Each file has 3 ad-hoc sites. The list endpoint (``_read_folders()``)
returns a bare top-level ARRAY in both; both api.js seams
(``folders.list`` / ``paperFolders.list``) already return
``(await get(...)) || []`` — list-UI semantics, so the unwrap contract is
the batch-4 ``|| []`` form (an empty list on probe failure is correct UI
degradation here, unlike the probe surfaces of batches 11/14). Coordinated:
backend wraps {ok, items}; each seam unwraps with an Array.isArray
fallback. create 201 → api_created; update → api_ok.

Layers: PARITY + COORDINATION + SHIPPED-SOURCE.
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
_TARGETS = (os.path.join(_ROOT, 'routes', 'api_v1', 'folders.py'),
            os.path.join(_ROOT, 'routes', 'api_v1', 'paper_folders.py'))
_API_JS = os.path.join(_ROOT, 'static', 'js', 'api.js')

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
    from lib.api_response import api_created, api_ok
    folder = {'id': 'f_1', 'name': 'n', 'color': '#fff',
              'createdAt': 1, 'updatedAt': 2}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('create-201', dict(folder), 201,
         lambda: api_created(dict(folder)), False),
        ('update', dict(folder, name='n2'), 200,
         lambda: api_ok(dict(folder, name='n2')), False),
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


def test_bare_array_coordination():
    """Both backends wrap; BOTH api.js seams unwrap with a fallback."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'id': 'f_1', 'name': 'n'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200 and body['ok'] is True
            assert body['items'] == rows

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    for domain in ('folders', 'paperFolders'):
        m = re.search(r'const ' + domain + r'\s*=\s*\{(?P<body>.*?)\n\s*\};',
                      src, re.DOTALL)
        assert m, f'could not locate Api.{domain} in api.js'
        block = m.group('body')
        assert '.items' in block, (
            f'Api.{domain}.list must unwrap .items (backend wraps)')
        assert re.search(r'Array\.isArray\(d\)', block), (
            f'Api.{domain}.list must keep an Array.isArray(d) fallback')


def test_shipped_source_converted():
    """Both files carry no ad-hoc jsonify( and no flask jsonify import
    (RED-first tripwire)."""
    for path in _TARGETS:
        with open(path, encoding='utf-8') as f:
            src = f.read()
        name = os.path.basename(path)
        assert 'jsonify(' not in src, (
            f'{name} still builds responses with bare jsonify(')
        assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
            f'{name} still imports jsonify')
        assert "'items': _read_folders()" in src, (
            f'{name}: expected the bare-array list to wrap as '
            "api_ok({'items': _read_folders()})")


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_bare_array_coordination,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
