"""Authoritative settled-cost fold into the durable turn projection.

Root cause (2026-08-29, conversation mtd9ci53zq3xfm): ``_task_projection``
folded ``usage`` and the per-round ``apiRounds`` ledger but never an
authoritative top-level ``cost`` total. On reload the finish footer therefore
had no ``msg.cost`` and fell back to the client-side ``calcCostCny``
micro-batch; that fill mutates no Turn fact, so the surface footer compare
skipped the re-render and the cost tag + hover breakdown never appeared.

The fold mirrors the done-event stamp in ``orchestrator/_finalize`` so live
and reload paths read the same number from the same ``lib.cost`` formula;
``apiRounds`` stays the per-round breakdown ledger.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'task-costfold1', 'convId': 'conv-1', 'config': {}}


def test_projection_folds_settled_cost_from_usage():
    from lib.turn_lifecycle import _task_projection

    task = _task()
    task['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 500}
    task['model'] = 'gpt-4o'
    projection = _task_projection(task, {})
    cost = projection.get('cost')
    assert cost is not None
    assert cost['costCny'] > 0
    assert cost['totalInputTokens'] == 1000
    assert cost['outputTokens'] == 500


def test_projection_without_usage_has_no_cost_key():
    from lib.turn_lifecycle import _task_projection

    projection = _task_projection(_task(), {})
    assert 'cost' not in projection


def test_projection_zero_usage_has_no_cost_key():
    from lib.turn_lifecycle import _task_projection

    task = _task()
    task['usage'] = {'prompt_tokens': 0, 'completion_tokens': 0}
    projection = _task_projection(task, {})
    assert 'cost' not in projection


def test_cost_fold_recomputes_carried_stale_cost():
    """The projection is cumulative (``dict(previous)``), but the fold
    RE-DERIVES cost from the carried usage on every write — a stale carried
    value can never survive next to different math (the single lib.cost
    formula owns the number)."""
    from lib.cost import compute_cost
    from lib.turn_lifecycle import _task_projection

    usage = {'prompt_tokens': 1000, 'completion_tokens': 500}
    previous = {'usage': usage, 'model': 'gpt-4o',
                'cost': {'costCny': 999.0, 'totalInputTokens': 1}}
    projection = _task_projection(_task(), previous)
    expected = compute_cost(usage, model_id='gpt-4o')
    assert projection['cost']['costCny'] == expected['costCny']
    assert projection['cost']['totalInputTokens'] == 1000


def test_mixed_model_rounds_are_priced_individually_before_aggregation():
    from lib.cost import compute_cost
    from lib.turn_lifecycle import _task_projection

    rounds = [
        {'model': 'gpt-4o',
         'usage': {'prompt_tokens': 10_000, 'completion_tokens': 1_000,
                   'cache_read_tokens': 8_000}},
        {'model': 'gpt-5.6-luna',
         'usage': {'prompt_tokens': 20_000, 'completion_tokens': 2_000,
                   'cache_read_tokens': 15_000}},
    ]
    task = _task()
    task.update({
        'model': 'gpt-5.6-sol',
        'usage': {'prompt_tokens': 30_000, 'completion_tokens': 3_000,
                  'cache_read_tokens': 23_000},
        'apiRounds': rounds,
    })

    projection = _task_projection(task, {})
    expected = sum(
        compute_cost(entry['usage'], model_id=entry['model'])['costCny']
        for entry in rounds
    )
    wrongly_flattened = compute_cost(
        task['usage'], model_id='gpt-5.6-sol')['costCny']

    assert projection['cost']['costCny'] == pytest.approx(expected, abs=1e-4)
    assert projection['cost']['costCny'] != wrongly_flattened
    assert projection['cost']['pricingSource'] == 'api_round_aggregate'
    assert projection['cost']['pricingSnapshot']['models'] == [
        'gpt-4o', 'gpt-5.6-luna']
    assert projection['cost']['totalInputTokens'] == 30_000
    assert projection['cost']['cacheReadTokens'] == 23_000


def test_cost_fold_failure_is_non_fatal(monkeypatch):
    """Pricing resolution is display-only: a failure must never break the
    projection write itself."""
    import lib.cost
    from lib.turn_lifecycle import _task_projection

    def _boom(*args, **kwargs):
        raise RuntimeError('pricing table exploded')

    monkeypatch.setattr(lib.cost, 'compute_cost', _boom)
    task = _task()
    task['usage'] = {'prompt_tokens': 1000, 'completion_tokens': 500}
    projection = _task_projection(task, {})
    assert 'cost' not in projection
    assert projection['usage']['prompt_tokens'] == 1000
