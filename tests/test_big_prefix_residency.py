"""tests/test_big_prefix_residency.py — residency-aware big-prefix admission.

WHY (the gap this closes)
=========================
The original ``big_prefix_slot`` gate keyed admission on *stream concurrency*:
a per-key ``BoundedSemaphore`` held only for the in-flight ``stream_chat``
duration. But Anthropic prompt-cache eviction is a *cache-RESIDENCY*
phenomenon: a cached prefix stays in the key's pool for the cache TTL (~5m/1h),
long after the stream returns. Two big prefixes whose STREAMS never overlap
(they run back-to-back) still coexist in the pool for minutes and LRU-evict
each other — a competition the stream-only semaphore is blind to.

Live evidence (2026-07-16): 94.8% of opus floor-misses (695 total) had a prefix
≥150k (the gated size), yet the stream gate fired only 6× and hit capacity
(DEGRADED) once. Floor-misses were time-ISOLATED (median 1/minute), i.e. NOT
concurrent streams — so the gate structurally could not see them.

Residency-aware admission counts the DISTINCT big prefixes RESIDENT on a key
within a residency-TTL window and serializes a new distinct prefix when that
working set is full — catching the flow-non-overlap-but-residency-overlap case.

HONEST BOUNDARY (asserted in test_residency_cannot_beat_single_pool_overload):
with a SINGLE key pool, admission can only serialize the active working set;
it cannot spread load. Sustained >capacity distinct-big-conv load still evicts
(needs route (i) dual-key capacity). This suite proves residency admission
holds/steers where the stream gate did NOT, not that it eliminates all misses.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q tests/test_big_prefix_residency.py
"""

from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_residency(monkeypatch):
    """Fresh residency state + deterministic knobs per test."""
    import lib.llm_dispatch.big_prefix_gate as g
    # Small, fast knobs so tests don't sleep for seconds.
    monkeypatch.setenv('TOFU_BIG_PREFIX_GATE', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY', '1')
    monkeypatch.setenv('TOFU_BIG_PREFIX_THRESHOLD_TOKENS', '150000')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_MAX', '2')
    # Budget < TTL so a full-working-set waiter exhausts the budget and
    # degrades (residents don't expire mid-wait); the explicit-expiry test
    # sleeps past the TTL to observe a slot freeing.
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_TTL_MS', '400')  # 0.4s
    # The residency wait budget (short) is what a saturated waiter uses — set it
    # small for fast tests. The STREAM budget stays LARGE on purpose so a test
    # can prove the residency waiter does NOT accidentally use it.
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS', '200')  # 0.2s
    monkeypatch.setenv('TOFU_BIG_PREFIX_WAIT_MS', '5000')          # 5s stream budget
    g._reset_residency_for_tests()
    yield
    g._reset_residency_for_tests()


_BIG = 200_000   # above threshold
_SMALL = 1_000   # below threshold


# ── 1. warm reuse of an already-resident prefix is never blocked ──

def test_same_conv_reuse_is_free_even_at_capacity():
    """A conv whose big prefix is already resident re-admits instantly — it is
    warm, re-using it is exactly what we want, NOT a new competitor."""
    import lib.llm_dispatch.big_prefix_gate as g

    # Fill capacity (2) with two DISTINCT convs, keep them resident.
    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            # A re-runs while A+B both resident → must NOT block (warm reuse).
            t0 = time.time()
            with g.big_prefix_slot('k0', _BIG, conv_id='A'):
                pass
            assert time.time() - t0 < 0.2, (
                'a warm same-conv re-admit must be immediate, not gated as a '
                'new distinct competitor')


# ── 2. a THIRD distinct big prefix waits when the working set is full ──

def test_third_distinct_conv_waits_then_degrades():
    """With RESIDENCY_MAX=2, a 3rd DISTINCT big conv waits up to the budget
    then proceeds degraded — the working set genuinely exceeds the pool."""
    import lib.llm_dispatch.big_prefix_gate as g

    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            t0 = time.time()
            with g.big_prefix_slot('k0', _BIG, conv_id='C'):
                waited = time.time() - t0
            # C found A+B resident (cap 2) → waited ~ the budget (0.2s) since
            # neither A nor B expired within it, then degraded through.
            assert waited >= 0.15, (
                f'3rd distinct big conv should wait ~budget, waited {waited:.2f}s')


# ── 3. THE core gap: flows that DON'T overlap but RESIDENCY does ──

def test_residency_overlap_without_stream_overlap_is_gated():
    """The exact case the stream-only semaphore missed: conv A's stream has
    ALREADY finished (context exited) but its prefix is still RESIDENT; two
    more distinct convs then arrive. The 3rd must still be gated because A+B
    are resident even though NO streams overlap in time."""
    import lib.llm_dispatch.big_prefix_gate as g

    # A streams and finishes — but stays resident (TTL 0.3s not yet elapsed).
    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        pass
    # B streams and finishes — also resident now.
    with g.big_prefix_slot('k0', _BIG, conv_id='B'):
        pass
    # No stream is in flight, yet A+B occupy the residency working set (cap 2).
    # C is a NEW distinct big prefix → must be gated (wait then degrade).
    t0 = time.time()
    with g.big_prefix_slot('k0', _BIG, conv_id='C'):
        waited = time.time() - t0
    assert waited >= 0.15, (
        'residency admission must gate a new distinct prefix when the '
        f'working set is resident even with NO concurrent streams; waited {waited:.2f}s')


# ── 4. NEUTER: without residency awareness (stream-only), case 3 does NOT gate ──

def test_NEUTER_stream_only_does_not_gate_residency_overlap(monkeypatch):
    """Turn residency OFF → the gate reverts to stream-only concurrency. The
    same non-overlapping-streams case (3) then passes through instantly,
    reproducing the blind spot and proving residency awareness is load-bearing.
    """
    import lib.llm_dispatch.big_prefix_gate as g
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY', '0')  # NEUTER
    g._reset_residency_for_tests()

    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        pass
    with g.big_prefix_slot('k0', _BIG, conv_id='B'):
        pass
    t0 = time.time()
    with g.big_prefix_slot('k0', _BIG, conv_id='C'):
        waited = time.time() - t0
    # Stream-only: A and B streams already exited → semaphore free → C instant.
    assert waited < 0.15, (
        'NEUTER expectation: with residency OFF the stream-only gate does NOT '
        f'see the residency overlap → C passes instantly (waited {waited:.2f}s)')


# ── 5. small requests never gate (below threshold) ──

def test_small_requests_never_gate():
    import lib.llm_dispatch.big_prefix_gate as g
    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            t0 = time.time()
            # A small request is never a cache competitor.
            with g.big_prefix_slot('k0', _SMALL, conv_id='C'):
                pass
            assert time.time() - t0 < 0.1


# ── 6. a resident prefix that EXPIRES frees its slot ──

def test_expired_resident_frees_slot():
    """After a resident's TTL elapses (its conv went idle), its slot frees so a
    new distinct prefix admits without waiting the full budget."""
    import lib.llm_dispatch.big_prefix_gate as g

    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        pass
    with g.big_prefix_slot('k0', _BIG, conv_id='B'):
        pass
    # Let A + B residency (0.3s TTL) fully lapse.
    time.sleep(0.4)
    t0 = time.time()
    with g.big_prefix_slot('k0', _BIG, conv_id='C'):
        waited = time.time() - t0
    assert waited < 0.15, (
        f'expired residents must free their slots; C waited {waited:.2f}s')


# ── 7. honest boundary: single pool cannot beat sustained overload ──

def test_residency_cannot_beat_single_pool_overload():
    """DOCUMENTED LIMIT. With cap=2 and 3 convs ALL actively resident, the 3rd
    is forced to degrade (proceed) — admission cannot conjure pool capacity, it
    can only serialize. This is why route (i) dual-key remains the capacity
    fix; residency admission only bounds the ACTIVE working set."""
    import lib.llm_dispatch.big_prefix_gate as g

    order = []
    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            # C cannot fit; it degrades through after the budget (does not
            # deadlock, does not error) — serialization, not capacity.
            with g.big_prefix_slot('k0', _BIG, conv_id='C'):
                order.append('C-admitted-degraded')
    assert order == ['C-admitted-degraded'], (
        'the gate must never deadlock a task — over a full working set it '
        'degrades through, it does not block forever')


# ── 8. REGRESSION: saturated waiter uses the SHORT residency budget, not 45s ──

def test_saturated_waiter_degrades_in_short_budget_not_stream_budget():
    """The 45s-stall regression guard. STREAM budget is 5s here, residency
    budget 0.2s. A 3rd distinct conv on a saturated (non-expiring) set must
    degrade in ~the SHORT residency budget, NOT the stream budget — because
    waiting cannot manufacture single-pool capacity, so a long stall is pure
    loss (a fast miss beats a 45s-then-same-miss)."""
    import lib.llm_dispatch.big_prefix_gate as g
    # Confirm the two budgets are genuinely different so the test is meaningful.
    assert g.residency_wait_budget_ms() < g.wait_budget_ms(), (
        'precondition: residency budget must be shorter than the stream budget')

    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            t0 = time.time()
            with g.big_prefix_slot('k0', _BIG, conv_id='C'):
                waited = time.time() - t0
    # Bounded to the short residency budget (0.2s) with generous slack, and
    # FAR below the 5s stream budget.
    assert waited < 1.0, (
        f'saturated 3rd conv must degrade in ~the short residency budget, '
        f'waited {waited:.2f}s (stream budget is {g.wait_budget_ms()/1000:.0f}s '
        f'— a regression would approach that)')


def test_NEUTER_residency_using_stream_budget_stalls_long(monkeypatch):
    """NEUTER: point the residency waiter back at the LONG stream budget (the
    pre-fix behaviour) and the saturated 3rd conv stalls ~that long — proving
    the SHORT residency budget is load-bearing. We keep the stream budget
    modest (1.0s) so the test stays fast while still an order of magnitude
    above the 0.2s short budget."""
    import lib.llm_dispatch.big_prefix_gate as g
    # NEUTER: make the residency budget equal the (now 1.0s) stream budget —
    # i.e. revert the separation the fix introduced.
    monkeypatch.setenv('TOFU_BIG_PREFIX_WAIT_MS', '1000')
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_WAIT_MS', '1000')
    g._reset_residency_for_tests()

    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            t0 = time.time()
            with g.big_prefix_slot('k0', _BIG, conv_id='C'):
                waited = time.time() - t0
    # With the budgets re-coupled, the saturated waiter burns ~the full 1.0s —
    # demonstrating the regression the short budget prevents.
    assert waited >= 0.8, (
        f'NEUTER expectation: re-coupling residency to the stream budget makes '
        f'the saturated 3rd conv stall ~the full budget (waited {waited:.2f}s)')


# ── 10. REGRESSION: degraded pass-throughs must NOT inflate the table past cap ──

def _resident_count(key='k0'):
    import lib.llm_dispatch.big_prefix_gate as g
    return len(g._residency.get(key, {}))


def test_degraded_passthroughs_keep_table_bounded_by_cap():
    """The unbounded-growth guard. Drive 5 DISTINCT big convs through a cap=2
    pool under saturation: C/D/E each degrade through (working set full). The
    resident table must NEVER exceed cap — a degraded pass-through models a
    write into the finite LRU pool (evicts the LRU resident), it does not
    append a fresh 5-min entry that poisons the next admission."""
    import lib.llm_dispatch.big_prefix_gate as g
    # Long TTL so residents do NOT expire during the sequence — this models the
    # production case (TTL 5min >> round time) where growth actually accrues;
    # a short TTL would let pruning mask it.
    import pytest as _pt
    with _pt.MonkeyPatch.context() as _mp:
        _mp.setenv('TOFU_BIG_PREFIX_RESIDENCY_TTL_MS', '60000')
        g._reset_residency_for_tests()
        _run_bounded_growth(g)


def _run_bounded_growth(g):
    # A and B fill the working set (cap=2); keep them resident (no exit) while
    # C, D, E arrive and degrade through.
    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            assert _resident_count() == 2
            for cid in ('C', 'D', 'E'):
                with g.big_prefix_slot('k0', _BIG, conv_id=cid):
                    # At no point may the table exceed cap.
                    assert _resident_count() <= 2, (
                        f'residency table grew past cap while {cid} was '
                        f'degrading through: {_resident_count()} entries')
            # After all three degraded through, still bounded.
            assert _resident_count() <= 2, (
                f'residency table left over-full after degraded pass-throughs: '
                f'{_resident_count()} entries (cap=2)')


def test_NEUTER_without_lru_bound_table_grows_past_cap(monkeypatch):
    """NEUTER: disable the LRU bound → the pre-fix unbounded-growth bug returns.
    The same 5-distinct-conv saturation inflates the table well past cap,
    proving the LRU bound is load-bearing."""
    import lib.llm_dispatch.big_prefix_gate as g
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_LRU_BOUND', '0')  # NEUTER
    # Long TTL (see bounded-growth test) so pruning can't mask the growth.
    monkeypatch.setenv('TOFU_BIG_PREFIX_RESIDENCY_TTL_MS', '60000')
    g._reset_residency_for_tests()

    with g.big_prefix_slot('k0', _BIG, conv_id='A'):
        with g.big_prefix_slot('k0', _BIG, conv_id='B'):
            for cid in ('C', 'D', 'E'):
                with g.big_prefix_slot('k0', _BIG, conv_id=cid):
                    pass
            # Without the bound every degraded pass-through appended an entry:
            # A, B (held) + C, D, E (degraded) = 5 > cap 2.
            assert _resident_count() > 2, (
                f'NEUTER expectation: without the LRU bound the table grows '
                f'past cap (got {_resident_count()} entries)')


# ── 11. concurrency safety: parallel distinct convs don't corrupt the map ──

def test_concurrent_admissions_thread_safe():
    import lib.llm_dispatch.big_prefix_gate as g
    errors = []

    def _run(cid):
        try:
            with g.big_prefix_slot('k0', _BIG, conv_id=cid):
                time.sleep(0.02)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=_run, args=(f'conv{i}',))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not errors, f'residency admission raised under concurrency: {errors}'
