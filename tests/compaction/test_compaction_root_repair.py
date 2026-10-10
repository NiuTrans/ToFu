"""Cold reconstruction cannot borrow cache evidence or bypass safe admission."""
import copy

import pytest

pytestmark = pytest.mark.unit


def test_cache_observation_is_task_and_route_scoped(monkeypatch):
    from lib.tasks_pkg.compaction._cache_evidence import current_cache_read
    from lib.tasks_pkg.llm_fallback import _usage
    monkeypatch.setattr(_usage, 'append_event', lambda *a, **k: None)
    task = {'id': 'new', 'model': 'kimi-k3', 'provider_id': 'gateway',
            'config': {'lastTurnCacheRead': 152064}}
    assert current_cache_read(task) == 0
    _usage._emit_round_usage(task, 1, 'kimi-k3', {
        'prompt_tokens': 20000, 'completion_tokens': 10,
        'prompt_tokens_details': {'cached_tokens': 19000},
        '_dispatch': {'provider_id': 'gateway'},
    })
    assert current_cache_read(task) == 19000
    for key, value in [('id', 'other'), ('model', 'gpt-6-astra'),
                       ('provider_id', 'other')]:
        changed = {**task, key: value}
        assert current_cache_read(changed) == 0
    _usage._emit_round_usage(task, 2, 'kimi-k3', {
        'prompt_tokens': 20000, '_dispatch': {'provider_id': 'gateway'}})
    assert current_cache_read(task) == 0


def test_over_window_proactive_call_disables_economic_veto(monkeypatch):
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    captured = {}
    def gate(messages, task, measurement_out=None, **kwargs):
        measurement_out.update(gate_tokens=2259160, message_tokens=2250000)
        return True
    def execute(messages, **kwargs):
        captured.update(kwargs)
        return ''
    monkeypatch.setattr(compact, '_should_force_compact', gate)
    monkeypatch.setattr(compact, 'execute_compact_tool', execute)
    monkeypatch.setattr(compact, '_get_context_limit', lambda task: 1000000)
    monkeypatch.setattr(compact, '_usable_context', lambda limit: 864000)
    compact.force_compact_if_needed(
        [{'role': 'user', 'content': 'goal'}],
        {'config': {'model': 'kimi-k3'}}, _compaction_round=0)
    assert captured['_proactive_economic'] is False


def test_empty_target_exclusion_does_not_admit_resume_payload(monkeypatch):
    from lib.tasks_pkg import model_replay
    rows = [{'turnId': 'u', 'actor': 'human', 'projection': {'content': 'goal'}},
            {'turnId': 'a', 'actor': 'assistant', 'projection': {'content': 'partial'}}]
    cfg = {'excludeLast': True, 'model': 'kimi-k3'}
    original = copy.deepcopy(rows)
    assert model_replay.restore_prefix(rows, cfg, conversation_id='c', user_id=1) == ([], 0)
    assert cfg['_modelReplayStatus'] == 'resume_target'
    assert rows == original


@pytest.mark.parametrize('round_num', [0, 1])
@pytest.mark.parametrize('working_set', [128000, 256000])
def test_recovery_target_uses_physical_capacity_not_advisory_working_set(
        round_num, working_set):
    from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
    from lib.tasks_pkg.compaction._prompt_admission import _resolved_ceiling
    task = {'config': {'model': 'kimi-k3',
                       'compaction': {'workingSetTokens': working_set}}}
    # Price-tier/working-set targets and first-dispatch targets are advisory.
    # Physical route capacity reserves output and safety headroom every round.
    assert resolve_admission_budget(task, round_num=round_num) == (777600, 777600)
    assert task['_contextBudgetDecision']['economicTargetTokens'] == working_set
    assert _resolved_ceiling(task, round_num=round_num) == (777600, 777600)
