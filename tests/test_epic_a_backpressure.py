#!/usr/bin/env python3
"""Epic A — DoS / backpressure caps for a public-facing deployment.

Three independent caps, each with its own NEGATIVE CONTROL:

  1. Per-principal concurrent-SSE semaphore (``lib/agent_core/sse_limit.py``)
     — bounds long-lived streams per user/IP so one actor can't exhaust the
     single-process ASGI server. The slot MUST be released even on a
     dropped/aborted stream (leak → the cap silently stops firing).

  2. Admission on the UI ``/chat/start`` path (``lib/agent_core/admission.py``
     ``controller``) — the SAME global inflight ceiling the headless paths
     use, so a UI spawn storm can't exhaust the agent-worker pool.

  3. Per-IP throttle for the open-mode synthetic context
     (``lib/rate_limit_api.check_open_mode_request``) — closes the
     "open+remote = unauthenticated AND unthrottled" hole.

Bare-CI-safe: pure in-process objects, no DB / node / network.
"""
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


def test_sse_limiter_caps_per_principal_and_releases():
    _reset_state_store()
    from lib.agent_core.sse_limit import SSELimiter
    lim = SSELimiter(cap=2)
    p = 'ip:1.2.3.4'
    t1 = lim.try_acquire(p)
    t2 = lim.try_acquire(p)
    assert t1 is not None and t2 is not None        # tokens, not booleans
    # Third concurrent stream for the SAME principal is refused (atomic cap).
    assert lim.try_acquire(p) is None
    assert lim.active(p) == 2
    # A DIFFERENT principal is independent (not affected by p's saturation).
    assert lim.try_acquire('ip:9.9.9.9') is not None
    # Releasing one BY TOKEN frees exactly one slot for p.
    lim.release(t1)
    assert lim.active(p) == 1
    t4 = lim.try_acquire(p)
    assert t4 is not None
    assert lim.try_acquire(p) is None


def test_sse_limiter_release_frees_exactly_that_slot():
    """Release is by TOKEN (each stream owns a distinct slot); releasing an
    unheld/None token is a safe no-op, and a released principal drops to 0
    active so distinct one-shot IPs don't accumulate."""
    _reset_state_store()
    from lib.agent_core.sse_limit import SSELimiter
    lim = SSELimiter(cap=4)
    tok = lim.try_acquire('ip:a')
    assert lim.active('ip:a') == 1
    lim.release(None)                # no-op, no crash
    lim.release(tok)
    assert lim.active('ip:a') == 0


def test_sse_limiter_cap_zero_disables():
    _reset_state_store()
    from lib.agent_core.sse_limit import SSELimiter
    lim = SSELimiter(cap=0)
    for _ in range(1000):
        assert lim.try_acquire('ip:x') is not None  # token even when disabled


def test_NC_sse_slot_leak_defeats_the_cap():
    """NEGATIVE CONTROL for the release contract: if a stream forgets to
    release its slot (the leak bug the ``finally`` in chat_stream prevents),
    the principal is permanently stuck at capacity and NO further stream for
    that principal is ever admitted — proving the cap depends on release.

    We simulate the leak by acquiring cap times and NOT releasing; the next
    acquire must be refused. Then we show the CORRECT path (release by token)
    restores capacity."""
    _reset_state_store()
    from lib.agent_core.sse_limit import SSELimiter
    lim = SSELimiter(cap=3)
    p = 'key:leaky'
    held = [lim.try_acquire(p) for _ in range(3)]
    assert all(t is not None for t in held)
    # Leaked (never released) → locked out.
    assert lim.try_acquire(p) is None
    # The fix is release; with it, capacity returns.
    lim.release(held[0])
    assert lim.try_acquire(p) is not None


def test_env_default_cap_is_positive():
    """The shipped default must be a FINITE positive cap (not 0/disabled),
    else the DoS protection is off by default."""
    import lib.agent_core.sse_limit as m
    old = os.environ.pop('TOFU_MAX_SSE_PER_PRINCIPAL', None)
    try:
        importlib.reload(m)
        assert m.SSELimiter().cap > 0
    finally:
        if old is not None:
            os.environ['TOFU_MAX_SSE_PER_PRINCIPAL'] = old
        importlib.reload(m)


def test_sse_cap_never_overshoots_under_concurrency():
    """Re-key correctness: the SSE cap is now backed by the store's ATOMIC
    acquire_slot, so many threads racing to open streams for the SAME
    principal admit EXACTLY `cap`, never more. (A check-then-act cap would
    overshoot — see test_NC_check_then_act_overshoots_under_concurrency in
    tests/test_runtime_state_store.py, the store-level bite.)"""
    import threading
    _reset_state_store()
    from lib.agent_core.sse_limit import SSELimiter
    lim = SSELimiter(cap=10)
    p = 'ip:race'
    tokens = []
    tok_lock = threading.Lock()
    barrier = threading.Barrier(80)

    def _w():
        barrier.wait()
        t = lim.try_acquire(p)
        if t is not None:
            with tok_lock:
                tokens.append(t)

    threads = [threading.Thread(target=_w) for _ in range(80)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(tokens) == 10, f'SSE cap overshot: admitted {len(tokens)} != 10'
    assert lim.active(p) == 10


def test_admission_cap_never_overshoots_under_concurrency():
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
    assert len(granted) == 10, f'admission overshot: {len(granted)} != 10'
    assert c.in_flight == 10


# ══════════════════════════════════════════════════════════════════════
#  Cap 2 — admission on the UI start path
# ══════════════════════════════════════════════════════════════════════
def test_admission_controller_caps_and_releases():
    _reset_state_store()
    from lib.agent_core.admission import AdmissionController
    c = AdmissionController(max_inflight=2)
    assert c.try_acquire() is True
    assert c.try_acquire() is True
    assert c.try_acquire() is False       # at capacity → UI start returns 503
    c.release()
    assert c.try_acquire() is True


def test_NC_admission_unbounded_never_refuses():
    """NEGATIVE CONTROL: an unbounded controller (cap=0, the pre-cap state)
    NEVER refuses — so wiring try_acquire into /chat/start only protects the
    server when the ceiling is > 0. Proves the gate is load-bearing."""
    _reset_state_store()
    from lib.agent_core.admission import AdmissionController
    c = AdmissionController(max_inflight=0)
    for _ in range(10_000):
        assert c.try_acquire() is True    # unbounded → UI storm not capped


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
    assert c.try_acquire() is False
    on_terminal('task-xyz', lambda _tid: c.release())
    fire_terminal_callbacks('task-xyz')   # simulates the terminal event
    assert c.try_acquire() is True        # slot was returned


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
            via_tunnel_token = False
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
#  WIRING — the three caps are actually integrated into the routes,
#  not just present as unused library primitives.
# ══════════════════════════════════════════════════════════════════════
import ast

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CHAT_PY = os.path.join(_ROOT, 'routes', 'chat.py')
_RL_PY = os.path.join(_ROOT, 'lib', 'rate_limit_api.py')


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def test_wiring_chat_stream_acquires_and_releases_sse_slot():
    src = open(_CHAT_PY, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = _find_func(tree, 'chat_stream')
    assert fn is not None
    scoped = ast.get_source_segment(src, fn)
    # Acquire keyed by the resolved principal, and a 429 refusal path.
    assert '_sse_limiter.try_acquire(' in scoped, 'chat_stream must acquire an SSE slot'
    assert 'principal_key(' in scoped, 'SSE slot must be keyed by principal'
    assert '429' in scoped, 'over-cap SSE must return 429'
    # Release must live in a finally (leak-proof on drop/abort).
    assert '_sse_limiter.release(' in scoped, 'chat_stream must release the SSE slot'
    assert 'finally:' in scoped


def test_wiring_chat_start_has_admission_gate():
    src = open(_CHAT_PY, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = _find_func(tree, 'chat_start')
    assert fn is not None
    scoped = ast.get_source_segment(src, fn)
    assert '_admission.try_acquire()' in scoped, 'UI /chat/start must gate on admission'
    assert '503' in scoped, 'over-capacity UI start must return 503'
    # Slot released via on_terminal AND on the spawn-failure path (no leak).
    assert 'on_terminal(' in scoped, 'admission slot must release via on_terminal'
    assert scoped.count('_admission.release()') >= 1, (
        'spawn-failure path must release the admission slot (terminal never fires)')


def test_wiring_check_request_routes_open_mode_to_ip_throttle():
    src = open(_RL_PY, encoding='utf-8').read()
    tree = ast.parse(src)
    fn = _find_func(tree, 'check_request')
    assert fn is not None
    scoped = ast.get_source_segment(src, fn)
    # The via_open_mode branch must call the IP throttle, not just return allowed.
    assert 'via_open_mode' in scoped
    assert 'check_open_mode_request(' in scoped, (
        'open-mode ctx must route through the per-IP throttle, not bypass')
    # And the throttle itself must delegate to the SHARED store (replica-correct
    # under the db backend), not a fresh in-process dict.
    omr = _find_func(tree, 'check_open_mode_request')
    assert omr is not None
    omr_src = ast.get_source_segment(src, omr)
    assert 'rate_limit_store' in omr_src and 'record_and_check' in omr_src, (
        'check_open_mode_request must delegate to lib.rate_limit_store '
        '(shared/pluggable), not a per-process counter dict')
    assert '_ip_state' not in src, (
        'the duplicate in-process _ip_state dict must be gone (replaced by the '
        'shared store)')


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
