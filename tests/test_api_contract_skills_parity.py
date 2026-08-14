#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/skills.py
envelope migration (api-contract epic pt_931e16c4, batch 6).

9 ad-hoc sites, all dict payloads:

  * read dicts               jsonify({...})            → api_ok({...})
  * soft-delete 200/404      jsonify({'deleted': ok}), (200|404)
                             → api_ok(deleted=True) /
                               api_not_found(…, deleted=False)
  * install results 201      jsonify({...}), 201       → api_created({...})
  * oversize 413 literals    jsonify({'error': msg}), 413
                             → api_error(msg, status=413)
                             (NOT api_payload_too_large — that helper
                             formats its OWN message; the legacy text
                             must survive for consumers that match it)

Layers: PARITY + SHIPPED-SOURCE, mirroring batches 1-5.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'skills.py')

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
        api_created, api_error, api_not_found, api_ok)
    mem = {'id': 's1', 'name': 'n', 'is_package': True, 'enabled': True}
    install_body = {'memory': dict(mem), 'replaced': False,
                    'install_hints': ['h']}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('list', {'skills': [dict(mem)]}, 200,
         lambda: api_ok({'skills': [dict(mem)]}), False),
        ('uninstall-200', {'deleted': True}, 200,
         lambda: api_ok(deleted=True), False),
        ('uninstall-404', {'deleted': False}, 404,
         lambda: api_not_found('Skill package not found', deleted=False),
         True),
        ('toggle', dict(mem), 200, lambda: api_ok(dict(mem)), False),
        ('files', {'skill_id': 's1', 'root': '/p', 'files': [], 'count': 0},
         200, lambda: api_ok({'skill_id': 's1', 'root': '/p', 'files': [],
                              'count': 0}), False),
        ('install-413', {'error': 'File exceeds 25 MB limit'}, 413,
         lambda: api_error('File exceeds 25 MB limit', status=413), True),
        ('install-201', dict(install_body), 201,
         lambda: api_created(dict(install_body)), False),
        ('catalog', {'catalog': [{'id': 'x', 'installed': False}],
                     'installed_ids': ['s1']}, 200,
         lambda: api_ok({'catalog': [{'id': 'x', 'installed': False}],
                         'installed_ids': ['s1']}), False),
        ('catalog-install-201', dict(install_body, catalog_id='x'), 201,
         lambda: api_created(dict(install_body, catalog_id='x')), False),
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
                allowed = {'ok', 'error'} if is_error else {'ok'}
                assert added <= allowed, (
                    f'{label}: unexpected added keys {added} '
                    f'(allowed {allowed})')
                expected_ok = leg_body.get('ok', not is_error)
                assert new_body.get('ok') is expected_ok, (
                    f'{label}: ok flag wrong')

    asyncio.run(_t())


def test_shipped_source_converted():
    """routes/api_v1/skills.py carries no ad-hoc jsonify( and no flask
    jsonify import (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/skills.py still builds responses with bare '
        'jsonify( — convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/skills.py still imports jsonify')
    assert 'api_created(' in src, (
        'expected api_created( CALLS in skills.py (the two 201 install '
        'sites) — paren needle so the import line cannot satisfy')


if __name__ == '__main__':
    for fn in (test_envelope_parity, test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
