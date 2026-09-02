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
    monkeypatch.setattr(ld, 'dispatch_chat',
                        lambda msgs, **kw: ('SUMMARY', {'prompt_tokens': 4200,
                                                        'completion_tokens': 310,
                                                        'total_tokens': 4510}))
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
                         task={'convId': 'cv3', 'config': {'model': 'deepseek-v4-flash'}},
                         advanced_steps=['summarize_openclaw'])
    g = cu.get_compaction_usage('cv3')
    assert g.get('prompt_tokens') == 4200, f'summarizer usage not captured: {g}'
    assert g.get('completion_tokens') == 310
    cu.reset_compaction_usage('cv3')
