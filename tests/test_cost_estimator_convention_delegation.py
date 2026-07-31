"""The budget gate's token split MUST be the canonical convention chokepoint.

WHY THIS EXISTS
===============
``lib/cost_estimator._split_tokens`` carried a THIRD, independent copy of the
cache-token convention decision — the magnitude heuristic
``if cw + cr > 0 and inp_raw > cw + cr``. ``lib/cost.usage_cache_convention``'s
own docstring names that shape a "latent 10x BILLING BUG", and pt_28375442
already removed it from the display path (``lib/cost.py``) and the wallet path
(``billing/request_flow``). This third copy survived.

WHY IT MATTERS MORE HERE THAN ON THE OTHER TWO PATHS
----------------------------------------------------
Consumers: ``estimate_usage_cost`` → ``check_budget`` →
``lib/tasks_pkg/orchestrator/_run.py`` per-round budget gate. The error
direction is OVER-estimation, so **a cost that never happened can abort a task
that is doing real work**. A wrong number on a dashboard is a reporting bug; a
wrong number here kills the run.

THE MEASURED CLIFF (case ① of the ticket, reproduced before the fix)
-------------------------------------------------------------------
A full cache hit on the OpenAI-compat wire — ``prompt_tokens == cached_tokens
== 82843`` — has ZERO uncached input: the whole prompt was served from cache.
The heuristic needs ``inp_raw > cw + cr`` to recognise the OpenAI convention,
and at exact equality that is False, so it fell through to
``uncached = inp_raw`` and billed the ENTIRE prompt at full input price.
82,843 tokens charged instead of 0 — priced at Opus rates that is the 10.4x
overshoot, and it is the same defect
``test_full_cache_hit_is_not_repriced_as_uncached`` fixed for ``lib/cost.py``.

The boundary is the whole bug: equality is the common case (a fully cached
prefix), not an edge case.

TICKET CORRECTION (measured, recorded so the next reader is not misled)
-----------------------------------------------------------------------
The ticket also claims case ② — the hybrid ``sankuai_anthropic`` payload —
returns 174,983 instead of 162,854. That is NO LONGER true: 174,983 is
``input_tokens + cache_creation`` (162854 + 12129), the pre-``ebfd5464`` shape.
Since that commit ``normalize_usage`` resolves ``input`` to the cache-inclusive
total for this payload, so ``inp_raw > cw + cr`` is False and the heuristic
happens to fall through to the right answer. Case ② is asserted here anyway —
it must KEEP agreeing after delegation, and it is the regression that would
reappear if the alias order were ever reverted.

Case ③ (margin-of-one, 428603/428602) was already correct and stays correct:
the heuristic is only wrong on ONE side of the boundary, which is exactly why
a test suite that never touches equality stayed green.

GUARDS
  * Equality boundary: a full cache hit bills ZERO uncached input.
  * Delegation is structural: for a swept matrix of shapes, the estimator's
    uncached figure equals ``lib.cost.split_input_tokens`` EXACTLY — asserted
    against the real function, not a re-derived expectation.
  * The other three tuple members (cache_write / cache_read / output) survive
    delegation, since the canonical helper returns only a 2-tuple.
  * The budget gate itself does not fire on a fully-cached round.
"""

from __future__ import annotations

import itertools

import pytest

from lib.cost import split_input_tokens
from lib.cost_estimator import _split_tokens, check_budget, estimate_usage_cost

pytestmark = pytest.mark.unit


def _openai_full_hit(n: int = 82843) -> dict:
    """A round whose entire prompt was served from cache (OpenAI-compat wire)."""
    return {'prompt_tokens': n, 'completion_tokens': 100,
            'prompt_tokens_details': {'cached_tokens': n}}


def _hybrid_gateway() -> dict:
    """The real sankuai_anthropic mixed payload: BOTH prompt_tokens (cache
    inclusive) and the Anthropic residual cache_* keys."""
    return {'prompt_tokens': 5562791, 'completion_tokens': 200,
            'input_tokens': 162854,
            'cache_creation_input_tokens': 12129,
            'cache_read_input_tokens': 5387808}


class TestEqualityBoundaryIsTheBug:
    def test_full_cache_hit_bills_zero_uncached_input(self):
        """The cliff. Whole prompt from cache ⇒ nothing to charge input for."""
        uncached, cw, cr, out = _split_tokens(_openai_full_hit())
        assert uncached == 0, (
            'a fully cached prompt was re-priced as entirely uncached — the '
            '10x billing cliff at the equality boundary')
        assert cr == 82843, 'the cache read itself must still be reported'
        assert out == 100

    def test_full_cache_hit_does_not_trip_the_budget_gate(self):
        """End-to-end through the real consumer chain.

        A round that cost almost nothing must not abort the task. This is the
        behaviour the cliff actually threatened — check_budget is wired to the
        orchestrator's per-round gate.
        """
        exceeded, cost, reason = check_budget(
            {'id': 'task-full-hit'}, _openai_full_hit(),
            'claude-opus-5', max_budget_usd=0.05)
        assert exceeded is False, (
            f'a fully-cached round aborted the task at ${cost:.4f}: {reason}')

    def test_a_genuinely_expensive_round_still_trips_the_gate(self):
        """COMPLEMENT — the fix must not disarm the budget gate.

        Without this, 'always return 0 uncached' would satisfy every other
        test in this file while silently removing the cap.
        """
        expensive = {'prompt_tokens': 4_000_000, 'completion_tokens': 50_000}
        exceeded, cost, _ = check_budget(
            {'id': 'task-expensive'}, expensive,
            'claude-opus-5', max_budget_usd=0.05)
        assert exceeded is True, (
            f'a 4M-token uncached round did not trip a $0.05 cap (cost={cost})')


class TestDelegationToTheCanonicalChokepoint:
    """Structural: the estimator must not hold its own convention opinion."""

    @pytest.mark.parametrize('usage,label', [
        (_openai_full_hit(), 'openai full hit (equality boundary)'),
        (_hybrid_gateway(), 'hybrid sankuai_anthropic gateway payload'),
        ({'prompt_tokens': 428603, 'completion_tokens': 10,
          'prompt_tokens_details': {'cached_tokens': 428602}},
         'margin-of-one'),
        ({'input_tokens': 100, 'cache_creation_input_tokens': 50,
          'cache_read_input_tokens': 200, 'output_tokens': 75},
         'pure anthropic residual'),
        ({'prompt_tokens': 1000, 'completion_tokens': 500}, 'no cache'),
    ])
    def test_matches_canonical_on_named_shapes(self, usage, label):
        assert _split_tokens(usage)[0] == split_input_tokens(usage)[0], label

    def test_matches_canonical_across_a_swept_matrix(self):
        """Sweep the space instead of hand-picking cases.

        The original 4-case suite passed while the cliff was live precisely
        because it never crossed the equality boundary. A sweep cannot have
        that blind spot: every combination where uncached input is ambiguous
        is compared against the real chokepoint.
        """
        divergent = []
        for inp, cw, cr in itertools.product(
                [0, 1, 100, 82843, 428603], [0, 1, 12129],
                [0, 1, 82843, 428602]):
            usage = {'prompt_tokens': inp, 'completion_tokens': 1}
            if cr:
                usage['prompt_tokens_details'] = {'cached_tokens': cr}
            if cw:
                usage['cache_creation_input_tokens'] = cw
            got = _split_tokens(usage)[0]
            want = split_input_tokens(usage)[0]
            if got != want:
                divergent.append((inp, cw, cr, got, want))
        assert not divergent, (
            f'{len(divergent)} shapes disagree with lib.cost.split_input_tokens '
            f'(first 5: {divergent[:5]}) — the estimator is still deciding the '
            'cache convention itself instead of delegating')


class TestTupleContractPreserved:
    """``split_input_tokens`` returns only (uncached, total); the estimator's
    signature is a 4-tuple. Delegation must keep the other three members."""

    def test_four_tuple_shape_and_members(self):
        got = _split_tokens({
            'input_tokens': 100, 'cache_creation_input_tokens': 50,
            'cache_read_input_tokens': 200, 'output_tokens': 75})
        assert got == (100, 50, 200, 75)

    def test_empty_and_none(self):
        assert _split_tokens({}) == (0, 0, 0, 0)
        assert _split_tokens(None) == (0, 0, 0, 0)

    def test_non_dict_is_tolerated(self):
        assert _split_tokens('nonsense') == (0, 0, 0, 0)

    def test_cost_still_computed_for_a_normal_round(self):
        """Delegation must not zero out real cost."""
        cost = estimate_usage_cost(
            {'prompt_tokens': 100_000, 'completion_tokens': 1000},
            'claude-opus-5')
        assert cost > 0
