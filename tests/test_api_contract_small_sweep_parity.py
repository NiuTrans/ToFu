#!/usr/bin/env python3
"""Wire-parity + shipped-source guards for the batch-20 small-file sweep
(api-contract epic pt_931e16c4): 15 ad-hoc sites across 8 files.

  * conversations_search.py (3) — bare top-level ARRAYS (two early-empty
    returns + the main results list). Consumer conversation_list.js has a
    ``!Array.isArray(hits)`` guard (→ returns []), the batch-4 ``|| []``
    list-UI contract. Coordinated: backend wraps {ok, items}; the seam
    unwraps with a fallback.
  * swarm.py (3) — status/config dicts → api_ok. The api_meta description
    claimed the UI shape is bare while the SDK alias wraps — after this
    batch both are api_ok, so the description is corrected (batch-19
    honesty rule: a shape migration migrates its documentation metadata).
  * audio.py (1) + translate.py (1) + endpoint.py (2)
    + conversations_compaction.py (2) + chat_poll_abort.py (2)
    — plain dicts → api_ok (additive only).
  * _task_routes.py (1) — the generic factory passthrough
    ``jsonify(resp), status_code`` → api_payload(resp, status_code)
    (runtime.poll already returns the canonical {ok, events, …} shape;
    preserved verbatim, only the HTTP status varies).
  * desktop.py (1) — NOT converted: /api/desktop/poll is the desktop-agent
    BRIDGE protocol (external client parses {'commands': …}); moved from
    the baseline to CARVE_OUT_FILES in the drift suite.

Layers: PARITY + COORDINATION + SHIPPED-SOURCE (per file).
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
_API_JS = os.path.join(_ROOT, 'static', 'js', 'api.js')

pytestmark = pytest.mark.unit

_CONVERTED = (
    'routes/conversations_search.py',
    'routes/api_v1/swarm.py',
    'routes/api_v1/audio.py',
    'routes/translate.py',
    'routes/_task_routes.py',
    'routes/api_v1/endpoint.py',
    'routes/conversations_compaction.py',
    'routes/chat_poll_abort.py',
)


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
    poll_r = {'id': 't1', 'status': 'done', 'content': 'c',
              'thinking': '', 'finishReason': 'stop'}
    return [
        # (label, legacy_body, legacy_status, new_thunk, is_error)
        ('swarm-none', {'active': False, 'message': 'No swarm for this task'},
         200, lambda: api_ok({'active': False,
                              'message': 'No swarm for this task'}), False),
        ('swarm-status', {'active': True, 'agents': [{'id': 'a1'}],
                          'running': 1}, 200,
         lambda: api_ok({'active': True, 'agents': [{'id': 'a1'}],
                         'running': 1}), False),
        ('swarm-config', {'available': True, 'version': '1.0.0',
                          'roles': ['coder'], 'max_concurrent_agents': 8},
         200, lambda: api_ok({'available': True, 'version': '1.0.0',
                              'roles': ['coder'],
                              'max_concurrent_agents': 8}), False),
        ('audio-transcribe', {'ok': True, 'text': 't', 'model': 'm',
                              'provider_id': 'p', 'durationS': 1.5}, 200,
         lambda: api_ok({'text': 't', 'model': 'm', 'provider_id': 'p',
                         'durationS': 1.5}), False),
        ('translate-pptx-start', {'taskId': 't1'}, 200,
         lambda: api_ok({'taskId': 't1'}), False),
        ('task-routes-poll', {'ok': True, 'events': [], 'next_cursor': 1,
                              'status': 'done', 'done': True}, 200,
         lambda: api_payload({'ok': True, 'events': [], 'next_cursor': 1,
                              'status': 'done', 'done': True}, 200), False),
        ('task-routes-poll-404', {'error': 'not_found', 'ok': False}, 404,
         lambda: api_payload({'error': 'not_found', 'ok': False}, 404),
         True),
        ('endpoint-start', {'taskId': 't1', 'convId': 'c1'}, 200,
         lambda: api_ok({'taskId': 't1', 'convId': 'c1'}), False),
        ('endpoint-status', {'id': 't1', 'status': 'done',
                             'endpointMode': True, 'totalIterations': 2,
                             'reason': 'critic_stop', 'criticMessages': [],
                             'content': 'c', 'error': None, 'usage': {}},
         200, lambda: api_ok({'id': 't1', 'status': 'done',
                              'endpointMode': True, 'totalIterations': 2,
                              'reason': 'critic_stop', 'criticMessages': [],
                              'content': 'c', 'error': None, 'usage': {}}),
         False),
        ('compaction-list', {'compactions': [{'id': 'a1'}], 'count': 1},
         200, lambda: api_ok({'compactions': [{'id': 'a1'}], 'count': 1}),
         False),
        ('compaction-get', {'archive': {'id': 'a1'},
                            'messages': [{'role': 'user'}]}, 200,
         lambda: api_ok({'archive': {'id': 'a1'},
                         'messages': [{'role': 'user'}]}), False),
        ('poll-memory', dict(poll_r), 200, lambda: api_ok(dict(poll_r)),
         False),
        ('poll-db', dict(poll_r, status='interrupted'), 200,
         lambda: api_ok(dict(poll_r, status='interrupted')), False),
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


def test_conv_search_bare_array_coordination():
    """backend wraps (all THREE array sites, including the two early-empty
    returns); the api.js search seam unwraps with a fallback."""
    from lib.api_response import api_ok
    app = _make_app()

    async def _t():
        async with app.test_request_context('/test'):
            s, body = await _resolve(api_ok({'items': []}))
            assert s == 200 and body['items'] == []

    asyncio.run(_t())

    with open(_API_JS, encoding='utf-8') as f:
        src = f.read()
    m = re.search(r"search:\s*async\s*\(query, opts\)[^=]*=>\s*\{(?P<body>.*?)\n\s*\},",
                  src, re.DOTALL)
    assert m, 'Api.conversations.search is not the unwrapped async form'
    block = m.group('body')
    assert '.items' in block, 'conversations.search must unwrap .items'
    assert re.search(r'Array\.isArray\(d\)', block), (
        'conversations.search must keep an Array.isArray(d) fallback')


def test_shipped_source_converted():
    """All eight files carry no ad-hoc jsonify( and no flask jsonify
    import (RED-first tripwire)."""
    for rel in _CONVERTED:
        path = os.path.join(_ROOT, rel)
        with open(path, encoding='utf-8') as f:
            src = f.read()
        assert 'jsonify(' not in src, (
            f'{rel} still builds responses with bare jsonify(')
        assert not re.search(r'from flask import[^\n]*\bjsonify\b', src), (
            f'{rel} still imports jsonify')


if __name__ == '__main__':
    for fn in (test_envelope_parity,
               test_conv_search_bare_array_coordination,
               test_shipped_source_converted):
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
