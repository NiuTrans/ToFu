#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the routes/api_v1/project.py
envelope migration (epic pt_931e16c4 batch 2, docs/API_CONTRACT.md §7).

project.py's 38 ad-hoc sites were ALL dict payloads — zero bare arrays —
in exactly two shapes:

  A. success passthrough     ``return jsonify(result)``          → api_ok(result)
  B. error result passthrough ``return jsonify(result), 400|409|code``
                              → api_payload(result, 400|409|code)

Shape B is the lib-layer ``{ok, error, ...}`` result dict the route only
adds an HTTP status to. ``api_error`` would NEST it under a single 'error'
key (breaking every consumer reading ``body.error``/``body.version`` …), so
this batch introduced ``api_payload`` (lib/api_response.py) — the primitive
that preserves the top-level shape and only GUARANTEES the envelope keys.

Layers:
  1. HELPER CONTRACT — api_payload: ok kept when present, defaulted by
     status when absent, request_id only on >=400, extras merged.
  2. PARITY — every distinct site shape reproduces the legacy body EXACTLY,
     allowing ONLY +ok (when absent) and +request_id (error statuses).
  3. SHIPPED-SOURCE — no ``jsonify(`` remains in project.py and its flask
     import dropped jsonify. RED before the migration, GREEN after.
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
_TARGET = os.path.join(_ROOT, 'routes', 'api_v1', 'project.py')

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


# ── Layer 1: api_payload helper contract ─────────────────────────────

def test_api_payload_keeps_ok_when_present():
    """A lib result that already carries ok (True or False) passes through
    untouched — the helper must not flip it to match the status."""
    from lib.api_response import api_payload
    app = _make_app()

    async def _t():
        async with app.test_request_context('/t'):
            s, body = await _resolve(api_payload(
                {'ok': False, 'error': 'version_conflict', 'version': 3}, 409))
            assert s == 409
            assert body['ok'] is False
            assert body['error'] == 'version_conflict'
            assert body['version'] == 3

            s2, body2 = await _resolve(api_payload({'ok': True, 'x': 1}, 200))
            assert body2['ok'] is True

    asyncio.run(_t())


def test_api_payload_defaults_ok_from_status():
    """A payload WITHOUT an ok key gains it: True below 400, False at 400+ —
    the pure-additive envelope guarantee."""
    from lib.api_response import api_payload
    app = _make_app()

    async def _t():
        async with app.test_request_context('/t'):
            _, ok_body = await _resolve(api_payload({'entries': []}, 200))
            assert ok_body['ok'] is True
            assert ok_body['entries'] == []

            _, err_body = await _resolve(api_payload({'error': 'boom'}, 400))
            assert err_body['ok'] is False
            assert err_body['error'] == 'boom'

    asyncio.run(_t())


def test_api_payload_status_and_extras():
    from lib.api_response import api_payload
    app = _make_app()

    async def _t():
        async with app.test_request_context('/t'):
            s, body = await _resolve(
                api_payload({'error': 'rate_limited'}, 429, retryAfter=5))
            assert s == 429
            assert body['ok'] is False
            assert body['retryAfter'] == 5

    asyncio.run(_t())


# ── Layer 2: per-shape parity (thunk vs legacy literal) ──────────────
# (label, legacy_body, legacy_status, new_thunk, is_error)
def _sites():
    from lib.api_response import api_ok, api_payload
    return [
        # ── A. success passthroughs (add only ok:True) ──
        ('status-global', {'path': '/p', 'roots': ['/p'], 'readOnlyPaths': []},
         200, lambda: api_ok({'path': '/p', 'roots': ['/p'],
                              'readOnlyPaths': []}), False),
        ('browse-ok', {'path': '/p', 'entries': [{'name': 'a'}]}, 200,
         lambda: api_ok({'path': '/p', 'entries': [{'name': 'a'}]}), False),
        ('mkdir-ok', {'ok': True, 'path': '/p/a'}, 200,
         lambda: api_ok({'ok': True, 'path': '/p/a'}), False),
        ('recent', {'projects': ['/a', '/b']}, 200,
         lambda: api_ok({'projects': ['/a', '/b']}), False),
        ('undo-ok', {'undone': 3, 'failed': 0}, 200,
         lambda: api_ok({'undone': 3, 'failed': 0}), False),
        ('rescan', {'ok': True, 'files': 42}, 200,
         lambda: api_ok({'ok': True, 'files': 42}), False),
        ('write-ok', {'ok': True, 'path': 'f.py', 'created': False,
                      'lines': 10}, 200,
         lambda: api_ok({'ok': True, 'path': 'f.py', 'created': False,
                         'lines': 10}), False),
        # ── B. error result passthroughs (add only ok:False / request_id) ──
        ('browse-err', {'error': 'boom'}, 400,
         lambda: api_payload({'error': 'boom'}, 400), True),
        ('gitignore-accept-err', {'error': 'x', 'unknown': ['d']}, 400,
         lambda: api_payload({'error': 'x', 'unknown': ['d']}, 400), True),
        ('charter-commit-conflict',
         {'ok': False, 'error': 'version_conflict', 'version': 3}, 409,
         lambda: api_payload({'ok': False, 'error': 'version_conflict',
                              'version': 3}, 409), True),
        ('charter-commit-err', {'ok': False, 'error': 'x'}, 400,
         lambda: api_payload({'ok': False, 'error': 'x'}, 400), True),
        ('board-post-err', {'ok': False, 'error': 'x'}, 400,
         lambda: api_payload({'ok': False, 'error': 'x'}, 400), True),
        ('status-ask-err', {'ok': False, 'error': 'x'}, 400,
         lambda: api_payload({'ok': False, 'error': 'x'}, 400), True),
        ('watch-promote-goal', {'ok': False, 'error': 'goal_not_promotable'},
         400, lambda: api_payload({'ok': False, 'error': 'goal_not_promotable'},
                                  400), True),
        ('peer-message-rate',
         {'ok': False, 'error': 'rate_limited', 'retryAfter': 5}, 400,
         lambda: api_payload({'ok': False, 'error': 'rate_limited',
                              'retryAfter': 5}, 400), True),
        ('write-err', {'ok': False, 'error': 'x'}, 400,
         lambda: api_payload({'ok': False, 'error': 'x'}, 400), True),
    ]


def test_envelope_parity():
    """status identical; legacy keys byte-identical; additions ⊆ {ok,
    request_id}; ok flag correct per branch."""
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
                assert added <= {'ok'}, (
                    f'{label}: unexpected added keys {added} '
                    '(only ok + request_id permitted)')
                assert new_body.get('ok') is (not is_error), (
                    f'{label}: ok flag wrong for '
                    f'{"error" if is_error else "success"}')

    asyncio.run(_t())


# ── Layer 3: shipped-source guard ────────────────────────────────────

def test_shipped_source_converted():
    """routes/api_v1/project.py carries no ad-hoc jsonify( and no flask
    jsonify import — the migration actually landed (RED-first tripwire)."""
    with open(_TARGET, encoding='utf-8') as f:
        src = f.read()
    assert 'jsonify(' not in src, (
        'routes/api_v1/project.py still builds responses with bare jsonify( — '
        'convert to api_ok / api_payload per docs/API_CONTRACT.md §7')
    assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
        'routes/api_v1/project.py still imports jsonify from flask — '
        'drop the dead import after converting')
    assert 'api_payload(' in src, (
        'expected api_payload CALLS in project.py — the passthrough '
        'branches require it; needle carries the paren so the bare import '
        'line cannot satisfy the guard')


def test_no_error_site_nests_under_error_key():
    """The exact failure mode api_payload exists to prevent: a converted
    passthrough that ran result through api_error would nest the lib result
    under a single 'error' key (body.error becomes the whole dict). Guard
    the two highest-stakes shapes directly."""
    from lib.api_response import api_payload
    app = _make_app()

    async def _t():
        async with app.test_request_context('/t'):
            _, body = await _resolve(api_payload(
                {'ok': False, 'error': 'version_conflict', 'version': 3}, 409))
            # A nested shape would put a DICT at body['error'] and lose
            # body['version'] from the top level.
            assert body['error'] == 'version_conflict'
            assert isinstance(body['error'], str)
            assert body.get('version') == 3

    asyncio.run(_t())


if __name__ == '__main__':
    for fn in (test_api_payload_keeps_ok_when_present,
               test_api_payload_defaults_ok_from_status,
               test_api_payload_status_and_extras,
               test_envelope_parity,
               test_shipped_source_converted,
               test_no_error_site_nests_under_error_key):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
