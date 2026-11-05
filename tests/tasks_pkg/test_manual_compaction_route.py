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

Run:  python -B -m pytest -p no:napari tests/tasks_pkg/test_manual_compaction_route.py
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.support.envelope import v1_shaped

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
    # The request context targets a /api/v4 path, so error branches render
    # RFC 7807 problem documents; project back to the legacy shape.
    return status, v1_shaped(json.loads(body) if body else {})


def _route():
    import routes.conversations_compaction as mod
    return mod


async def _call(monkeypatch, *, live, engine_result, body=None, scheduled=None,
                busy_scheduled=None):
    """Drive the real compact_conversation view with collaborators stubbed."""
    mod = _route()
    monkeypatch.setattr(
        mod, '_conv_has_live_task', lambda cid, *, user_id: live)
    import lib.tasks_pkg.compaction._manual as man
    # Ordinary (never-coordinated) conversations classify as None.
    monkeypatch.setattr(man, 'schedule_protected_compaction',
                        lambda conv_id, *, user_id: scheduled)

    def _busy(conv_id, *, user_id):
        _busy.seen = (conv_id, user_id)
        return busy_scheduled
    _busy.seen = None
    monkeypatch.setattr(man, 'schedule_busy_compaction', _busy)

    def _fake_engine(
            conv_id, *, user_id, config=None, task=None,
            keep_recent_turns=None):
        _fake_engine.seen = {
            'conv_id': conv_id, 'user_id': user_id,
            'keep': keep_recent_turns,
        }
        return engine_result
    _fake_engine.seen = None
    monkeypatch.setattr(mod,
                        'compact_conversation_now', _fake_engine, raising=False)
    # The route imports the engine lazily via
    # `from lib.tasks_pkg.compaction._manual import compact_conversation_now`,
    # so patch it at the source module too.
    monkeypatch.setattr(man, 'compact_conversation_now', _fake_engine)

    app = _make_app()
    data = json.dumps(body) if body is not None else None
    async with app.test_request_context(
            '/api/v4/conversations/c1/compact', method='POST',
            data=data, headers={'Content-Type': 'application/json'}):
        from quart import g
        from lib.api_keys import local_admin_context
        g.auth_ctx = local_admin_context()
        result = await mod.compact_conversation('c1')
    return (await _resolve(result)), _fake_engine.seen


# ─── Test 5: task active → scheduled working view, never the rewrite ───

@pytest.mark.unit
def test_route_task_active_schedules_working_view_compaction(monkeypatch):
    import asyncio

    async def _t():
        marker = {'ok': True, 'scheduled': True, 'alreadyScheduled': False,
                  'requestId': 'r2', 'requestedAt': 5, 'protection': None,
                  'appliesAt': 'next_model_call'}
        (status, body), seen = await _call(
            monkeypatch, live=True, engine_result={'ok': True},
            busy_scheduled=marker)
        assert status == 200
        assert body['scheduled'] is True and body['requestId'] == 'r2'
        assert body['protection'] is None and body['appliesAt'] == 'next_model_call'
        # The destructive engine never runs beside a live task.
        assert seen is None, 'engine ran despite an active task'

    asyncio.run(_t())


@pytest.mark.unit
def test_cancel_route_withdraws_exactly_the_requested_marker(monkeypatch):
    import asyncio
    import lib.tasks_pkg.compaction._manual as man

    calls = []

    def _cancel(conv_id, *, user_id, request_id):
        calls.append((conv_id, request_id))
        return {'ok': True, 'cancelled': True, 'applying': False}

    monkeypatch.setattr(man, 'cancel_scheduled_compaction', _cancel)
    mod = _route()

    async def _t(request_id):
        app = _make_app()
        async with app.test_request_context(
                f'/api/v4/conversations/c1/compact/scheduled/{request_id}',
                method='DELETE'):
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()
            return await _resolve(
                await mod.cancel_scheduled_compaction('c1', request_id))

    status, body = asyncio.run(_t('abc123'))
    assert status == 200 and body['cancelled'] is True and body['applying'] is False
    assert calls == [('c1', 'abc123')]
    for bad in ('x' * 129, 'bad id', 'semi;colon'):
        status, body = asyncio.run(_t(bad))
        assert status == 400 and body['error']['message'] == 'invalid_request_id'
    assert calls == [('c1', 'abc123')]


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
        'turn_protocol_unsupported': 409,
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
        # archiveId (harmless kept snapshot) is a v1-wire extra; the strict
        # v4 problem document carries the stale signal as 409 + detail only.
        assert body['error']['message'] == 'stale'

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


# ─── protected conversations: scheduled working view, never a 409 ─────

@pytest.mark.unit
@pytest.mark.parametrize('live', [False, True])
def test_protected_conversation_is_scheduled_even_while_a_task_runs(monkeypatch, live):
    import asyncio

    async def _t():
        marker = {'ok': True, 'scheduled': True, 'alreadyScheduled': False,
                  'requestId': 'r1', 'requestedAt': 1, 'protection': 'coordinator'}
        (status, body), seen = await _call(
            monkeypatch, live=live, engine_result={'ok': True}, scheduled=marker)
        assert status == 200
        assert body['ok'] is True and body['scheduled'] is True
        assert body['requestId'] == 'r1' and body['protection'] == 'coordinator'
        assert seen is None, 'protected Turns must never reach the destructive engine'

    asyncio.run(_t())


@pytest.mark.unit
def test_protected_classification_errors_keep_their_status(monkeypatch):
    import asyncio

    async def _t():
        for err, expect in {'not_found': 404, 'turn_protocol_unsupported': 409}.items():
            (status, body), seen = await _call(
                monkeypatch, live=False, engine_result={'ok': True},
                scheduled={'ok': False, 'error': err})
            assert status == expect and body['ok'] is False and seen is None

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


# ─── a body the caller cannot have meant as JSON is a 400, never a 500 ───

@pytest.mark.unit
@pytest.mark.parametrize('raw', [b'{not json', b'\xff\xfe{"config": {}}'])
def test_route_undecodable_body_is_invalid_json_400(monkeypatch, raw):
    """Malformed JSON and a non-UTF-8 body are both the caller's fault."""
    import asyncio

    async def _t():
        mod = _route()
        monkeypatch.setattr(
            mod, '_conv_has_live_task', lambda cid, *, user_id: False)

        def _engine(*_args, **_kwargs):
            raise AssertionError('engine ran for an undecodable body')

        import lib.tasks_pkg.compaction._manual as man
        monkeypatch.setattr(man, 'compact_conversation_now', _engine)
        app = _make_app()
        async with app.test_request_context(
                '/api/v4/conversations/c1/compact', method='POST',
                data=raw, headers={'Content-Type': 'application/json'}):
            from quart import g
            from lib.api_keys import local_admin_context
            g.auth_ctx = local_admin_context()
            result = await mod.compact_conversation('c1')
        status, body = await _resolve(result)
        assert status == 400
        assert body['ok'] is False
        assert body['error']['message'] == 'invalid_json'

    asyncio.run(_t())
