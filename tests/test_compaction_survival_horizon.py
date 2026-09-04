"""Economic compaction horizon follows prefix-rewrite cadence, not task age."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _system() -> dict:
    return {'role': 'system', 'content': 'Keep task state exact.'}


def _user(content: str) -> dict:
    return {'role': 'user', 'content': content}


@pytest.mark.parametrize(
    ('cadence', 'current_round', 'remaining_rounds',
     'expected_horizon', 'expected_policy'),
    [
        ({}, 64, 128, 1.0, 'fixed_one_round'),
        ({'_compactionCadenceLastRound': 60}, 64, 128,
         4.0, 'fixed_compaction_cadence'),
        ({'_compactionCadenceLastRound': 60,
          '_compactionCadenceRoundGaps': [5, 3]}, 64, 128,
         3.0, 'fixed_compaction_cadence'),
        ({'_compactionCadenceLastRound': 40,
          '_compactionCadenceRoundGaps': [10, 12]}, 64, 128,
         6.0, 'fixed_compaction_cadence'),
        ({'_compactionCadenceLastRound': 60}, 64, 2,
         2.0, 'fixed_compaction_cadence'),
        ({'_compactionCadenceLastRound': 60}, 64, 0,
         1.0, 'fixed_one_round'),
    ],
)
def test_fixed_policy_uses_only_budgeted_compaction_cadence(
    cadence,
    current_round,
    remaining_rounds,
    expected_horizon,
    expected_policy,
):
    from lib.tasks_pkg.compaction._layer2 import _compact as layer2

    task = {'config': {'compaction': {'strategy': 'fixed'}}, **cadence}
    horizon, policy = layer2._proactive_payback_policy(
        task,
        current_round=current_round,
        remaining_api_rounds=remaining_rounds,
    )

    assert horizon == expected_horizon
    assert policy == expected_policy


def test_adaptive_policy_keeps_its_explicit_expected_value_horizon():
    from lib.tasks_pkg.compaction._layer2 import _compact as layer2

    horizon, policy = layer2._proactive_payback_policy(
        {
            'config': {'compaction': {'strategy': 'adaptive'}},
            '_adaptiveCompactionDecision': {
                'shouldTrigger': True,
                'remainingRoundsMedian': 7.0,
            },
        },
        current_round=64,
        remaining_api_rounds=2,
    )

    assert horizon == 7.0
    assert policy == 'adaptive_expected_horizon'


def test_longer_observed_compaction_window_invalidates_stale_economic_veto(
    monkeypatch,
):
    import lib.tasks_pkg.cache_tracking._state as cache_state
    import lib.tasks_pkg.compaction._tokens as tokens

    def authoritative_count(_messages, _task, *, measurement_out=None):
        if measurement_out is not None:
            measurement_out.update({
                'message_tokens': 130_000,
                'message_count': len(_messages),
                'gate_tokens': 130_000,
                'method': 'test',
            })
        return 130_000, 'test'

    monkeypatch.setattr(
        tokens, '_count_tokens_authoritative', authoritative_count)
    monkeypatch.setattr(
        cache_state, 'get_warm_cache_read', lambda *args, **kwargs: 120_000)

    def task() -> dict:
        return {
            'convId': 'survival-veto',
            '_userId': 1,
            'config': {'model': 'kimi-k3'},
            '_compactionCadenceLastRound': 2,
            '_autoCompactRetryAfterTokens': 250_000,
            '_autoCompactRetryWitness': {
                'reason': 'cache_negative',
                'cacheReadTokens': 120_000,
                'paybackLimitRounds': 1.0,
            },
        }

    still_short = task()
    assert tokens._should_force_compact(
        [_system(), _user('continue')],
        still_short,
        current_round=3,
        remaining_api_rounds=189,
    ) is False
    assert still_short['_autoCompactRetryAfterTokens'] == 250_000

    earned_second_round = task()
    assert tokens._should_force_compact(
        [_system(), _user('continue')],
        earned_second_round,
        current_round=4,
        remaining_api_rounds=188,
    ) is True
    assert '_autoCompactRetryAfterTokens' not in earned_second_round
    assert '_autoCompactRetryWitness' not in earned_second_round


def test_three_round_candidate_requires_observed_three_round_window(
    monkeypatch,
):
    from lib.tasks_pkg.compaction._layer2 import _compact as layer2

    summary_calls = []
    monkeypatch.setattr(
        layer2, '_projected_summary_usage_tokens',
        lambda *args, **kwargs: 10_000)

    def summarize(*args, **kwargs):
        summary_calls.append((args, kwargs))
        kwargs['usage_out'].update({
            'prompt_tokens': 8_000,
            'completion_tokens': 500,
        })
        return 'Objective and unfinished implementation remain preserved.'

    monkeypatch.setattr(
        layer2, '_generate_query_aware_summary', summarize)

    def economics(
        _task,
        *,
        tokens_before,
        candidate_tokens,
        summary_usage_tokens=0,
    ):
        return {
            'cache_read_tokens': 100_000,
            'cache_rewrite_tokens': candidate_tokens,
            'dropped_tokens': max(1, tokens_before - candidate_tokens),
            'cache_write_mul': 1.0,
            'cache_read_mul': 0.1,
            'rewrite_cost_tokens': candidate_tokens,
            'summary_cost_tokens': summary_usage_tokens,
            'payback_rounds': 3.0,
            'pricing_source': 'test',
        }

    monkeypatch.setattr(layer2, '_proactive_cache_economics', economics)

    def messages() -> list[dict]:
        return [
            _system(),
            _user('Objective: finish the implementation.'),
            {'role': 'assistant',
             'content': 'old state ' + ('x' * 20_000)},
            _user('continue'),
            {'role': 'assistant', 'content': 'current state'},
        ]

    early_messages = messages()
    early_meta = {}
    layer2.execute_compact_tool(
        early_messages,
        task={'convId': 'fixed-early', 'id': 't',
              'config': {'model': 'kimi-k3'}},
        preserve_budget_tokens=1,
        _proactive_economic=True,
        _compaction_skip_archive=True,
        _compaction_round=3,
        _compaction_remaining_api_rounds=189,
        _result_meta=early_meta,
    )

    assert summary_calls == []
    assert early_meta['reason'] == 'cache_negative'
    assert early_meta['economics']['payback_limit_rounds'] == 1.0

    late_messages = messages()
    late_meta = {}
    late_task = {
        'convId': 'fixed-late', 'id': 't',
        'config': {'model': 'kimi-k3'},
        '_compactionCadenceLastRound': 5,
    }
    layer2.execute_compact_tool(
        late_messages,
        task=late_task,
        preserve_budget_tokens=1,
        _proactive_economic=True,
        _compaction_skip_archive=True,
        _compaction_round=8,
        _compaction_remaining_api_rounds=184,
        _result_meta=late_meta,
    )

    assert len(summary_calls) == 1
    assert late_meta['compacted'] is True
    assert late_meta['economics']['payback_limit_rounds'] == 3.0
    assert late_meta['economics']['payback_policy'] == (
        'fixed_compaction_cadence')


def test_successful_rewrite_cadence_is_bounded_and_idempotent():
    from lib.tasks_pkg.compaction import _tokens as tokens

    task = {}
    assert tokens._record_compaction_cadence(task, 3) is None
    assert tokens._record_compaction_cadence(task, 3) is None
    for round_num in range(4, 20):
        assert tokens._record_compaction_cadence(task, round_num) == 1

    assert task['_compactionCadenceLastRound'] == 19
    assert task['_compactionCadenceRoundGaps'] == [1] * 8


def test_cache_economics_resolves_before_and_after_pricing_tiers(monkeypatch):
    import lib.tasks_pkg.cache_tracking._state as cache_state
    from lib.pricing import clear_provider_pricing, set_provider_pricing
    from lib.tasks_pkg.compaction._layer2 import _compact as layer2

    monkeypatch.setattr(
        cache_state, 'get_warm_cache_read',
        lambda *args, **kwargs: 280_000)
    set_provider_pricing('econ-provider', 'econ-model', {
        'contextTiers': [
            {'id': 'base', 'maxPromptTokens': 250_000, 'input': 1,
             'output': 2, 'cacheReadMul': .1, 'cacheWriteMul': 1.25},
            {'id': 'premium', 'maxPromptTokens': 1_000_000, 'input': 2,
             'output': 3, 'cacheReadMul': .1, 'cacheWriteMul': 1.25},
        ]})
    try:
        economics = layer2._proactive_cache_economics(
            {'convId': 'tier-economics', '_userId': 1,
             'provider_id': 'econ-provider',
             'config': {'model': 'econ-model'}},
            tokens_before=300_000,
            candidate_tokens=200_000,
            summary_usage_tokens=10_000,
        )
    finally:
        clear_provider_pricing('econ-provider')

    assert economics['crosses_pricing_tier'] is True
    assert economics['pricing_before']['tier_id'] == 'premium'
    assert economics['pricing_after']['tier_id'] == 'base'
    assert economics['cache_replay_tokens_before'] == 280_000
    assert economics['cache_replay_tokens_after'] == 200_000
    assert economics['savings_per_round_usd'] == pytest.approx(0.036)
    assert economics['rewrite_cost_usd'] == pytest.approx(0.25)


def test_pipeline_threads_remaining_round_budget_into_l2(monkeypatch):
    import lib.tasks_pkg.compaction._pipeline as pipeline

    captured = []
    monkeypatch.setattr(pipeline, 'micro_compact', lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        pipeline,
        'force_compact_if_needed',
        lambda *args, **kwargs: captured.append(kwargs) or False,
    )

    pipeline.run_compaction_pipeline(
        [_user('continue')],
        current_round=64,
        task={'convId': 'budget-thread', 'config': {}},
        remaining_api_rounds=2,
    )

    assert captured[0]['_compaction_round'] == 64
    assert captured[0]['_compaction_remaining_api_rounds'] == 2
