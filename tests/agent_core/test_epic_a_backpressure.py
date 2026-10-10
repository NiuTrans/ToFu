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
    import lib.runtime_state_store as rss
    rss.reset_for_test()


def test_sse_counts_all_connections_without_refusing_old_cap():
    from lib.agent_core.sse_limit import SSELimiter
    limiter = SSELimiter(cap=1)
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


def test_sse_old_environment_cannot_restore_count_or_expiration_gate(monkeypatch):
    from lib.agent_core.sse_limit import SSELimiter
    monkeypatch.setenv('TOFU_MAX_SSE_PER_PRINCIPAL', '1')
    monkeypatch.setenv('TOFU_SSE_SLOT_TTL', '1')
    limiter = SSELimiter()
    tokens = [limiter.try_acquire('owner:7') for _ in range(3)]
    assert limiter.cap == 0
    for token in tokens:
        limiter.release(token)
    assert limiter.active('owner:7') == 0


def test_concurrent_sse_accounting_is_exact_and_unlimited():
    from concurrent.futures import ThreadPoolExecutor
    from lib.agent_core.sse_limit import SSELimiter
    limiter = SSELimiter(cap=1)
    with ThreadPoolExecutor(max_workers=8) as pool:
        tokens = list(pool.map(lambda index: limiter.try_acquire('owner:7'), range(256)))
        assert len(set(tokens)) == limiter.active('owner:7') == 256
        list(pool.map(limiter.release, tokens))
    assert limiter.active('owner:7') == 0


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


# ══════════════════════════════════════════════════════════════════════
#  Cap 3 — open-mode per-IP throttle (delegated to the SHARED store so it is
#  replica-correct under TOFU_RATE_LIMIT_BACKEND=db, NOT a per-process dict)
# ══════════════════════════════════════════════════════════════════════
def _reset_rate_store():
    """Force the shared store to rebuild with a clean counter (memory backend)."""
    import lib.rate_limit_store as store
    store.reset_for_test()


def test_open_mode_ip_throttle_fires():
    import lib.rate_limit_api as rl
    os.environ['TOFU_OPEN_MODE_RPM'] = '3'
    os.environ['TOFU_RATE_LIMIT_BACKEND'] = 'memory'
    try:
        importlib.reload(rl)
        _reset_rate_store()
        ip = '203.0.113.7'
        allowed = [rl.check_open_mode_request(client_ip=ip).allowed
                   for _ in range(5)]
        # First 3 allowed (the rpm cap), then throttled.
        assert allowed[:3] == [True, True, True]
        assert allowed[3] is False and allowed[4] is False
        d = rl.check_open_mode_request(client_ip=ip)
        assert d.allowed is False and d.reason == 'rpm' and d.retry_after_s > 0
        # A different IP has its own independent window.
        assert rl.check_open_mode_request(client_ip='198.51.100.1').allowed is True
    finally:
        os.environ.pop('TOFU_OPEN_MODE_RPM', None)
        os.environ.pop('TOFU_RATE_LIMIT_BACKEND', None)
        _reset_rate_store()
        importlib.reload(rl)


def test_open_mode_ctx_routed_through_ip_throttle():
    """check_request must route a via_open_mode context to the per-IP throttle,
    NOT bypass it. With a tiny cap the synthetic ctx is throttled after the
    budget — the exact hole (open+remote unthrottled) this closes."""
    import lib.rate_limit_api as rl
    os.environ['TOFU_OPEN_MODE_RPM'] = '2'
    os.environ['TOFU_RATE_LIMIT_BACKEND'] = 'memory'
    try:
        importlib.reload(rl)
        _reset_rate_store()

        class _Ctx:
            via_open_mode = True
            key_id = ''
            rate_limit_rpm = 0
            rate_limit_tpd = 0

        ctx = _Ctx()
        # Route through the PUBLIC check_request entry (not the helper directly)
        # so this bites the actual via_open_mode branch.
        results = [rl.check_request(ctx).allowed for _ in range(3)]
        assert results == [True, True, False]
    finally:
        os.environ.pop('TOFU_OPEN_MODE_RPM', None)
        os.environ.pop('TOFU_RATE_LIMIT_BACKEND', None)
        _reset_rate_store()
        importlib.reload(rl)


def test_open_mode_delegates_to_shared_store_not_inproc_dict():
    """The throttle MUST go through lib.rate_limit_store.get_store(), the
    pluggable seam — NOT a fresh in-process dict. We spy on the store's
    record_and_check and assert it is invoked with the open-mode endpoint +
    the IP + the configured rpm limit. This is what makes the cap
    replica-correct under the db backend (a duplicate in-proc dict would
    NEVER touch the store and would multiply by N across replicas)."""
    import lib.rate_limit_api as rl
    import lib.rate_limit_store as store
    os.environ['TOFU_OPEN_MODE_RPM'] = '5'
    try:
        importlib.reload(rl)
        calls = []

        class _SpyStore:
            def record_and_check(self, endpoint, ip, limit, per_seconds):
                calls.append((endpoint, ip, limit, per_seconds))
                return True, 1

        _orig = store.get_store
        store.get_store = lambda: _SpyStore()
        try:
            rl.check_open_mode_request(client_ip='192.0.2.44')
        finally:
            store.get_store = _orig
        assert len(calls) == 1, 'open-mode throttle did not call the shared store'
        endpoint, ip, limit, per_seconds = calls[0]
        assert ip == '192.0.2.44'
        assert limit == 5           # the configured TOFU_OPEN_MODE_RPM
        assert per_seconds == 60    # a 1-minute window
    finally:
        os.environ.pop('TOFU_OPEN_MODE_RPM', None)
        importlib.reload(rl)


def test_NC_open_mode_bypasses_store_multiplies_across_replicas():
    """NEGATIVE CONTROL for the shared-store requirement: if the throttle used
    a per-PROCESS dict instead of the shared store (the bug this fold-in
    fixes), then N independent 'replicas' would each admit up to `rpm`
    requests for the SAME ip → the effective cap is rpm×N. We simulate two
    replicas: with the SHARED store both replicas see one combined window
    (total admits == rpm); with a PER-PROCESS counter they'd admit rpm each
    (== rpm×N). The shared-store path must produce the combined count."""
    import lib.rate_limit_store as store
    # Two 'replicas' backed by the SAME store object == the db backend's
    # shared table. Under the shared store, IP 'x' gets ONE window of `rpm`.
    store.reset_for_test()
    shared = store.MemoryRateLimitStore()  # stands in for the shared db table
    rpm = 4
    admits = 0
    # Alternate replicas A/B hitting the SAME shared store.
    for _ in range(10):
        ok, _ = shared.record_and_check('open_mode', 'ip:x', limit=rpm, per_seconds=60)
        admits += 1 if ok else 0
    assert admits == rpm, (
        f'shared store admitted {admits} (expected {rpm}); a per-process dict '
        f'would admit rpm×replicas — the multiply-by-N bug')

    # Contrast: two SEPARATE per-process stores (the buggy in-proc-dict shape)
    # each admit `rpm` for the same IP → rpm×2, the exact regression.
    a, b = store.MemoryRateLimitStore(), store.MemoryRateLimitStore()
    buggy_admits = 0
    for _ in range(10):
        ok, _ = a.record_and_check('open_mode', 'ip:x', limit=rpm, per_seconds=60)
        buggy_admits += 1 if ok else 0
    for _ in range(10):
        ok, _ = b.record_and_check('open_mode', 'ip:x', limit=rpm, per_seconds=60)
        buggy_admits += 1 if ok else 0
    assert buggy_admits == rpm * 2, (
        'per-process stores must multiply the cap by replica count — this is '
        'the bug the shared store prevents')


def test_NC_open_mode_rpm_zero_is_unthrottled():
    """NEGATIVE CONTROL: TOFU_OPEN_MODE_RPM=0 (or the old unconditional
    allowed=True) means open mode is NOT throttled — a single IP can hammer
    forever. Proves the cap is what stops the abuse; disabling it reopens the
    hole exactly."""
    import lib.rate_limit_api as rl
    os.environ['TOFU_OPEN_MODE_RPM'] = '0'
    try:
        importlib.reload(rl)
        _reset_rate_store()
        for _ in range(1000):
            assert rl.check_open_mode_request(client_ip='192.0.2.9').allowed is True
    finally:
        os.environ.pop('TOFU_OPEN_MODE_RPM', None)
        _reset_rate_store()
        importlib.reload(rl)


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

    import lib.task_replay as replay
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
