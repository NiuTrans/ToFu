#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/orchestrations.py
envelope migration (api-contract epic pt_931e16c4, batch 4).

15 of the 16 sites are dict payloads → api_ok / api_created (byte-identical
when the lib result already carries ``ok``, additive +ok otherwise).

The 16th is the FIRST executed instance of docs/API_CONTRACT.md §4's
coordinated bare-array migration: ``GET /api/v1/orchestrations`` returned a
bare top-level ARRAY (``jsonify(_read_all())``). Enveloping changes the
top-level type, so it ships as ONE front+back change:

  * backend  → ``api_ok({'items': _read_all()})``
  * frontend ``Api.orchestrations.list`` unwraps ``.items`` — with an
    ``Array.isArray(d)`` fallback so a rolling-deploy skew (old server,
    new client) still yields the array callers expect. Every caller of
    ``list()`` keeps receiving a bare array: zero call-site change.

Layers:
  1. PARITY — legacy literal vs new call per dict site; for the list
     endpoint the array moves under ``items`` verbatim (+ok).
  2. FRONT+BACK COORDINATION — backend wraps, api.js unwraps with fallback.
  3. SHIPPED-SOURCE — no ``jsonify(`` / no flask jsonify import remains.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'orchestrations.py')
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
    entry = {'id': 'orch_x', 'name': 'n', 'definition': {'nodes': []},
             'createdAt': 1, 'updatedAt': 2}
    verdict = {'ok': False, 'errors': ['e'], 'warnings': []}
    compose_result = {'ok': True, 'reply': 'r',
                      'definition': {'nodes': []}, 'validation': {'ok': True}}
    plan = {'ok': True, 'steps': [{'node': 'a'}]}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('get-one', dict(entry), 200, lambda: api_ok(dict(entry)), False),
        ('validate', dict(verdict), 200, lambda: api_ok(dict(verdict)),
         False),
        ('compose', dict(compose_result), 200,
         lambda: api_ok(dict(compose_result)), False),
        ('create-201', dict(entry, warnings=[]), 201,
         lambda: api_created(dict(entry, warnings=[])), False),
        ('update', dict(entry, warnings=[]), 200,
         lambda: api_ok(dict(entry, warnings=[])), False),
        ('builtin', {'ok': True, 'definition': {'nodes': []}}, 200,
         lambda: api_ok({'definition': {'nodes': []}}), False),
        ('role-schema-one', {'ok': True, 'role': 'coder', 'fields': [],
                             'persona': 'p'}, 200,
         lambda: api_ok({'role': 'coder', 'fields': [], 'persona': 'p'}),
         False),
        ('role-schema-all', {'ok': True, 'roles': {}, 'generic': [],
                             'personas': {}, 'kinds': [], 'ioTypes': [],
                             'defaultOutput': 'out'}, 200,
         lambda: api_ok({'roles': {}, 'generic': [], 'personas': {},
                         'kinds': [], 'ioTypes': [], 'defaultOutput': 'out'}),
         False),
        ('layout', {'ok': True, 'definition': {'nodes': []}}, 200,
         lambda: api_ok({'definition': {'nodes': []}}), False),
        ('plan', dict(plan), 200, lambda: api_ok(dict(plan)), False),
        ('run', {'ok': True, 'task_id': 't1'}, 200,
         lambda: api_ok({'task_id': 't1'}), False),
        ('task-create-201', {'ok': True, 'run_id': 'r1'}, 201,
         lambda: api_created({'run_id': 'r1'}), False),
        ('task-list', {'ok': True, 'runs': [{'id': 'r1'}]}, 200,
         lambda: api_ok({'runs': [{'id': 'r1'}]}), False),
        ('task-get', {'ok': True, 'run': {'id': 'r1'}}, 200,
         lambda: api_ok({'run': {'id': 'r1'}}), False),
        ('task-events', {'ok': True, 'events': [], 'next_cursor': 0,
                         'status': 'done', 'done': True}, 200,
         lambda: api_ok({'events': [], 'next_cursor': 0,
                         'status': 'done', 'done': True}), False),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; additions ⊆
    {ok, request_id}; ok flag correct per branch."""
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
                assert added <= {'ok'}, (
                    f'{label}: unexpected added keys {added}')
                # A lib-result passthrough carries its OWN ok (e.g. the
                # validate endpoint's logical-failure 200 with ok:False);
                # otherwise the envelope default applies (ok = not is_error).
                expected_ok = leg_body.get('ok', not is_error)
                assert new_body.get('ok') is expected_ok, (
                    f'{label}: ok flag {new_body.get("ok")} != '
                    f'expected {expected_ok}')

    asyncio.run(_t())


def test_bare_array_coordinated_migration():
    """The list endpoint: the array moves under ``items`` verbatim (+ok),
    and api.js unwraps ``.items`` with an ``Array.isArray`` fallback so
    callers still receive a bare array under either server generation."""
    from lib.api_response import api_ok
    app = _make_app()
    rows = [{'id': 'a'}, {'id': 'b'}]

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': rows}))
            assert s == 200
            assert body['ok'] is True
            assert body['items'] == rows, 'the array must move verbatim'

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r'orchestrations\s*=\s*\{(?P<body>.*?)\n\s*\};', src,
                  re.DOTALL)
    assert m, 'could not locate Api.orchestrations in api.js'
    block = m.group('body')
    assert '.items' in block, (
        'Api.orchestrations.list must unwrap .items (the backend no longer '
        'returns a bare array) — callers expect an array')
    assert re.search(r'Array\.isArray\(d\)', block), (
        'Api.orchestrations.list must keep an Array.isArray(d) fallback for '
        'rolling-deploy skew against a pre-migration server')


def test_shipped_source_converted():
    """routes/api_v1/orchestrations.py carries no ad-hoc jsonify( and no
    flask jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/orchestrations.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/orchestrations.py still imports jsonify')
    assert 'api_created(' in src, (
        'expected api_created( CALLS in orchestrations.py (the two 201 '
        'sites) — paren needle so the import line cannot satisfy the guard')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_bare_array_coordinated_migration,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
