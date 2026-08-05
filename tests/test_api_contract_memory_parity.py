#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/memory.py
envelope migration (docs/API_CONTRACT.md §7 workflow, exemplar batch).

Two layers, mirroring tests/test_api_response_route_conversions.py:

1. PARITY — for each of the 11 converted sites the post-conversion
   ``api_ok``/``api_created``/``api_not_found`` call reproduces the legacy
   ``jsonify(...)`` body EXACTLY, allowing ONLY:
     * ``ok: True``               (success sites; purely additive)
     * ``ok: False`` + ``error`` (+ optional ``request_id``)
                                  (the delete-404 site)
   and the HTTP status is byte-identical (201 stays 201, 404 stays 404).

2. SHIPPED-SOURCE — the real file no longer contains ``jsonify(`` and its
   flask import no longer carries ``jsonify``. RED before the migration,
   GREEN after; the regression tripwire that keeps memory.py migrated.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim before importing anything that pulls in routes/lib.
import quart as _quart
sys.modules.setdefault('flask', _quart)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'memory.py')

pytestmark = pytest.mark.unit


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


async def _resolve(resp):
    """(status, body_dict) from a (Response, status) tuple in test ctx."""
    response, status = resp
    body = await response.get_data(as_text=True)
    return status, (json.loads(body) if body else {})


# ── The 11 converted sites ───────────────────────────────────────────
# (label, legacy_body, legacy_status, new_thunk, is_error)
def _sites():
    from lib.api_response import api_created, api_not_found, api_ok

    mem = {'id': 'm1', 'name': 'n', 'description': 'd', 'body': 'b',
           'tags': ['t'], 'scope': 'project'}
    profile = {'body': 'p', 'items': [{'header': 'h', 'text': 'x'}],
               'chars': 1, 'cap': 4000, 'over_cap': False, 'pending': []}
    return [
        ('list', {'memories': [mem]}, 200,
         lambda: api_ok({'memories': [mem]}), False),
        ('get', dict(mem), 200, lambda: api_ok(dict(mem)), False),
        ('create', dict(mem), 201, lambda: api_created(dict(mem)), False),
        ('update', dict(mem), 200, lambda: api_ok(dict(mem)), False),
        ('delete-200', {'deleted': True}, 200,
         lambda: api_ok(deleted=True), False),
        ('delete-404', {'deleted': False}, 404,
         lambda: api_not_found('Memory not found', deleted=False), True),
        ('merge', {'merged_memory': dict(mem), 'deleted_ids': ['a', 'b']}, 201,
         lambda: api_created({'merged_memory': dict(mem),
                              'deleted_ids': ['a', 'b']}), False),
        ('toggle', dict(mem), 200, lambda: api_ok(dict(mem)), False),
        ('profile-get', dict(profile), 200,
         lambda: api_ok(dict(profile)), False),
        ('profile-put', {'saved': True, 'items': []}, 200,
         lambda: api_ok({'saved': True, 'items': []}), False),
        ('profile-pending', {'resolved': True}, 200,
         lambda: api_ok({'resolved': True}), False),
    ]


def test_envelope_parity():
    """Every converted site reproduces the legacy body; the only additions
    are ok (always) and error/request_id (error sites only)."""
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
                        f'{label}: legacy key {k!r} lost/changed '
                        f'(new={new_body.get(k)!r})')

                added = set(new_body) - set(leg_body)
                allowed = {'ok', 'error'} if is_error else {'ok'}
                assert added <= allowed, (
                    f'{label}: unexpected added keys {added} '
                    f'(allowed {allowed})')
                assert new_body.get('ok') is (not is_error), (
                    f'{label}: ok flag wrong for {"error" if is_error else "success"}')

    asyncio.run(_t())


def test_shipped_source_converted():
    """routes/api_v1/memory.py carries no ad-hoc jsonify( and no flask
    jsonify import — the migration actually landed (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/memory.py still builds responses with bare jsonify( — '
        'convert to api_ok/api_created/api_not_found per docs/API_CONTRACT.md')
    import re
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/memory.py still imports jsonify from flask — '
        'drop the dead import after converting')
    for needle in ('api_ok(', 'api_created('):
        assert needle in src, (
            f'expected {needle} CALLS in memory.py — the paren keeps the '
            f'bare import line from satisfying the guard')


def test_status_codes_preserved():
    """The two non-200 statuses (201 create/merge, 404 delete-miss) survive
    the conversion — frontend memory.create uses parse:"response" and reads
    resp.ok/resp.status at the HTTP layer."""
    from lib.api_response import api_created, api_not_found
    app = _make_app()

    async def _t():
        async with app.test_request_context('/test'):
            s1, _ = await _resolve(api_created({'id': 'x'}))
            assert s1 == 201
            s2, _ = await _resolve(api_not_found('Memory not found',
                                                 deleted=False))
            assert s2 == 404

    asyncio.run(_t())


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted,
               test_status_codes_preserved):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
