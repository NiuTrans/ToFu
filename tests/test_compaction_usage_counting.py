"""Regression tests for precise compaction-cost counting.

Compaction's own LLM calls (L2 smart-summary + advanced-host summarizers)
must have their token usage counted toward the conversation's cost —
otherwise summary-based arms appear artificially cheaper than prune-only
arms, biasing the experiment. These tests pin the accumulator + the
advanced-host capture path.

Run:  pytest tests/test_compaction_usage_counting.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.mark.unit
def test_accumulator_sums_and_pops():
    from lib.tasks_pkg.compaction._compaction_usage import (
        record_compaction_usage, get_compaction_usage,
        pop_compaction_usage, reset_compaction_usage)
    reset_compaction_usage('cv')
    record_compaction_usage('cv', {'prompt_tokens': 1000, 'completion_tokens': 200,
                                   'total_tokens': 1200,
                                   '_dispatch': {
                                       'latency_ms': 50,
                                       'queue_wait_ms': 5,
                                       'queue_wait_measurement': 'dispatcher_backpressure_only',
                                       'first_content_at_unix_ns': 200,
                                       'ttft_measurement': 'upper_bound',
                                   }}, 'L2')
    record_compaction_usage('cv', {'prompt_tokens': 500, 'completion_tokens': 100,
                                   'total_tokens': 600,
                                   '_dispatch': {
                                       'latency_ms': 25,
                                       'queue_wait_ms': 7,
                                       'queue_wait_measurement': 'dispatcher_backpressure_only',
                                       'first_content_at_unix_ns': 150,
                                       'ttft_measurement': 'upper_bound',
                                   }}, 'advanced')
    g = get_compaction_usage('cv')
    assert g['prompt_tokens'] == 1500
    assert g['completion_tokens'] == 300
    assert g['total_tokens'] == 1800
    assert g['n_calls'] == 2
    assert g['timing'] == {
        'modelWallMs': 75.0,
        'queueWaitMs': 12.0,
        'queueMeasurement': 'dispatcher_backpressure_only',
        'firstModelOutputAtUnixNs': 150,
        'ttftMeasurement': 'upper_bound',
    }
    # pop clears
    popped = pop_compaction_usage('cv')
    assert popped['prompt_tokens'] == 1500
    assert get_compaction_usage('cv') == {}


@pytest.mark.unit
def test_accumulator_ignores_empty_and_none():
    from lib.tasks_pkg.compaction._compaction_usage import (
        record_compaction_usage, get_compaction_usage)
    record_compaction_usage('', {'prompt_tokens': 9}, 'x')      # empty conv
    assert get_compaction_usage('') == {}
    record_compaction_usage('cv2', None, 'x')                   # None usage
    assert get_compaction_usage('cv2') == {}


@pytest.mark.unit
def test_advanced_summarizer_usage_is_captured(monkeypatch):
    """The advanced-host summarizer's dispatch_chat usage must land in the
    accumulator (the bug this fixes: usage was discarded)."""
    import lib.tasks_pkg.compaction._advanced as adv
    import lib.tasks_pkg.compaction._faithful_methods._openclaw as openclaw
    import lib.tasks_pkg.compaction._compaction_usage as cu
    import lib.llm_dispatch as ld

    cu.reset_compaction_usage('cv3')
    dispatch_kwargs = {}

    def _dispatch_chat(_messages, **kwargs):
        dispatch_kwargs.update(kwargs)
        return ('SUMMARY', {'prompt_tokens': 4200,
                            'completion_tokens': 310,
                            'total_tokens': 4510})

    monkeypatch.setattr(ld, 'dispatch_chat', _dispatch_chat)
    monkeypatch.setattr(openclaw, '_raw_context_limit', lambda ctx: 200_000)
    monkeypatch.setattr(openclaw, '_tok', lambda m, t: 999_999)
    monkeypatch.setattr(openclaw, '_cooldown_ok', lambda c: True)
    monkeypatch.setattr(openclaw, '_select_middle_turns',
                        lambda ctx, keep_recent_tokens, protect_first_n=1, protect_last_n=0:
                        ([t for t in ctx.edit.turns()[1:-1]], 'MIDDLE ' * 200))

    msgs = [{'role': 'system', 'content': 's'},
            {'role': 'user', 'content': 'orig ' * 40}]
    for i in range(6):
        msgs += [{'role': 'assistant', 'content': f'w{i} ' * 40},
                 {'role': 'user', 'content': f'c{i}'}]
    msgs += [{'role': 'user', 'content': 'tail'}]

    adv.advanced_compact(msgs, conv_id='cv3',
                         task={'convId': 'cv3', '_userId': 41,
                               'config': {'model': 'deepseek-v4-flash'}},
                         advanced_steps=['summarize_openclaw'])
    g = cu.get_compaction_usage('cv3')
    assert g.get('prompt_tokens') == 4200, f'summarizer usage not captured: {g}'
    assert g.get('completion_tokens') == 310
    assert dispatch_kwargs['prefer_model'] == 'deepseek-v4-flash'
    assert dispatch_kwargs['owner_user_id'] == 41
    assert 'model' not in dispatch_kwargs
    cu.reset_compaction_usage('cv3')



@pytest.mark.unit
def test_accumulator_canonicalizes_vendor_aliases_and_keeps_call_rows():
    from lib.tasks_pkg.compaction._compaction_usage import (
        pop_compaction_usage, record_compaction_usage, reset_compaction_usage)

    reset_compaction_usage('cv-alias')
    record_compaction_usage('cv-alias', {
        'input_tokens': 12,
        'output_tokens': 3,
        'cached_tokens': 90,
        '_dispatch': {'model': 'kimi-k3', 'provider_id': 'gateway'},
    }, 'L2')
    got = pop_compaction_usage('cv-alias')

    assert got['cache_read_tokens'] == 90
    assert got['n_calls'] == 1
    assert got['calls'][0]['kind'] == 'L2'
    assert got['calls'][0]['model'] == 'kimi-k3'
    assert got['calls'][0]['provider_id'] == 'gateway'
    assert got['calls'][0]['usage']['cache_read_tokens'] == 90


@pytest.mark.unit
def test_accumulator_does_not_double_sum_canonicalized_cache_aliases():
    from lib.tasks_pkg.compaction._compaction_usage import (
        pop_compaction_usage, record_compaction_usage, reset_compaction_usage)

    reset_compaction_usage('cv-cache')
    record_compaction_usage('cv-cache', {
        'prompt_tokens': 100,
        'cached_tokens': 80,
        'cache_read_tokens': 80,
    })
    got = pop_compaction_usage('cv-cache')

    assert got['cache_read_tokens'] == 80
    assert got['cached_tokens'] == 80



@pytest.mark.unit
def test_settled_merge_keeps_main_and_internal_calls_without_mutation():
    from lib.tasks_pkg.compaction._compaction_usage import (
        merge_compaction_usage_into_total)

    main = {'prompt_tokens': 478_100, 'completion_tokens': 266,
            'cache_read_tokens': 14_300}
    compact = {
        'n_calls': 1,
        'calls': [{'kind': 'L2', 'usage': {'input_tokens': 10_000}}],
        'timing': {'modelWallMs': 50},
        'input_tokens': 10_000,
        'output_tokens': 500,
        'cache_read_input_tokens': 8_000,
    }

    merged = merge_compaction_usage_into_total(main, compact)

    assert merged['prompt_tokens'] == 478_100
    assert merged['input_tokens'] == 10_000
    assert merged['completion_tokens'] == 266
    assert merged['output_tokens'] == 500
    assert merged['cache_read_tokens'] == 14_300
    assert merged['cache_read_input_tokens'] == 8_000
    assert 'n_calls' not in merged and 'calls' not in merged
    assert main['prompt_tokens'] == 478_100
    assert compact['n_calls'] == 1
