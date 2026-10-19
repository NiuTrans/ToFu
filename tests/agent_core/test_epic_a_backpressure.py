#!/usr/bin/env python3
"""Root/SSE lifetime accounting and the separate open-mode authority boundary."""
import importlib
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

pytestmark = pytest.mark.unit


# ══════════════════════════════════════════════════════════════════════
#  Cap 1 — per-principal concurrent-SSE semaphore (re-keyed onto the shared
#  runtime_state_store atomic acquire_slot; token contract, N-invariant)
# ══════════════════════════════════════════════════════════════════════
def _reset_state_store():
    """Reset the shared runtime-state store so each test starts clean (the SSE
    cap + admission controller now count their slots there)."""
    import lib.runtime.runtime_state_store as rss
    rss.reset_for_test()


def test_sse_explicit_zero_cap_preserves_unbounded_escape_hatch():
    from lib.agent_core.sse_limit import SSELimiter
    limiter = SSELimiter(cap=0)
    tokens = [limiter.try_acquire('owner:7') for _ in range(256)]
    foreign = limiter.try_acquire('owner:8')
    assert len(set(tokens)) == 256
    assert limiter.active('owner:7') == 256
    limiter.release(foreign)
    for token in tokens:
        limiter.refresh(token)
        limiter.release(token)
        limiter.release(token)
    assert limiter.active('owner:7') == limiter.active('owner:8') == 0


def test_sse_probed_cap_enforces_per_principal_refusal(monkeypatch):
    from lib.agent_core.sse_limit import SSELimiter
    monkeypatch.setenv('TOFU_MAX_SSE_PER_PRINCIPAL', '2')
    limiter = SSELimiter()
    assert limiter.cap == 2
    first = limiter.try_acquire('owner:7')
    second = limiter.try_acquire('owner:7')
    assert first is not None and second is not None
    assert limiter.try_acquire('owner:7') is None
    # A different principal retains its own independent budget.
    assert limiter.try_acquire('owner:8') is not None
    limiter.release(first)
    assert limiter.try_acquire('owner:7') is not None


def test_sse_explicit_cap_refuses_above_limit():
    from lib.agent_core.sse_limit import SSELimiter
    limiter = SSELimiter(cap=1)
    assert limiter.cap == 1
    token = limiter.try_acquire('owner:7')
    assert token is not None
    assert limiter.try_acquire('owner:7') is None
    limiter.release(token)
    assert limiter.try_acquire('owner:7') is not None


def test_root_accounting_accepts_every_concurrent_execution():
    """Same atomic guarantee for the admission ceiling under a thread race."""
    import threading
    _reset_state_store()
    from lib.agent_core.admission import AdmissionController
    c = AdmissionController(max_inflight=10)
    granted = []
    g_lock = threading.Lock()
    barrier = threading.Barrier(80)

    def _w():
        barrier.wait()
        if c.try_acquire():
            with g_lock:
                granted.append(1)

    threads = [threading.Thread(target=_w) for _ in range(80)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 80, 'root accounting must accept every concurrent task'
    assert c.in_flight == 80


# ══════════════════════════════════════════════════════════════════════
#  Cap 2 — admission on the UI start path
# ══════════════════════════════════════════════════════════════════════
def test_root_accounting_accepts_above_old_capacity_and_releases():
    _reset_state_store()
    from lib.agent_core.admission import AdmissionController
    c = AdmissionController(max_inflight=2)
    assert c.try_acquire() is True
    assert c.try_acquire() is True
    assert c.try_acquire() is True        # old capacity is no longer admission
    c.release()
    assert c.try_acquire() is True


def test_root_accounting_never_refuses_burst():
    """Accounting carries ownership without imposing a local task ceiling."""
    _reset_state_store()
    from lib.agent_core.admission import AdmissionController
    c = AdmissionController(max_inflight=0)
    for _ in range(10_000):
        assert c.try_acquire() is True    # every execution remains tracked


def test_admission_on_terminal_releases_slot():
    """The UI start path releases the slot via on_terminal (fires from the
    orchestrator worker thread on the task's terminal event). Verify the
    seam: registering on_terminal then firing the terminal callback runs the
    release exactly once."""
    _reset_state_store()
    from lib.agent_core.admission import (
        AdmissionController, on_terminal, fire_terminal_callbacks)
    c = AdmissionController(max_inflight=1)
    assert c.try_acquire() is True
    assert c.in_flight == 1
    on_terminal('task-xyz', lambda _tid: c.release())
    fire_terminal_callbacks('task-xyz')   # simulates the terminal event
    assert c.in_flight == 0
    assert c.try_acquire() is True


def test_open_mode_has_no_peer_resource_admission(monkeypatch):
    from lib.api_keys import AuthContext
    from lib.rate_limit.rate_limit_api import check_request
    import lib.rate_limit.rate_limit_store as store
    monkeypatch.setenv('TOFU_OPEN_MODE_RPM', '1')
    monkeypatch.setattr(store, 'get_store', lambda: pytest.fail('peer quota store consulted'))
    context = AuthContext(key_id='', name='open', scopes=frozenset({'admin'}),
                          via_open_mode=True)
    assert all(check_request(context).allowed for _ in range(4097))


# ══════════════════════════════════════════════════════════════════════
#  WIRING — drive the actual route/generator boundaries. These assertions
#  deliberately avoid shipped-source anchors: a renamed helper is harmless
#  when the externally visible cap, heartbeat and release behavior survives.
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_chat_sse_accounting_keeps_owner_identity_without_quota_response(flask_app):
    import routes.api_v1.chat as chat
    auth = SimpleNamespace(owner_user_id=47, key_id='rotatable-key')
    async with flask_app.app_context():
        token, rejection = chat._try_acquire_sse_slot(auth)
    try:
        assert token and rejection is None
        assert chat.sse_limiter.active('owner:47') == 1
    finally:
        chat.sse_limiter.release(token)
    assert chat.sse_limiter.active('owner:47') == 0


@pytest.mark.anyio
async def test_chat_stream_close_releases_observer_slot_without_aborting_task(
        monkeypatch):
    from types import SimpleNamespace

    import lib.task_replay.task_replay as replay
    import routes.api_v1.chat as chat

    monkeypatch.setattr(
        replay,
        'task_memory_replay_page',
        lambda _task, _cursor: SimpleNamespace(events=[], next_cursor=0),
    )

    async def idle_wait(_task_id, *, timeout):
        assert timeout == 15.0
        return False

    refreshed = []
    released = []
    unregistered = []
    monkeypatch.setattr(chat, 'wait_for_event', idle_wait)
    monkeypatch.setattr(chat.sse_limiter, 'refresh', refreshed.append)
    monkeypatch.setattr(chat.sse_limiter, 'release', released.append)
    monkeypatch.setattr(chat, 'unregister_waiter', unregistered.append)

    task = {'id': 'stream-task', 'status': 'running', 'aborted': False}
    stream = chat._stream_generator(
        task,
        'test-model',
        'completion-id',
        sse_slot_token='owner-slot',
    )
    assert await anext(stream) == ': heartbeat\n\n'
    assert refreshed == ['owner-slot']
    await stream.aclose()
    assert released == ['owner-slot']
    assert unregistered == ['stream-task']
    assert task['status'] == 'running'
    assert task['aborted'] is False


@pytest.mark.anyio
async def test_chat_completion_admission_refusal_releases_stream_slot(
        flask_app, monkeypatch):
    from types import SimpleNamespace
    from lib.agent_core.execution_session import ExecutionSession

    import lib.tasks_pkg.manager as task_manager
    import routes.api_v1.chat as chat

    monkeypatch.setattr(
        task_manager,
        'create_task',
        lambda *_args, **_kwargs: {
            'id': 'refused-task',
            'status': 'pending',
            '_executionSession': ExecutionSession(
                execution_id='refused-task', kind='chat', owner_user_id=1),
        },
    )
    rejected = []
    monkeypatch.setattr(
        task_manager,
        'reject_unstarted_chat_task',
        lambda task, _error, *, cause, conv_id='': rejected.append(
            (task['id'], cause, conv_id)),
    )
    monkeypatch.setattr(chat.controller, 'acquire', lambda: None)
    monkeypatch.setattr(chat.sse_limiter, 'try_acquire', lambda _key: 'slot')
    released = []
    monkeypatch.setattr(chat.sse_limiter, 'release', released.append)
    # This backpressure contract is deliberately storage-free. Supply the
    # already-authorized route-group seam so the test reaches admission while
    # still sending the native structured model shape.
    route_group = SimpleNamespace(pin_id='test-route')
    selection = SimpleNamespace(
        model=SimpleNamespace(public_dict=lambda: {
            'creator_id': 'tofu-test', 'model_id': 'stub-model',
        }),
        provider_offering=None,
    )
    monkeypatch.setattr(
        chat, 'mint_native_request_route',
        lambda *_args, **_kwargs: ('stub-model', selection, route_group),
    )
    monkeypatch.setattr(
        chat, 'guard_model_relay_or_dispose', lambda _group: None)
    disposed = []
    monkeypatch.setattr(
        chat, 'dispose_routed_slot_group', disposed.append)

    response = await flask_app.test_client().post(
        '/api/v4/chat/completions',
        json={
            'model': {'creator_id': 'tofu-test', 'model_id': 'stub-model'},
            'messages': [{'role': 'user', 'content': 'bounded'}],
            'stream': True,
        },
    )
    assert response.status_code == 503
    assert released == ['slot']
    assert disposed == [route_group]
    assert rejected[0][0] == 'refused-task'
    assert rejected[0][1] == 'task_admission_refused'
    assert rejected[0][2].startswith('api-')


@pytest.mark.anyio
async def test_direct_chat_stream_releases_admission_and_sse_slots(
        flask_app, monkeypatch):
    import routes.api_v1.chat as chat
    import routes.api_v1.chat_direct as direct

    async def frames():
        yield 'data: [DONE]\n\n'

    route_group = SimpleNamespace(pin_id='direct-route')
    selection = SimpleNamespace(
        model=SimpleNamespace(public_dict=lambda: {
            'creator_id': 'tofu-test', 'model_id': 'stub-model',
        }),
        provider_offering=None,
    )
    monkeypatch.setattr(
        direct, 'mint_native_request_route',
        lambda *_args, **_kwargs: ('stub-model', selection, route_group),
    )
    monkeypatch.setattr(
        direct, 'guard_model_relay_or_dispose', lambda _group: None)
    disposed = []
    monkeypatch.setattr(
        direct, 'dispose_routed_slot_group', disposed.append)

    monkeypatch.setattr(direct, 'run_direct_stream',
                        lambda *_args, **_kwargs: frames())
    monkeypatch.setattr(direct.controller, 'acquire', lambda: 'admission-lease')
    admission_releases = []
    monkeypatch.setattr(direct.controller, 'release',
                        lambda lease: admission_releases.append(lease) or True)
    monkeypatch.setattr(chat.sse_limiter, 'try_acquire', lambda _key: 'slot')
    refreshed = []
    released = []
    monkeypatch.setattr(chat.sse_limiter, 'refresh', refreshed.append)
    monkeypatch.setattr(chat.sse_limiter, 'release', released.append)

    response = await flask_app.test_client().post(
        '/api/v4/chat/stream-direct',
        json={
            'model': {'creator_id': 'tofu-test', 'model_id': 'stub-model'},
            'messages': [{'role': 'user', 'content': 'bounded'}],
        },
    )
    assert response.status_code == 200
    assert await response.get_data(as_text=True) == 'data: [DONE]\n\n'
    assert refreshed == ['slot']
    assert released == ['slot']
    assert admission_releases == ['admission-lease']
    assert disposed == [route_group]


@pytest.mark.anyio
async def test_direct_chat_terminal_settles_usage_billing_and_route_once(
        flask_app, monkeypatch):
    import routes.api_v1.chat as chat
    import routes.api_v1.chat_direct as direct
    from lib.llm.stream_result import ProviderStreamResult

    auth = SimpleNamespace(
        owner_user_id=1, tenant_id=None, account_user_id='account-1',
        key_id='key-1', name='test-key',
    )
    route_group = SimpleNamespace(pin_id='direct-billed-route')
    selection = SimpleNamespace(
        model=SimpleNamespace(public_dict=lambda: {
            'creator_id': 'tofu-test', 'model_id': 'stub-model',
        }),
        provider_offering=None,
    )
    monkeypatch.setattr(direct, 'current_auth', lambda: auth)
    monkeypatch.setattr(
        direct, 'mint_native_request_route',
        lambda *_args, **_kwargs: ('stub-model', selection, route_group),
    )
    monkeypatch.setattr(
        direct, 'guard_model_relay_or_dispose', lambda _group: None)

    reservations = []
    monkeypatch.setattr(
        direct, 'reserve_for_task',
        lambda task, **kwargs: reservations.append((task['id'], kwargs)) or 17,
    )
    settlements = []

    def settle(task, **kwargs):
        settlements.append((dict(task['usage']), kwargs))
        return {'cost_micro': 3, 'reserved_micro': 17}

    monkeypatch.setattr(direct, 'settle_task', settle)
    disposed = []
    monkeypatch.setattr(
        direct, 'dispose_routed_slot_group', disposed.append)
    monkeypatch.setattr(direct, 'record_tokens', lambda *_args: None)
    monkeypatch.setattr(direct, 'record_usage', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(direct.controller, 'acquire', lambda: 'admission-lease')
    admission_releases = []
    monkeypatch.setattr(
        direct.controller, 'release',
        lambda lease: admission_releases.append(lease) or True)
    monkeypatch.setattr(chat.sse_limiter, 'try_acquire', lambda _key: 'slot')
    monkeypatch.setattr(chat.sse_limiter, 'refresh', lambda _token: None)
    monkeypatch.setattr(chat.sse_limiter, 'release', lambda _token: None)

    async def frames(*_args, **kwargs):
        kwargs['on_dispatch_started']()
        kwargs['dispatch_terminal']['result'] = ProviderStreamResult.from_legacy(
            {'content': 'ok'}, 'stop', {
                'prompt_tokens': 4,
                'completion_tokens': 2,
                '_dispatch': {'provider_id': 'provider-1'},
            },
        )
        await kwargs['on_dispatch_settled']()
        yield 'data: [DONE]\n\n'

    monkeypatch.setattr(direct, 'run_direct_stream', frames)

    response = await flask_app.test_client().post(
        '/api/v4/chat/stream-direct',
        json={
            'model': {'creator_id': 'tofu-test', 'model_id': 'stub-model'},
            'messages': [{'role': 'user', 'content': 'bounded'}],
        },
    )

    assert response.status_code == 200
    assert await response.get_data(as_text=True) == 'data: [DONE]\n\n'
    assert len(reservations) == 1
    assert len(settlements) == 1
    settled_usage, settled_identity = settlements[0]
    assert settled_usage['prompt_tokens'] == 4
    assert settled_usage['completion_tokens'] == 2
    assert settled_usage['_dispatch']['provider_id'] == 'provider-1'
    assert settled_identity == {
        'user_id': 'account-1', 'model': 'stub-model',
        'raise_on_error': True}
    assert disposed == [route_group]
    assert admission_releases == ['admission-lease']


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
