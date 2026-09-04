"""tests/test_cache_accounting_convention.py — cache-token accounting correctness.

Covers FOUR defects found by auditing real gateway payloads pulled from
``task_events`` (4000 persisted ``round_usage`` rows, 4 models, 2026-07-25):

1. **The ``<=`` convention cliff (BILLING).** ``compute_cost`` decided the
   vendor convention with ``if inp <= cache_write + cache_read``. On the
   OpenAI-compat wire ``prompt_tokens`` ALREADY INCLUDES the cached tokens, so
   a round that reads back its ENTIRE prompt (``cached == prompt_tokens``)
   satisfies ``<=`` and is misread as Anthropic — the cache is then ADDED on
   top of a total that already contained it and the whole prefix is re-priced
   at the full uncached rate. Measured blast radius: prompt=82843 /
   cached=82843 jumps $0.0439 → $0.4581 (**10.4x**). Production has already
   been observed at ``margin=1`` (prompt=428603, cached=428602 on
   aws.claude-opus-4.7) — one token from a 10x over-charge.

2. **The hit% double-count (MONITORING).** ``log_round_cache_stats`` computed
   ``total_input = prompt_tokens + cache_write + cache_read`` unconditionally.
   Under the OpenAI convention that counts the cached tokens TWICE, halving
   every reported hit rate — which is why the live logs cluster on a physically
   impossible ``hit=50%`` (a 99.998% hit reported as 50%).

3. **Cold rounds were invisible (MONITORING).** The same function returned
   early when ``not cache_write and not cache_read`` — so a round that missed
   the cache ENTIRELY, i.e. the single most expensive kind of round, emitted no
   line at all. Two 20+ round Opus-5 sessions burned a full context each with
   zero ``[CacheStats]`` evidence.

4. **Write-gated logic is inert ON THE OPENAI-COMPAT WIRE (SCOPED 2026-07-26).**
   The gateway's OpenAI-compat usage carries ``cache_write_tokens`` but always
   0, so write-gated predicates cannot fire on that line. The tests below pin
   THAT — and nothing broader.

   ★ SCOPE CORRECTION: this was originally written as "pins it to 0 on every
   model (231/231 rounds)" and concluded the branches were dead **in this
   deployment**. That over-generalised a biased sample. The 231/231 came from
   ``round_usage`` events, which are **3052/3052 openai-convention** and never
   sampled aws.claude-opus-4.6/4.8 at all. On the CURRENT full ``task_results``
   (23,980 rows carrying usage) the anthropic-convention line reports
   ``cache_write > 0`` on **10,938 of 10,961** rows, and the real
   ``is_floor_collapse`` predicate fires **1732** times. Those branches are
   LIVE — just not on this wire.

   Reachability on the anthropic wire is pinned separately in
   ``tests/test_cache_write_reachability_by_convention.py``. Do NOT restore the
   "every model" wording, and do NOT infer reachability from the write
   threshold alone — ``is_floor_collapse`` is a CONJUNCTION
   (``cw > 20000 AND cr <= 90000``); counting only the write half overstates
   it (4697 vs the true 1732).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_cache_accounting_convention.py -v
"""

from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cost import (  # noqa: E402
    compute_cost,
    normalize_usage,
    split_input_tokens,
    usage_cache_convention,
)

pytestmark = pytest.mark.unit


# ── Real payload shapes, copied verbatim from persisted task_events ──

def _openai_wire(prompt_tokens: int, cached: int) -> dict:
    """The sankuai AIGC gateway's OpenAI-compat usage shape.

    ``prompt_tokens`` is the TOTAL (cache included); the hit is reported in the
    nested ``prompt_tokens_details.cached_tokens``. ``cache_write_tokens`` is
    present but always 0 — the gateway does not meter cache creation.
    """
    return {
        'prompt_tokens': prompt_tokens,
        'completion_tokens': 100,
        'cache_read_tokens': cached,
        'cache_write_tokens': 0,
        'cached_tokens': cached,
        'effectiveCachedTokens': cached,
        'input_tokens': 0,
        'output_tokens': 0,
        'prompt_tokens_details': {'cached_tokens': cached},
        'total_tokens': prompt_tokens + 100,
    }


def _anthropic_wire(uncached: int, cache_read: int, cache_write: int = 0) -> dict:
    """Anthropic-native usage: ``input_tokens`` is the UNCACHED residual."""
    u = {
        'input_tokens': uncached,
        'output_tokens': 100,
        'cache_read_input_tokens': cache_read,
    }
    if cache_write:
        u['cache_creation_input_tokens'] = cache_write
    return u


_MODEL = 'aws.claude-opus-4.8'


# ── Defect 1: the convention cliff ──────────────────────────────────────

def test_full_cache_hit_is_not_repriced_as_uncached():
    """cached == prompt_tokens must NOT flip the engine to the Anthropic branch.

    This is the exact cliff. Under the old ``inp <= cw + cr`` test the equality
    case took the Anthropic branch and re-added the cache on top of a total
    that already contained it.
    """
    u = _openai_wire(prompt_tokens=82843, cached=82843)
    cc = compute_cost(u, model_id=_MODEL)
    assert cc['totalInputTokens'] == 82843, (
        'total input must equal prompt_tokens on the OpenAI wire — got %s '
        '(the cache was added on top of a total that already contained it)'
        % cc['totalInputTokens'])
    assert cc['inputTokens'] == 0, (
        'a fully-cached round has ZERO uncached input; got %s'
        % cc['inputTokens'])


def test_full_cache_hit_costs_the_same_as_a_one_token_miss():
    """Continuity across the boundary: 1 uncached token must not cost 10x.

    cached=82842 (1 token uncached) and cached=82843 (0 uncached) differ by a
    SINGLE token, so their costs must differ by at most that token's price.
    Pre-fix they differed by 10.4x.
    """
    near = compute_cost(_openai_wire(82843, 82842), model_id=_MODEL)['costUsd']
    exact = compute_cost(_openai_wire(82843, 82843), model_id=_MODEL)['costUsd']
    assert exact <= near, (
        'a FULL cache hit (%s) must never cost more than a near-full hit (%s)'
        % (exact, near))
    assert abs(near - exact) < 0.01, (
        'one uncached token moved the price by $%.4f — the convention '
        'detection flipped at the boundary' % abs(near - exact))


def test_production_margin_of_one_stays_on_the_openai_branch():
    """The closest margin actually observed in production (prompt-cached == 1).

    aws.claude-opus-4.7, prompt=428603 cached=428602. One more cached token
    would have tipped the old predicate.
    """
    u = _openai_wire(prompt_tokens=428603, cached=428602)
    assert usage_cache_convention(u) == 'openai'
    cc = compute_cost(u, model_id='aws.claude-opus-4.7')
    assert cc['totalInputTokens'] == 428603
    assert cc['inputTokens'] == 1


def test_anthropic_native_convention_is_preserved():
    """REGRESSION GUARD: the Anthropic split must be untouched by the fix.

    ``input_tokens`` is the uncached residual, so the total is
    input + cache_read + cache_write.
    """
    u = _anthropic_wire(uncached=2, cache_read=82841, cache_write=1200)
    assert usage_cache_convention(u) == 'anthropic'
    cc = compute_cost(u, model_id=_MODEL)
    assert cc['inputTokens'] == 2
    assert cc['totalInputTokens'] == 2 + 82841 + 1200


def test_anthropic_fully_cached_round_has_zero_input_tokens():
    """Anthropic reports input_tokens=0 on a full hit — still 'anthropic'."""
    u = _anthropic_wire(uncached=0, cache_read=82841)
    assert usage_cache_convention(u) == 'anthropic'
    assert split_input_tokens(u) == (0, 82841)


def test_convention_is_decided_structurally_not_by_magnitude():
    """The detector must key on WHICH KEYS are present, not on their sizes.

    A magnitude comparison is inherently fragile — the whole point of the fix.
    Two payloads with IDENTICAL numbers but different key spellings must be
    classified differently.
    """
    same_numbers_openai = _openai_wire(prompt_tokens=1000, cached=1000)
    same_numbers_anthropic = _anthropic_wire(uncached=1000, cache_read=1000)
    assert usage_cache_convention(same_numbers_openai) == 'openai'
    assert usage_cache_convention(same_numbers_anthropic) == 'anthropic'
    assert split_input_tokens(same_numbers_openai) == (0, 1000)
    assert split_input_tokens(same_numbers_anthropic) == (1000, 2000)


def test_no_cache_activity_passes_input_through_unchanged():
    """A cold round has uncached == total == prompt_tokens, under either wire."""
    assert split_input_tokens({'prompt_tokens': 5000}) == (5000, 5000)
    assert split_input_tokens({'input_tokens': 5000}) == (5000, 5000)
    assert split_input_tokens(None) == (0, 0)


def test_cache_only_payload_without_prompt_field_has_nonzero_total():
    """A provider reporting cache tokens but NO prompt field must not total 0.

    The Kimi/sankuai gateway reports the auto-cache hit in ``cached_tokens``
    while leaving ``prompt_tokens`` / ``input_tokens`` absent (or pinned to 0).
    Pre-fix, ``split_input_tokens`` returned ``(0, 0)`` there, so the frontend
    rendered the contradictory "总输入 0 · 缓存读 39.2k" and ``compute_cost``
    priced the round at a 0-token tier. The total cannot be smaller than the
    cache tokens it read/wrote, so fall back to that sum.
    """
    u = {'completion_tokens': 931, 'cached_tokens': 39200,
         'cache_read_tokens': 0}
    assert usage_cache_convention(u) == 'openai'
    assert split_input_tokens(u) == (0, 39200)
    cc = compute_cost(u, model_id='kimi-k3')
    assert cc['totalInputTokens'] == 39200
    assert cc['inputTokens'] == 0
    assert cc['cacheReadTokens'] == 39200
    assert cc['cacheSavingsCny'] > 0, (
        'a fully-cached round must report cache savings, not ¥0')


# ── Defect 2 + 3: the per-round log line ────────────────────────────────

class _Capture(logging.Handler):
    """Collect formatted records off a specific logger (no caplog dependency)."""

    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        # getMessage() already applies record.args — re-applying % would
        # choke on literal '%' in the formatted output (e.g. "hit=100%").
        self.lines.append(record.getMessage())


@pytest.fixture()
def roi_log():
    import lib.tasks_pkg.cache_tracking._roi as _roi
    cap = _Capture()
    _roi.logger.addHandler(cap)
    prev = _roi.logger.level
    _roi.logger.setLevel(logging.INFO)
    try:
        yield cap
    finally:
        _roi.logger.removeHandler(cap)
        _roi.logger.setLevel(prev)


def test_hit_pct_reports_the_true_read_ratio(roi_log):
    """A 99.998% cache hit must report ~100%, not the double-counted 50%.

    This is the live ``hit=50%`` cluster: 160 log lines pinned at exactly 50%
    is not a coincidence, it is ``cr / (cr + cr)``.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    log_round_cache_stats('convtest1', 0, _openai_wire(82843, 82841),
                          model=_MODEL, tid='t1234567')
    assert roi_log.lines, 'no CacheStats line emitted'
    line = roi_log.lines[-1]
    assert 'hit=100%' in line, 'expected hit=100%%, got: %s' % line


def test_anthropic_hit_pct_is_unchanged_by_the_fix(roi_log):
    """REGRESSION GUARD: the Anthropic split already reported correctly."""
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    # uncached=1000, read=1000 -> total 2000 -> 50% is the TRUE ratio here.
    log_round_cache_stats('convtest2', 0, _anthropic_wire(1000, 1000),
                          model=_MODEL, tid='t1234567')
    assert 'hit=50%' in roi_log.lines[-1], roi_log.lines[-1]


def test_a_total_miss_round_still_emits_a_line(roi_log):
    """The most EXPENSIVE round must not be the only silent one.

    Two Opus-5 sessions (23 and 22 rounds) ran to completion with
    total_read=0 and emitted ZERO per-round lines, because the early return
    fired on exactly the rounds worth seeing.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    log_round_cache_stats('convtest3', 4, _openai_wire(120000, 0),
                          model=_MODEL, tid='t1234567')
    assert roi_log.lines, 'a 120k-token full-miss round emitted NO log line'
    line = roi_log.lines[-1]
    assert 'cache_r=0' in line and 'hit=0%' in line, line
    assert 'COLD' in line, 'a full-miss round should be greppable: %s' % line


def test_trivially_small_rounds_stay_quiet(roi_log):
    """The anti-spam intent of the old gate is preserved for tiny prompts.

    A prompt below the largest documented minimum-cacheable length cannot be
    cached by any Claude tier, so its miss is not actionable signal.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    log_round_cache_stats('convtest4', 0, {'prompt_tokens': 200},
                          model=_MODEL, tid='t1234567')
    assert not roi_log.lines, 'a 200-token round should not be logged'


# ── Defect 1b: the billing adapter must not destroy the convention ──────

def test_billing_scalars_do_not_become_an_overcharge():
    """The wallet debit must equal the displayed price for an OpenAI round.

    ``compute_request_cost`` takes loose SCALARS. It used to re-encode them
    under hardcoded Anthropic key names, which flips their meaning: a gpt-4o
    round of 10000 TOTAL prompt tokens with 6000 cached was billed as 10000
    UNCACHED + 6000 cache on top — 52500µ against a displayed 37500µ, a 40%
    over-charge on every cached OpenAI round.
    """
    from lib.billing.cost import MICRO_PER_USD, compute_request_cost
    u = {'prompt_tokens': 10000, 'completion_tokens': 2000,
         'cache_read_tokens': 6000}
    cc = compute_cost(u, model_id='gpt-4o')
    displayed = round((cc['inputCostUsd'] + cc['outputCostUsd']
                       + cc['cacheWriteCostUsd'] + cc['cacheReadCostUsd'])
                      * MICRO_PER_USD)
    billed = compute_request_cost(
        'gpt-4o', input_tokens=10000, output_tokens=2000,
        cache_read_tokens=6000, margin=0.0)
    assert billed.base_micro == displayed, (
        'wallet debit %s != displayed %s — the billing adapter re-spelled the '
        'usage and changed its convention' % (billed.base_micro, displayed))


def test_synthesize_usage_round_trips_both_conventions():
    """Scalars must be spelled so the engine reads them back unchanged."""
    from lib.cost import synthesize_usage
    # Cache-inclusive total (OpenAI): cache is a subset of input.
    openai_like = synthesize_usage(input_tokens=10000, output_tokens=100,
                                   cache_read_tokens=6000)
    assert usage_cache_convention(openai_like) == 'openai'
    assert split_input_tokens(openai_like) == (4000, 10000)
    # Residual (Anthropic): cache exceeds the input, so input can't be a total.
    anthropic_like = synthesize_usage(input_tokens=500, output_tokens=100,
                                      cache_read_tokens=40000,
                                      cache_write_tokens=8000)
    assert usage_cache_convention(anthropic_like) == 'anthropic'
    assert split_input_tokens(anthropic_like) == (500, 48500)


def test_hybrid_payload_with_impossible_cache_reads_as_residual():
    """cache > prompt_tokens is arithmetically impossible under OpenAI rules.

    Some providers emit ``prompt_tokens`` carrying residual semantics. Since
    the cache cannot exceed a total that contains it, such a payload must be
    read as Anthropic — otherwise the hit rate exceeds 100% (observed live as
    ``hit=15000%``).
    """
    u = {'prompt_tokens': 100, 'cache_write_tokens': 5000,
         'cache_read_tokens': 15000}
    assert usage_cache_convention(u) == 'anthropic'
    uncached, total = split_input_tokens(u)
    assert (uncached, total) == (100, 20100)
    assert round(15000 / total * 100) <= 100, 'hit rate exceeded 100%'


# ── Defect 5: the HYBRID wire, and the two surfaces drifting on it ──────
#
# ★ FIXTURE BLINDSPOT THIS SECTION EXISTS TO CLOSE (read before editing):
#   Neither `_openai_wire()` nor `_anthropic_wire()` above can express the
#   shape that actually broke. `_anthropic_wire()` deliberately omits
#   `prompt_tokens` entirely, so every Anthropic-side assertion in this file
#   runs against a PURE payload. The sankuai_anthropic gateway is not pure: it
#   emits `prompt_tokens` (cache-INCLUSIVE total) *beside* the Anthropic
#   `cache_*_input_tokens` residual keys. That real shape had therefore never
#   entered the scan surface of ANY guard, which is why a 72% fleet-wide
#   overstatement stayed green.
#
#   `test_hybrid_payload_with_impossible_cache_reads_as_residual` above covers
#   only the INVERSE arithmetic case (cache > prompt_tokens). The live defect
#   is cache < prompt_tokens, which that test cannot reach.
#
#   Do NOT "simplify" `_hybrid_gateway_wire()` by dropping `prompt_tokens` —
#   that is precisely the omission that made this class of payload invisible.

def _hybrid_gateway_wire(uncached: int, cache_read: int, cache_write: int) -> dict:
    """The REAL sankuai_anthropic shape: BOTH conventions spelled at once.

    Verbatim key set from conversation ms5i5ydigs9j9w. The defining property,
    verified on 28/28 rounds of that turn, is the identity

        input_tokens + cache_read + cache_write == prompt_tokens

    i.e. `prompt_tokens` is the cache-INCLUSIVE total while `input_tokens`
    carries the Anthropic uncached residual. Both are present and they mean
    DIFFERENT things — the XOR assumption documented in `normalize_usage` does
    not hold here, so the alias order stops being "immaterial".
    """
    return {
        'input_tokens': uncached,
        'output_tokens': 39580,
        'completion_tokens': 39645,
        'prompt_tokens': uncached + cache_read + cache_write,
        'total_tokens': uncached + cache_read + cache_write + 39645,
        'cached_tokens': 0,
        'cache_read_tokens': 0,
        'cache_write_tokens': 0,
        'cache_read_input_tokens': cache_read,
        'cache_creation_input_tokens': cache_write,
    }


# The exact turn the owner flagged: 5.5M "tokens" shown as ¥351.60.
_LIVE_UNCACHED = 162854
_LIVE_READ = 2420002
_LIVE_WRITE = 2967806


def test_hybrid_wire_fixture_really_is_hybrid():
    """Guard-the-guard: the fixture must carry BOTH spellings and the identity.

    Without this, someone "tidying" the fixture into a pure Anthropic dict
    would leave every assertion below passing while testing nothing relevant.
    """
    u = _hybrid_gateway_wire(_LIVE_UNCACHED, _LIVE_READ, _LIVE_WRITE)
    assert u.get('prompt_tokens'), 'fixture lost the OpenAI total spelling'
    assert u.get('input_tokens'), 'fixture lost the Anthropic residual spelling'
    assert u['input_tokens'] + u['cache_read_input_tokens'] \
        + u['cache_creation_input_tokens'] == u['prompt_tokens'], (
        'fixture violates the measured identity — prompt_tokens must be the '
        'cache-INCLUSIVE total')
    assert usage_cache_convention(u) == 'anthropic'


def test_hybrid_residual_comes_from_the_anthropic_key_not_the_total():
    """THE DEFECT: the residual must be `input_tokens`, never `prompt_tokens`.

    Pre-fix, `normalize_usage`'s alias order resolved `input` to the 5,562,791
    TOTAL while the structural detector said 'anthropic', so the whole prefix
    was re-priced at the uncached rate (¥201.37 instead of ¥5.90) and
    `totalInputTokens` double-counted the cache (10,950,599 vs 5,550,662).
    """
    u = _hybrid_gateway_wire(_LIVE_UNCACHED, _LIVE_READ, _LIVE_WRITE)
    uncached, total = split_input_tokens(u)
    assert uncached == _LIVE_UNCACHED, (
        'uncached input must be the Anthropic residual %d, got %d — the '
        'cache-inclusive total was billed at the uncached rate'
        % (_LIVE_UNCACHED, uncached))
    assert total == _LIVE_UNCACHED + _LIVE_READ + _LIVE_WRITE
    # The cache must not be counted twice into the total.
    assert total == u['prompt_tokens'], (
        'total input must equal the gateway-reported prompt_tokens; got %d '
        'vs %d — the cache was added on top of a total containing it'
        % (total, u['prompt_tokens']))


def test_the_two_cost_surfaces_agree_on_the_hybrid_wire():
    """PARITY: display and wallet must price the SAME turn identically.

    Both `lib/cost.py` and `lib/billing/cost.py` claim in their docstrings that
    the two surfaces "can NEVER drift" because they share one engine. That was
    FALSE on this payload: display resolved 5,562,791 uncached tokens while the
    billing adapter's `synthesize_usage` re-inference resolved ~174,983 —
    $48.56 displayed against $21.62 debited, a 2.246x divergence.

    This drives BOTH real paths from ONE raw dict. A guard that exercised only
    one side would stay green while they diverge — which is exactly how this
    shipped.
    """
    from lib.billing.cost import MICRO_PER_USD, compute_request_cost
    from lib.cost import normalize_usage as _nu_fn

    u = _hybrid_gateway_wire(_LIVE_UNCACHED, _LIVE_READ, _LIVE_WRITE)
    model = 'claude-opus-5'

    cc = compute_cost(u, model_id=model, provider_id='sankuai_anthropic')
    displayed = round((cc['inputCostUsd'] + cc['outputCostUsd']
                       + cc['cacheWriteCostUsd'] + cc['cacheReadCostUsd'])
                      * MICRO_PER_USD)

    # Mirror the production call site (lib/billing/request_flow.py): resolve
    # the split at the seam that owns the convention, then hand over scalars.
    _n = _nu_fn(u)
    _uncached, _ = split_input_tokens(u)
    billed = compute_request_cost(
        model, input_tokens=_uncached, output_tokens=_n['output'],
        cache_read_tokens=_n['cache_read'],
        cache_write_tokens=_n['cache_write'],
        provider_id='sankuai_anthropic', margin=0.0)

    assert billed.base_micro == displayed, (
        'wallet %s != displayed %s (%.3fx) — the two cost surfaces drifted on '
        'a real gateway payload'
        % (billed.base_micro, displayed,
           displayed / billed.base_micro if billed.base_micro else 0))

    # ...and BOTH must equal the truth derived from the gateway's own residual.
    truth = compute_cost(
        {'input_tokens': _LIVE_UNCACHED, 'output_tokens': 39580,
         'completion_tokens': 39645,
         'cache_read_input_tokens': _LIVE_READ,
         'cache_creation_input_tokens': _LIVE_WRITE},
        model_id=model, provider_id='sankuai_anthropic')
    assert abs(cc['costUsd'] - truth['costUsd']) < 0.01, (
        'display $%.4f != pure-Anthropic truth $%.4f — agreement alone is not '
        'enough, both surfaces could be wrong together'
        % (cc['costUsd'], truth['costUsd']))


def test_hybrid_round_is_not_overcharged_against_its_own_residual():
    """Concrete floor: the flagged turn must price at ~¥156, not ¥351.60.

    A pure parity assertion can be satisfied by BOTH surfaces being wrong in
    the same direction, so pin the absolute number the gateway's own residual
    implies.
    """
    u = _hybrid_gateway_wire(_LIVE_UNCACHED, _LIVE_READ, _LIVE_WRITE)
    cc = compute_cost(u, model_id='claude-opus-5',
                      provider_id='sankuai_anthropic')
    assert cc['inputTokens'] == _LIVE_UNCACHED
    assert cc['costCny'] < 200.0, (
        'the flagged turn still prices at ¥%.2f — it was ¥351.60 pre-fix and '
        'its true cost is ~¥156' % cc['costCny'])


def test_the_production_billing_call_site_resolves_the_split_at_the_seam():
    """WIRING: `request_flow` must hand `compute_request_cost` rich usage.

    ★ Added because a NEUTER exposed this hole: reverting the real call site to
    `_nu['input']` left `tests/test_billing.py` fully GREEN (22 passed). Every
    existing billing guard drives `compute_request_cost` with hand-written
    scalars, so NOTHING exercised the one line that decides which number the
    wallet actually receives in production — the "tested the helper, not the
    call site" failure this project has hit repeatedly.

    Asserted on the AST, not by substring: a comment mentioning
    `raw_usage` must not be able to satisfy this.
    """
    import ast
    import pathlib

    src = pathlib.Path(
        __file__).resolve().parent.parent / 'lib' / 'billing' / 'request_flow.py'
    tree = ast.parse(src.read_text())

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name)
             and n.func.id == 'compute_request_cost']
    assert calls, 'no compute_request_cost call found in request_flow.py'

    for call in calls:
        kw = {k.arg: k.value for k in call.keywords if k.arg}
        assert 'raw_usage' in kw, 'call site stopped passing the full usage shape'
        assert 'input_tokens' not in kw, (
            'request_flow collapsed rich usage back to a scalar before the '
            'shared cost engine could resolve cache convention and prompt tier')
        node = kw['raw_usage']
        assert isinstance(node, ast.Name) and node.id == 'u', (
            'billing must forward the unmodified task usage object')


# ── Defect 5b: the TELEMETRY axis of the very same double-count ─────────
#
# ★ WHY THIS SECTION EXISTS (owner-found coverage gap, 2026-07-29):
#   `split_input_tokens` is not only the cost engine's helper — it is also the
#   `total_input` source for BOTH cache-telemetry sites:
#       lib/tasks_pkg/cache_tracking/_roi.py:251     (per-round hit=N% line)
#       lib/tasks_pkg/cache_tracking/_detect.py:1005 (session total_input_tokens)
#   So the hybrid-payload defect fixed above was ALSO halving every reported
#   cache hit rate on this gateway. Measured on the live fleet BEFORE the fix:
#       ms5i5ydigs9j9w session : 22.1% reported vs 43.6% true
#       fleet, 3,181 hybrid rounds: 35.5% reported vs 70.4% true
#   Per-round it is starker: a round reading 121,817 of 123,615 tokens back
#   from cache reported `hit=49%` when the truth is `hit=98%`.
#
#   The defect-2 guards above (`test_hit_pct_reports_the_true_read_ratio` and
#   siblings) drive `log_round_cache_stats` with `_openai_wire` / `_anthropic_wire`
#   ONLY — neither can express the hybrid shape, so the telemetry axis had ZERO
#   coverage for it. Cost had a guard, telemetry did not: change the meaning of
#   `total_input` and the cost guards would go red while these stayed green.
#
#   This mirrors the file's own defect-2 story (the "physically impossible
#   hit=50% cluster"): the same double-count survived by moving to another
#   carrier. Do NOT delete these in favour of the cost guards — they pin a
#   DIFFERENT consumer of the same helper.

# Real R3 of ms5i5ydigs9j9w: read 121,817 of a 123,615-token prompt.
# Pre-fix this logged hit=49%; the truth is 98%.
_R3_UNCACHED = 2
_R3_READ = 121817
_R3_WRITE = 1796


def test_hybrid_wire_hit_pct_is_not_halved(roi_log):
    """A ~99% cache hit on the HYBRID wire must not be reported as ~49%.

    The denominator must be the prompt the provider actually processed
    (== prompt_tokens), NOT `prompt_tokens + cache_read + cache_write`, which
    counts the cached tokens a second time on this wire.

    121,817 / 123,615 = 98.5% -> rounds to 99. Pre-fix the same round logged
    49%. The assertion is a BAND rather than the exact integer so a rounding
    tweak cannot make it lie, while the halved value (49) is still excluded by
    a wide margin.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    u = _hybrid_gateway_wire(_R3_UNCACHED, _R3_READ, _R3_WRITE)
    log_round_cache_stats('convhybrid1', 2, u, model='claude-opus-5', tid='t1234567')
    assert roi_log.lines, 'no CacheStats line emitted for a hybrid-wire round'
    line = roi_log.lines[-1]
    pct = int(line.split('hit=')[1].split('%')[0])
    assert pct >= 95, (
        'expected ~99%% on a 121817/123615 round, got %d%% — a value near 49%% '
        'means the cached tokens were counted twice: %s' % (pct, line))
    # ...and the logged prompt size must be the gateway's own total.
    assert 'input=%d' % u['prompt_tokens'] in line, (
        'logged input size must equal prompt_tokens (%d); got: %s'
        % (u['prompt_tokens'], line))


def test_hybrid_wire_hit_pct_can_never_exceed_100(roi_log):
    """Complement: a FULL hit must read 100%, not something impossible.

    Guards the opposite failure direction — an under-counted denominator
    (e.g. using only the uncached residual) would print hit>100%, which is the
    `hit=15000%` shape this file already records for the inverse hybrid.
    """
    from lib.tasks_pkg.cache_tracking._roi import log_round_cache_stats
    u = _hybrid_gateway_wire(0, 200000, 0)
    log_round_cache_stats('convhybrid2', 0, u, model='claude-opus-5', tid='t1234567')
    line = roi_log.lines[-1]
    pct = int(line.split('hit=')[1].split('%')[0])
    assert pct == 100, 'a fully-cached hybrid round must read hit=100%%; got %d%%' % pct


def test_session_total_input_is_not_double_counted_on_the_hybrid_wire():
    """`_detect.py`'s session accumulator must use the same corrected total.

    The session-level `overall_hit_pct` divides by `state.total_input_tokens`.
    Accumulating `prompt_tokens + cache_read + cache_write` there inflates the
    denominator by exactly the cached tokens and halves the session rate — the
    fleet-wide 35.5%-vs-70.4% gap measured before the fix.
    """
    from lib.cost import split_input_tokens as sit
    rounds = [
        _hybrid_gateway_wire(_R3_UNCACHED, _R3_READ, _R3_WRITE),
        _hybrid_gateway_wire(2, 123613, 3245),
        _hybrid_gateway_wire(162854, 2420002, 2967806),
    ]
    accumulated = sum(sit(u)[1] for u in rounds)
    gateway_truth = sum(u['prompt_tokens'] for u in rounds)
    assert accumulated == gateway_truth, (
        'session total_input %d != the gateway-reported total %d — the cache '
        'was counted twice into the hit-rate denominator'
        % (accumulated, gateway_truth))
    # The discriminating check is RELATIVE, not an absolute threshold: the
    # session's true rate depends on the rounds chosen, but the double-counted
    # denominator always inflates by exactly (cache_read + cache_write), so the
    # reported rate is always materially lower. An absolute floor here would be
    # a magic number that says nothing about the defect.
    reads = sum(u['cache_read_input_tokens'] for u in rounds)
    writes = sum(u['cache_creation_input_tokens'] for u in rounds)
    hit_true = reads / accumulated * 100
    hit_double = reads / (accumulated + reads + writes) * 100
    assert hit_true > hit_double * 1.5, (
        'corrected session hit %.1f%% is not materially above the '
        'double-counted %.1f%% — the inflated denominator is back'
        % (hit_true, hit_double))


def test_both_telemetry_sites_resolve_the_prompt_size_through_the_shared_helper():
    """WIRING: neither telemetry site may re-derive the total on its own.

    Asserted on the AST so a comment mentioning `split_input_tokens` cannot
    satisfy it. If a site ever goes back to computing
    `prompt_tokens + cache_read + cache_write` inline, the cost guards stay
    green while every logged hit rate silently halves again — which is exactly
    how this defect survived its first fix.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for rel in ('lib/tasks_pkg/cache_tracking/_roi.py',
                'lib/tasks_pkg/cache_tracking/_detect.py'):
        tree = ast.parse((root / rel).read_text())
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert 'split_input_tokens' in called or '_split_input_tokens' in called, (
            '%s no longer calls split_input_tokens — it is deriving the '
            'prompt size itself, so the telemetry and the price can disagree '
            'about how big the prompt was' % rel)


# ── Complements: the two PURE wires must be byte-identical to before ────

def test_pure_openai_wire_is_untouched_by_the_hybrid_fix():
    """COMPLEMENT: 'always read input_tokens' would break the OpenAI line.

    `_openai_wire()` carries `input_tokens: 0` as a vestigial field while the
    real total lives in `prompt_tokens`. A naive fix that unconditionally
    preferred `input_tokens` would resolve the uncached input to 0 and
    under-charge every OpenAI round. Only the 'anthropic' branch may consult
    the native key.
    """
    u = _openai_wire(prompt_tokens=82843, cached=60000)
    assert usage_cache_convention(u) == 'openai'
    assert split_input_tokens(u) == (82843 - 60000, 82843)
    cold = _openai_wire(prompt_tokens=5000, cached=0)
    assert split_input_tokens(cold) == (5000, 5000)


def test_pure_anthropic_wire_is_untouched_by_the_hybrid_fix():
    """COMPLEMENT: payloads WITHOUT prompt_tokens must behave exactly as before."""
    u = _anthropic_wire(uncached=2, cache_read=82841, cache_write=1200)
    assert split_input_tokens(u) == (2, 2 + 82841 + 1200)
    full_hit = _anthropic_wire(uncached=0, cache_read=82841)
    assert split_input_tokens(full_hit) == (0, 82841)


def test_prompt_tokens_residual_shape_still_falls_back():
    """COMPLEMENT: the inverse hybrid (cache > prompt_tokens) keeps working.

    That shape has NO `input_tokens` key at all, so the residual must fall back
    to the normalized value rather than collapsing to 0.
    """
    u = {'prompt_tokens': 100, 'cache_write_tokens': 5000,
         'cache_read_tokens': 15000}
    assert usage_cache_convention(u) == 'anthropic'
    assert split_input_tokens(u) == (100, 20100)


# ── Defect 4: write-gated logic is inert ON THE OPENAI-COMPAT WIRE ─────

def test_gateway_never_reports_cache_write_so_floor_collapse_cannot_fire():
    """PIN: ``is_floor_collapse`` cannot fire ON THE OPENAI-COMPAT WIRE.

    It requires ``cache_write > 20000``, and the gateway's openai-compat usage
    always reports ``cache_write_tokens = 0``. So on THIS wire the branch is
    unreachable and tuning ``_FLOOR_WRITE_LO`` against it would be fitting
    noise.

    ★ NOT a statement about the deployment as a whole. The anthropic-convention
    line DOES meter writes (10,938 of 10,961 rows) and the predicate fires 1732
    times in production — see
    ``tests/test_cache_write_reachability_by_convention.py``. The earlier
    "every model / 231-of-231" claim came from ``round_usage`` events that are
    100% openai-convention and never sampled 4.6/4.8.
    """
    from lib.tasks_pkg import floor_retry as fr
    # A round that WOULD be a textbook floor collapse if writes were metered.
    u = _openai_wire(prompt_tokens=300000, cached=74000)
    assert normalize_usage(u)['cache_write'] == 0
    assert fr.is_floor_collapse(u) is False


def test_no_reuse_classifiers_cannot_fire_without_metered_writes():
    """PIN: ``_classify_break``'s no_reuse / partial_no_reuse stay quiet when
    the wire does not meter writes (i.e. on the openai-compat line).

    Both require ``cache_write >= _MIN_NO_REUSE_TOKENS``.

    ★ They are NOT dead code deployment-wide — on the anthropic-convention wire
    a metered write makes ``no_reuse`` fire correctly. That direction is pinned
    in ``tests/test_cache_write_reachability_by_convention.py``.
    """
    from lib.tasks_pkg.cache_tracking._detect import _classify_break
    api_break, no_reuse, partial = _classify_break(
        call_count=5, was_compaction=False,
        prev_cache_read=200000, cache_read=0,
        prev_cache_write=0, cache_write=0,
        prev_prefix_tokens=200000,
    )
    assert no_reuse is False, 'no_reuse fired without a metered write'
    assert partial is False, 'partial_no_reuse fired without a metered write'
    # api_break MUST still fire — it is read-driven and remains our only
    # working break signal on this gateway.
    assert api_break is True, (
        'api_break is the ONLY live break detector on a write-blind gateway; '
        'if this goes False we have no cache diagnostics left')
