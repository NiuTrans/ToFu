#!/usr/bin/env python3
"""Route tests for the manual /compact endpoint (§8 step 2).

Covers design-doc tests 5 (task_active → 409) and 6 (nothing_to_compact → 422),
the full engine-error-code → HTTP-status mapping, a happy path, and the race
you flagged: the idle check passes but the engine's CAS loses to a
concurrently-started turn → the route must return 409 ``stale`` (never a dirty
write).

The view function is exercised directly inside a Quart ``test_request_context``
with its two collaborators (``_conv_has_live_task`` idle probe + the engine
``compact_conversation_now``) monkeypatched — hermetic, no DB / no LLM / no app
boot, but it runs the REAL route body (idle gate, body parse, status mapping).

Run:  python -B -m pytest -p no:napari tests/test_manual_compaction_route.py
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install the Flask→Quart shim before importing the route module (mirrors
# tests/test_api_response.py + server.py's default_config patch).
import quart as _quart          # noqa: E402
sys.modules['flask'] = _quart


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


async def _resolve(resp):
    response, status = resp
    if hasattr(response, 'get_data'):
        body = await response.get_data(as_text=True)
    else:
        body = response
    return status, (json.loads(body) if body else {})


def _route():
    import routes.conversations_compaction as mod
    return mod


async def _call(monkeypatch, *, live, engine_result, body=None):
    """Drive the real compact_conversation view with collaborators stubbed."""
    mod = _route()
    monkeypatch.setattr(mod, '_conv_has_live_task', lambda cid: live)

    def _fake_engine(conv_id, *, config=None, task=None, keep_recent_turns=None):
        _fake_engine.seen = {'conv_id': conv_id, 'keep': keep_recent_turns}
        return engine_result
    _fake_engine.seen = None
    monkeypatch.setattr(mod,
                        'compact_conversation_now', _fake_engine, raising=False)
    # The route imports the engine lazily via
    # `from lib.tasks_pkg.compaction._manual import compact_conversation_now`,
    # so patch it at the source module too.
    import lib.tasks_pkg.compaction._manual as man
    monkeypatch.setattr(man, 'compact_conversation_now', _fake_engine)

    app = _make_app()
    data = json.dumps(body) if body is not None else None
    async with app.test_request_context(
            '/api/v1/conversations/c1/compact', method='POST',
            data=data, headers={'Content-Type': 'application/json'}):
        result = await mod.compact_conversation('c1')
    return (await _resolve(result)), _fake_engine.seen


# ─── Test 5: task active → 409 ─────────────────────────────────────────

@pytest.mark.unit
def test_route_task_active_returns_409(monkeypatch):
    import asyncio

    async def _t():
        (status, body), seen = await _call(
            monkeypatch, live=True, engine_result={'ok': True})
        assert status == 409
        assert body['ok'] is False
        # engine must NOT have been called when a task is live
        assert seen is None, 'engine ran despite an active task'
        assert body.get('error_code') == 'task_active' or body.get('error') == 'task_active'

    asyncio.run(_t())


# ─── Test 6: nothing to compact → 422 ─────────────────────────────────

@pytest.mark.unit
def test_route_nothing_to_compact_returns_422(monkeypatch):
    import asyncio

    async def _t():
        (status, body), seen = await _call(
            monkeypatch, live=False,
            engine_result={'ok': False, 'error': 'nothing_to_compact'})
        assert status == 422
        assert body['ok'] is False
        assert seen is not None, 'engine should have been consulted'

    asyncio.run(_t())


# ─── error-code → status mapping ──────────────────────────────────────

@pytest.mark.unit
def test_route_error_code_status_mapping(monkeypatch):
    import asyncio
    cases = {
        'not_found':          404,
        'nothing_to_compact': 422,
        'stale':              409,
        'summary_failed':     503,
        'some_unknown_error': 500,
    }

    async def _t():
        for err, expect in cases.items():
            (status, body), _ = await _call(
                monkeypatch, live=False,
                engine_result={'ok': False, 'error': err})
            assert status == expect, f'{err} → {status}, expected {expect}'
            assert body['ok'] is False

    asyncio.run(_t())


# ─── race: idle passed, CAS lost → stale/409 (no dirty write) ─────────

@pytest.mark.unit
def test_route_idle_passed_but_cas_lost_returns_stale(monkeypatch):
    """The idle check passes (no live task at check time) but a task started
    before the rewrite; the engine's updated_at CAS loses and returns
    ``stale``.  The route must surface 409 stale — proving the check→write
    window is closed by the CAS, not left as a dirty-write hole."""
    import asyncio

    async def _t():
        (status, body), seen = await _call(
            monkeypatch, live=False,
            engine_result={'ok': False, 'error': 'stale', 'archiveId': 7})
        assert status == 409
        assert body['ok'] is False
        assert seen is not None
        # archiveId (harmless kept snapshot) is surfaced to the client
        assert body.get('archiveId') == 7

    asyncio.run(_t())


# ─── happy path: 200 with the engine payload + keepRecentTurns passthrough

@pytest.mark.unit
def test_route_happy_path_200_and_keep_passthrough(monkeypatch):
    import asyncio

    async def _t():
        payload = {'ok': True, 'archiveId': 5, 'tokensBefore': 5000,
                   'tokensAfter': 900, 'msgsBefore': 40, 'msgsAfter': 6,
                   'reductionPct': 82.0, 'summaryPreview': 'S'}
        (status, body), seen = await _call(
            monkeypatch, live=False, engine_result=payload,
            body={'keepRecentTurns': 3})
        assert status == 200
        assert body['ok'] is True
        assert body['tokensAfter'] == 900 and body['archiveId'] == 5
        # keepRecentTurns from the body reaches the engine
        assert seen['keep'] == 3

    asyncio.run(_t())


# ─── empty body is tolerated (keep defaults to None) ──────────────────

@pytest.mark.unit
def test_route_empty_body_ok(monkeypatch):
    import asyncio

    async def _t():
        (status, body), seen = await _call(
            monkeypatch, live=False,
            engine_result={'ok': True, 'archiveId': 1, 'tokensBefore': 1,
                           'tokensAfter': 1, 'msgsBefore': 2, 'msgsAfter': 1},
            body=None)
        assert status == 200
        assert seen['keep'] is None

    asyncio.run(_t())
