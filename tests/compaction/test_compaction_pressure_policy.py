"""Warm-prefix L1 candidates and L2 share one budget and one adoption decision."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._pressure import try_pressure_l1
from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

pytestmark = pytest.mark.unit


def history():
    messages = [{'role': 'system', 'content': 'Do not lose the objective.'},
                {'role': 'user', 'content': 'Investigate the bug.'}]
    for index in range(6):
        call_id = f'call-{index}'
        messages.extend([
            {'role': 'assistant', 'content': '', 'reasoning_content': 'keep reasoning',
             'tool_calls': [{'id': call_id, 'type': 'function',
                             'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'name': 'read_files', 'tool_call_id': call_id,
             'content': 'observed evidence 0123456789\n' * 1500},
        ])
    return messages


@pytest.fixture
def harness(monkeypatch):
    events, notices = [], []
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda *a, **k: events.append((a, k)))
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.notify_compaction',
                        lambda *a, **k: notices.append(k))
    monkeypatch.setattr('lib.tasks_pkg.compaction._tokens._count_tokens_authoritative',
                        lambda messages, task, **kw: (_estimate_total_tokens(messages), 'test'))
    task = {'id': 'task', '_userId': 7, 'convId': 'conv',
            'config': {'model': 'deepseek-v4.1-flash'}}
    return task, events, notices


def test_required_l1_resolves_pressure_preserving_newest_batch_and_reasoning(harness):
    task, events, notices = harness
    messages = history()
    original = deepcopy(messages)
    assert try_pressure_l1(messages, task, target_tokens=30_000,
                           total_tokens=_estimate_total_tokens(messages), mandatory=True)
    assert _estimate_total_tokens(messages) <= 30_000
    assert messages[:2] == original[:2]
    assert messages[-2:] == original[-2:]
    assert [m.get('reasoning_content') for m in messages] == [m.get('reasoning_content') for m in original]
    assert events and len(notices) == 1
    assert notices[0]['intended_rewrite_index'] >= 2
    assert history() == original  # durable source was never rewritten


def test_rejected_cache_negative_candidate_emits_nothing(harness, monkeypatch):
    task, events, notices = harness
    monkeypatch.setattr('lib.tasks_pkg.compaction._cache_economics._proactive_cache_economics',
                        lambda *a, **k: {'cache_read_tokens': 50_000, 'payback_rounds': 1000})
    messages = history()
    original = deepcopy(messages)
    assert not try_pressure_l1(messages, task, target_tokens=30_000,
                               total_tokens=_estimate_total_tokens(messages))
    assert messages == original
    assert not events and not notices


def test_trial_has_no_durable_or_telemetry_side_effects_and_bounded_passes(harness, monkeypatch):
    task, events, notices = harness
    from lib.tasks_pkg.compaction import _layer1
    real = _layer1.micro_compact
    passes, telemetry = [], []
    def run(*args, **kwargs):
        passes.append(bool(kwargs.get('_dry_run')))
        return real(*args, **kwargs)
    monkeypatch.setattr(_layer1, 'micro_compact', run)
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._prefix.get_cache_prefix_count',
                        lambda *a, **k: pytest.fail('pressure reads cache storage'))
    monkeypatch.setattr('lib.context_telemetry.record_compaction_event',
                        lambda *a, **k: telemetry.append(k))
    messages = history()
    assert try_pressure_l1(messages, task, target_tokens=30_000,
                           total_tokens=_estimate_total_tokens(messages), mandatory=True)
    assert passes == [True, False]
    assert len(telemetry) == 1  # no dry-run compaction event
    assert isinstance(task['_lastPressureL1'], dict)  # one bounded receipt, no growing log


def test_archival_experiment_never_creates_speculative_artifacts(harness, monkeypatch):
    task, _, _ = harness
    task['config']['compaction'] = {'evidenceLedger': True}
    monkeypatch.setattr('lib.tasks_pkg.compaction._persist._persist_to_disk',
                        lambda *a, **k: pytest.fail('speculative archive'))
    messages = history()
    assert not try_pressure_l1(messages, task, target_tokens=30_000,
                               total_tokens=_estimate_total_tokens(messages), mandatory=True)


def test_insufficient_l1_candidate_is_not_applied_before_l2(harness):
    task, events, notices = harness
    messages = history()
    original = deepcopy(messages)
    assert not try_pressure_l1(messages, task, target_tokens=1000,
                               total_tokens=_estimate_total_tokens(messages), mandatory=True)
    assert messages == original
    assert not events and not notices


def test_cancelled_pressure_does_no_candidate_work(harness, monkeypatch):
    task, events, notices = harness
    task['aborted'] = True
    monkeypatch.setattr('lib.tasks_pkg.compaction._layer1.micro_compact',
                        lambda *a, **k: pytest.fail('work after cancellation'))
    assert not try_pressure_l1(history(), task, target_tokens=1000, total_tokens=100_000)


def test_economic_retry_decline_survives_final_dispatch(monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    task = {'config': {'model': 'kimi-k3', 'compaction': {'workingSetTokens': 128_000}},
            '_autoCompactRetryAfterTokens': 220_000,
            '_autoCompactRetryWitness': {'reason': 'cache_negative'}}
    monkeypatch.setattr(admission, '_measure', lambda *a, **k: {
        'totalTokens': 180_000, 'messageTokens': 170_000, 'toolSchemaTokens': 10_000})
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: pytest.fail('forced economic decline'))
    admission.enforce_dispatch_prompt_limit([{'role': 'user', 'content': 'continue'}],
                                            [], task, round_num=8, model='kimi-k3')
    assert task['_lastPromptAdmission']['action'] == 'admit'


def test_final_guard_uses_l1_without_buying_a_summary(harness, monkeypatch):
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    task, events, notices = harness
    monkeypatch.setattr(admission, '_resolved_ceiling', lambda *a, **k: (30_000, 30_000))
    monkeypatch.setattr(admission, '_measure', lambda messages, *a, **k: {
        'totalTokens': _estimate_total_tokens(messages) + 500,
        'messageTokens': _estimate_total_tokens(messages), 'toolSchemaTokens': 500})
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: pytest.fail('L2 after sufficient L1'))
    result = admission.enforce_dispatch_prompt_limit(
        history(), [], task, round_num=8, model='deepseek-v4.1-flash')
    assert result['totalTokens'] <= 30_000
    assert task['_lastPromptAdmission']['action'] == 'prune_then_admit'
    assert events and len(notices) == 1


def test_proactive_l1_adoption_does_not_publish_an_l2_receipt(harness, monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as layer2
    task, events, notices = harness
    messages = history()
    def gate(messages, task, *, measurement_out, **kwargs):
        measurement_out.update(gate_tokens=_estimate_total_tokens(messages),
                               message_tokens=_estimate_total_tokens(messages))
        return True
    monkeypatch.setattr(layer2, '_should_force_compact', gate)
    monkeypatch.setattr(layer2, '_compaction_trigger_threshold',
                        lambda *a, **k: (30_000, 30_000, 0))
    monkeypatch.setattr(layer2, 'execute_compact_tool',
                        lambda *a, **k: pytest.fail('summary after L1 resolved target'))
    assert layer2.force_compact_if_needed(messages, task, _compaction_round=8) is False
    assert _estimate_total_tokens(messages) <= 30_000
    assert events and len(notices) == 1
