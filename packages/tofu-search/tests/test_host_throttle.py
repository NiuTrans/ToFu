"""Per-engine-host request throttle — offline concurrency tests.

Root cause pinned (see the ACL-26 "no results" trace): two PARALLEL search
calls (two concurrent recommend batches) fired the same query at DDG-HTML
within the same second and drove it into ``202 (rate-limited)`` — a
self-inflicted throttle. The fix is a process-global, per-engine minimum
inter-request interval enforced inside ``http_search_get`` just before
``search_session.get``, so:

  (i)  two CONCURRENT calls to the SAME engine serialize to >= the interval;
  (ii) two calls to DIFFERENT engines are NOT serialized against each other
       (a per-engine lock, never one global lock that would re-serialize the
       whole fleet and kill the engine+fetch overlap);
  (iii) a small upward jitter desynchronizes two threads that would otherwise
        re-collide on the next tick.

Ordering requirement: the throttle wait sits AFTER the circuit-breaker skip
(a benched engine spends zero interval) and BEFORE the actual GET, and is
clamped to the per-request ``timeout`` so it consumes budget the caller
already has rather than pushing a slow query past its deadline.

The academic / JSON vertical path (``vertical.base.http_get``) is a SEPARATE
envelope and is deliberately NOT throttled — that's the breaker-independent
fast path the recommend engine relies on.

All offline: no network. The throttle is a pure timing primitive driven by
threads + monotonic clock; the wiring test fakes ``search_session.get``.
"""

import os
import threading
import time

import pytest

import tofu_search
from tofu_search.search import _common as common


@pytest.fixture(autouse=True)
def _reset_host_throttle():
    """Reset the process-global throttle around every test.

    Mirrors the ``engine_circuit``-not-reset-by-conftest lesson: this module
    global is NOT reset by the shared conftest, so a leaked per-engine
    timestamp from one test would skew the next. (conftest already restores
    the SearchConfig singleton.)
    """
    ht = getattr(common, 'host_throttle', None)
    if ht is not None:
        ht.reset()
    try:
        yield
    finally:
        if ht is not None:
            ht.reset()


def _fire_times(engine, n, *, interval_ms, seed=True):
    """Run ``n`` threads that each call ``host_throttle.wait(engine)`` at a
    shared barrier, and return the monotonic instant each wait RETURNED (i.e.
    the moment that thread's GET would fire)."""
    tofu_search.configure(min_request_interval_ms=interval_ms)
    if seed:
        common.host_throttle.wait(engine)  # first call = 0 wait, seeds last-ts
    barrier = threading.Barrier(n)
    fired = [0.0] * n

    def _worker(i):
        barrier.wait()
        common.host_throttle.wait(engine)
        fired[i] = time.monotonic()

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return sorted(fired)


def test_config_knob_default_and_override():
    """The interval is a config knob (env-overridable) with a conservative
    400ms default — not a hardcoded magic number."""
    assert tofu_search.SearchConfig().min_request_interval_ms == 400
    tofu_search.configure(min_request_interval_ms=250)
    assert tofu_search.get_config().min_request_interval_ms == 250


def test_config_env_override():
    """TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS maps onto the field."""
    prev = os.environ.get('TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS')
    os.environ['TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS'] = '175'
    try:
        tofu_search.configure()
        assert tofu_search.get_config().min_request_interval_ms == 175
    finally:
        if prev is None:
            os.environ.pop('TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS', None)
        else:
            os.environ['TOFU_SEARCH_MIN_REQUEST_INTERVAL_MS'] = prev


def test_same_engine_concurrent_calls_are_spaced():
    """(i) Two concurrent calls to the SAME engine fire >= interval apart."""
    interval = 0.2
    fired = _fire_times('DDG-HTML', 2, interval_ms=200)
    gap = fired[1] - fired[0]
    assert gap >= interval * 0.95, \
        f'same-engine calls not spaced: gap={gap:.3f}s < interval={interval}s'


def test_different_engines_are_not_serialized():
    """(ii) Two concurrent calls to DIFFERENT engines run in parallel — total
    wall time ~= one interval, NOT two (no global lock)."""
    interval = 0.2
    tofu_search.configure(min_request_interval_ms=200)
    # Seed both engines so each SECOND call needs a full interval.
    common.host_throttle.wait('DDG-HTML')
    common.host_throttle.wait('Bing')

    barrier = threading.Barrier(2)

    def _worker(engine):
        barrier.wait()
        common.host_throttle.wait(engine)

    t0 = time.monotonic()
    threads = [threading.Thread(target=_worker, args=(e,))
               for e in ('DDG-HTML', 'Bing')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    # Serialized would be ~2*interval; overlapped is ~1*interval.
    assert elapsed < interval * 1.6, \
        f'different engines were serialized: elapsed={elapsed:.3f}s ~= 2*interval'
    assert elapsed >= interval * 0.8, \
        f'sanity: each engine should still have waited ~interval, got {elapsed:.3f}s'


def test_jitter_is_present_and_never_below_interval():
    """(iii) Repeated forced collisions produce VARYING waits (jitter), and
    every wait is >= the configured interval (upward-only jitter floor)."""
    interval = 0.1
    tofu_search.configure(min_request_interval_ms=100)
    waits = []
    for _ in range(8):
        # Force gap~=0 each time: stamp last-ts to now, then wait.
        common.host_throttle._last['DDG-HTML'] = time.monotonic()
        waits.append(common.host_throttle.wait('DDG-HTML'))
    assert all(w >= interval * 0.95 for w in waits), \
        f'a wait dipped below the interval floor: {waits}'
    assert max(waits) > min(waits), \
        f'no jitter observed — all waits identical: {waits}'


def test_neuter_throttle_disabled_removes_spacing():
    """NEUTER: interval=0 disables the throttle → concurrent same-engine calls
    are NOT spaced. Proves the throttle is what creates the spacing (load-bearing),
    not some incidental serialization."""
    fired = _fire_times('DDG-HTML', 2, interval_ms=0)
    gap = fired[1] - fired[0]
    assert gap < 0.05, \
        f'NEUTER did not bite — spacing persisted with the throttle disabled: gap={gap:.3f}s'


def test_http_search_get_waits_then_gets_and_skips_when_breaker_open():
    """Ordering (req #2): http_search_get calls the throttle for a healthy
    engine (before the GET), but a circuit-OPEN engine returns instantly and
    spends ZERO interval (throttle not consulted)."""
    tofu_search.configure(min_request_interval_ms=200)

    calls = {'wait': [], 'get': 0}
    orig_wait = common.host_throttle.wait
    orig_get = common.search_session.get
    orig_is_open = common.engine_circuit.is_open

    def _rec_wait(name, **kw):
        calls['wait'].append(name)
        return 0.0

    class _Resp:
        ok = True
        status_code = 200
        text = ''

    def _fake_get(url, **kw):
        calls['get'] += 1
        return _Resp()

    common.host_throttle.wait = _rec_wait
    common.search_session.get = _fake_get
    try:
        # Healthy engine: breaker closed → throttle consulted, then GET.
        common.engine_circuit.is_open = lambda name: False
        common.http_search_get(name='DDG-HTML', url='https://x/', params={},
                               query='q', parser=lambda r: [])
        assert calls['wait'] == ['DDG-HTML'], f'throttle not consulted: {calls}'
        assert calls['get'] == 1

        # Circuit OPEN: engine skipped BEFORE the throttle — zero interval spent.
        calls['wait'].clear()
        calls['get'] = 0
        common.engine_circuit.is_open = lambda name: True
        common.http_search_get(name='Bing', url='https://x/', params={},
                               query='q', parser=lambda r: [])
        assert calls['wait'] == [], \
            f'benched engine still consumed the throttle: {calls}'
        assert calls['get'] == 0
    finally:
        common.host_throttle.wait = orig_wait
        common.search_session.get = orig_get
        common.engine_circuit.is_open = orig_is_open


def test_throttle_wait_clamped_to_timeout():
    """The wait never exceeds the per-request timeout budget (so it consumes
    budget the caller already has, never adds an unbounded new wait)."""
    tofu_search.configure(min_request_interval_ms=5000)  # 5s interval
    common.host_throttle.wait('DDG-HTML')  # seed
    common.host_throttle._last['DDG-HTML'] = time.monotonic()  # force full wait
    t0 = time.monotonic()
    waited = common.host_throttle.wait('DDG-HTML', max_wait=0.15)
    elapsed = time.monotonic() - t0
    assert waited <= 0.15 + 1e-3 and elapsed < 0.3, \
        f'wait not clamped to max_wait: waited={waited:.3f}s elapsed={elapsed:.3f}s'
