#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/mcp.py
envelope migration (epic pt_931e16c4 batch 3, docs/API_CONTRACT.md §7).

mcp.py's 21 ad-hoc sites: zero bare arrays, every legacy body already
carried an explicit ``ok`` key — so the conversion is BYTE-IDENTICAL
(no added keys beyond ``request_id`` on error statuses, which the
comparison strips). Distinct shapes:

  * ok literals            ``jsonify({'ok': True, ...})``        → api_ok(...)
  * error literals         ``jsonify({'ok': False, 'error': m}), N``
                            → api_error(m, status=N, **extra_keys)
  * custom-status bodies   ``jsonify({...}), 202`` / body carrying a
                            'status' BODY key (kwarg collision with
                            api_error's status=) → api_payload(body, N)

Layers: PARITY (thunk vs legacy literal, byte-identical modulo request_id)
+ SHIPPED-SOURCE (no ``jsonify(`` remains; flask import dropped).
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'mcp.py')

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


# (label, legacy_body, legacy_status, new_thunk, is_error)
def _sites():
    from lib.api_response import api_error, api_ok, api_payload
    return [
        ('upsert-ok', {'ok': True, 'message': 'Server "x" configured'}, 200,
         lambda: api_ok(message='Server "x" configured'), False),
        ('delete-ok', {'ok': True, 'message': 'Server "x" removed'}, 200,
         lambda: api_ok(message='Server "x" removed'), False),
        ('connect-404', {'ok': False, 'error': 'Server "x" not in config'},
         404, lambda: api_error('Server "x" not in config', status=404), True),
        ('connect-ok', {'ok': True, 'server': 'x', 'tools_count': 2,
                        'tool_names': ['a', 'b']}, 200,
         lambda: api_ok(server='x', tools_count=2,
                        tool_names=['a', 'b']), False),
        ('connect-500', {'ok': False, 'error': 'boom', 'stderr_tail': 't'},
         500, lambda: api_error('boom', status=500, stderr_tail='t'), True),
        ('connect-all-ok', {'ok': True, 'servers': {'x': {'tools': ['a']}},
                            'total_tools': 1}, 200,
         lambda: api_ok(servers={'x': {'tools': ['a']}},
                        total_tools=1), False),
        ('disconnect-ok', {'ok': True, 'message': 'Disconnected from "x"'},
         200, lambda: api_ok(message='Disconnected from "x"'), False),
        ('tools-ok', {'ok': True, 'tools': [], 'total': 0,
                      'servers_connected': 0}, 200,
         lambda: api_ok(tools=[], total=0, servers_connected=0), False),
        ('install-404', {'ok': False, 'error': 'Unknown server: x'}, 404,
         lambda: api_error('Unknown server: x', status=404), True),
        ('install-env-400', {'ok': False, 'error': 'Required: API Key'}, 400,
         lambda: api_error('Required: API Key', status=400), True),
        ('install-202', {'ok': True, 'status': 'installing', 'id': 'x',
                         'message': 'm'}, 202,
         lambda: api_payload({'ok': True, 'status': 'installing', 'id': 'x',
                              'message': 'm'}, 202), False),
        ('install-job-500', {'ok': False, 'error': 'hint',
                             'config_saved': True, 'stderr_tail': 'd'}, 500,
         lambda: api_error('hint', status=500, config_saved=True,
                           stderr_tail='d'), True),
        ('connect-after-500',
         {'ok': False, 'error': 'Config saved but connection failed.\n\nb',
          'config_saved': True, 'stderr_tail': 't'}, 500,
         lambda: api_error('Config saved but connection failed.\n\nb',
                           status=500, config_saved=True,
                           stderr_tail='t'), True),
        ('connect-after-crash-500',
         {'ok': False, 'error': 'Config saved but connection failed: b',
          'config_saved': True}, 500,
         lambda: api_error('Config saved but connection failed: b',
                           status=500, config_saved=True), True),
        ('install-ready-ok', {'ok': True, 'status': 'ready', 'message': 'm',
                              'tools_count': 1, 'tool_names': ['a']}, 200,
         lambda: api_ok(status='ready', message='m', tools_count=1,
                        tool_names=['a']), False),
        ('status-unknown', {'ok': True, 'status': 'unknown', 'id': 'x'}, 200,
         lambda: api_ok(status='unknown', id='x'), False),
        ('status-installing-202', {'ok': True, 'status': 'installing',
                                   'id': 'x'}, 202,
         lambda: api_payload({'ok': True, 'status': 'installing',
                              'id': 'x'}, 202), False),
        ('status-error-500', {'ok': False, 'status': 'error', 'error': 'hint',
                              'config_saved': True, 'stderr_tail': 'd'}, 500,
         lambda: api_payload({'ok': False, 'status': 'error', 'error': 'hint',
                              'config_saved': True, 'stderr_tail': 'd'},
                             500), True),
        ('uninstall-purge', {'ok': True, 'message': 'Uninstalled x',
                             'purged': True}, 200,
         lambda: api_ok(message='Uninstalled x', purged=True), False),
        ('uninstall-soft',
         {'ok': True, 'message': 'x disabled (credentials kept for re-enable)',
          'purged': False}, 200,
         lambda: api_ok(
             message='x disabled (credentials kept for re-enable)',
             purged=False), False),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; ZERO added keys
    (request_id stripped — it is the only permitted addition)."""
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
                assert not added, (
                    f'{label}: unexpected added keys {added} — every mcp.py '
                    'legacy body already carried ok, so the conversion must '
                    'be byte-identical modulo request_id')
                assert new_body.get('ok') is (not is_error), (
                    f'{label}: ok flag wrong')

    asyncio.run(_t())


def test_status_body_field_survives():
    """The batch's sharp edge, proven executable: a payload carrying a BODY
    field named ``status`` (the install state machine) must keep it —
    api_error's keyword-only ``status`` (the HTTP code) cannot express it
    (duplicate kwarg → TypeError), which is exactly why shape D uses
    api_payload. If a future api_error/api_payload signature change removes
    the collision, this test tells us the carve needs re-review."""
    from lib.api_response import api_error, api_payload
    app = _make_app()

    async def _t():
        async with app.test_request_context('/t'):
            s, body = await _resolve(api_payload(
                {'status': 'error', 'error': 'hint', 'ok': False}, 500))
            assert s == 500
            assert body['status'] == 'error'
            assert body['ok'] is False
            try:
                api_error('hint', status=500, **{'status': 'error'})
            except TypeError:
                pass
            else:
                raise AssertionError(
                    'api_error unexpectedly accepted a duplicate status kwarg '
                    '— the api_payload carve for body-status payloads needs '
                    're-review')

    asyncio.run(_t())


def test_shipped_source_converted():
    """routes/api_v1/mcp.py carries no ad-hoc jsonify( and no flask jsonify
    import — the migration actually landed (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/mcp.py still builds responses with bare jsonify( — '
        'convert per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/mcp.py still imports jsonify from flask')
    assert 'api_payload(' in src, (
        'routes/api_v1/mcp.py no longer CALLS api_payload — shape D (202 and '
        'the body-status-collision 500) REQUIRE it; an api_ok/api_error '
        'substitute either loses the 202 status or TypeErrors on the '
        'duplicate status kwarg. Needle carries the paren: the bare import '
        'line must NOT satisfy this guard')


if __name__ == '__main__':
    test_envelope_parity()
    print('ok test_envelope_parity')
    test_shipped_source_converted()
    print('ok test_shipped_source_converted')
    print('ALL PASSED')
