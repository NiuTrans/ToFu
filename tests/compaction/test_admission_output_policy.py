"""Output caps adapt to complete prompts; native bound reservations do not."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from lib.llm.body import build_body
from lib.llm.body._clamp import _clamp_completion_to_context_window
from lib.llm_dispatch.context_policy import task_context_profile
from lib.llm_dispatch.native_context_scope import native_context_scope
from lib.llm_dispatch.slot import Slot
from lib.llm.llm_errors import ContextCompactionError, PromptTooLongError
from lib.tasks_pkg.compaction._admission_budget import resolve_admission_budget
from lib.tasks_pkg.compaction import _prompt_admission as admission

pytestmark = pytest.mark.unit


def task_and_route(monkeypatch, *, window=128_000, output_cap=128_000,
                   model='gpt-5.6-sol'):
    slot = Slot(key_name='test', api_key='', model=model,
                logical_model=model, capabilities={'text'},
                context_window=window, max_output_tokens=output_cap,
                provider_id='request-budget', routing_provider_id='gateway',
                routing_owner_user_id=7, request_scoped=True)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher',
                        lambda: SimpleNamespace(slots=[slot]))
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit',
                        lambda provider, model, declared: declared)
    task = {'id': 'budget-task', '_userId': 7,
            '_pinned_provider_id': 'request-budget',
            'config': {'model': slot.model, 'maxTokens': 128_000,
                       'compaction': {'workingSetTokens': 1000}}}
    return task, slot


def test_astra_catalog_window_admits_reported_full_prompt(monkeypatch):
    task, slot = task_and_route(monkeypatch, window=272_000, model='gpt-6-astra')
    messages = [{'role': 'user', 'content': 'required objective'}]
    tools = [{'type': 'function', 'function': {'name': 'read_files',
             'parameters': {'type': 'object', 'properties': {}}}}]
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda *a, **kw: (81_308, 'test_complete_prompt'))
    monkeypatch.setattr(admission, '_tool_schema_tokens', lambda *a, **kw: 21_577)
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **kw: pytest.fail('fitting input needs no summary'))
    measured = admission.enforce_dispatch_prompt_limit(
        messages, tools, task, round_num=5, model=slot.model)
    assert measured['messageTokens'] == 59_731
    assert measured['totalTokens'] == 81_308
    assert task['_lastPromptAdmission']['action'] == 'admit'
    decision = task['_contextBudgetDecision']
    assert decision['contextWindowTokens'] == 272_000
    assert decision['hardCeilingTokens'] == 144_000
    assert decision['resolvedOutputTokens'] == 128_000
    assert decision['windowSafetyTokens'] < 272_000


@pytest.mark.parametrize('round_num', [0, 5])
def test_incident_complete_prompt_admitted_then_body_clamps(monkeypatch, round_num):
    task, _ = task_and_route(monkeypatch)
    config = deepcopy(task['config'])
    messages = [{'role': 'user', 'content': 'required objective'}]
    tools = [{'type': 'function', 'function': {'name': 'read_files',
             'parameters': {'type': 'object', 'properties': {}}}}]
    surfaces = []
    def count(*args, **kwargs):
        surfaces.append(kwargs['tool_schema'])
        return 29_030, 'test_complete_prompt'
    monkeypatch.setattr(admission, '_count_tokens_authoritative', count)
    monkeypatch.setattr(admission, '_tool_schema_tokens', lambda *a, **k: 13_833)
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: pytest.fail('fitting input needs no summary'))
    measured = admission.enforce_dispatch_prompt_limit(
        messages, tools, task, round_num=round_num, model='gpt-5.6-sol')
    assert measured['messageTokens'] == 15_197
    assert measured['totalTokens'] == 29_030
    assert surfaces == [tools]
    hard, _ = resolve_admission_budget(task, input_tokens=measured['totalTokens'])
    body = build_body('gpt-5.6-sol', messages, tools=tools, max_tokens=128_000,
                      precomputed_input_tokens=measured['totalTokens'],
                      context_window=128_000, defer_reasoning=True)
    assert 29_030 <= hard <= task['_contextBudgetDecision']['windowSafetyTokens']
    assert body['max_tokens'] == task['_contextBudgetDecision']['resolvedOutputTokens']

    assert task['_contextBudgetDecision']['requestedOutputTokens'] == 128_000
    assert task['_contextBudgetDecision']['outputCapTokens'] == 128_000
    assert task['_contextBudgetDecision']['outputResolutionSource'] == 'task_config'
    assert body['max_tokens'] > 8192
    assert body['max_tokens'] + measured['totalTokens'] < 128_000
    assert task['config'] == config
    assert messages == [{'role': 'user', 'content': 'required objective'}]


@pytest.mark.parametrize('total', [1000, 29_030, 60_000, 80_640, 100_000, 127_999])
def test_current_input_refreshes_cap_and_never_expands_safety(monkeypatch, total):
    task, _ = task_and_route(monkeypatch)
    hard, target = resolve_admission_budget(task, input_tokens=total)
    decision = task['_contextBudgetDecision']
    expected_output = _clamp_completion_to_context_window(
        'gpt-5.6-sol', [{'role': 'user', 'content': 'input'}], 128_000,
        context_window=128_000, precomputed_input_tokens=total)
    assert decision['resolvedOutputTokens'] == expected_output
    assert hard == target == min(decision['windowSafetyTokens'], 128_000 - expected_output)
    assert (total <= hard) == (total <= decision['windowSafetyTokens']
                              and total + expected_output <= 128_000)
    resolve_admission_budget(task, input_tokens=29_030)
    assert task['_contextBudgetDecision']['outputResolutionInputTokens'] == 29_030


@pytest.mark.parametrize('invalid', [None, 0, -1, True, 1.5, '29030'])
def test_missing_current_count_plans_at_safety_not_stale_receipt(monkeypatch, invalid):
    task, _ = task_and_route(monkeypatch)
    task['_lastPromptAdmission'] = {'totalTokens': 127_999}
    hard, _ = resolve_admission_budget(task, input_tokens=invalid)
    assert hard == task['_contextBudgetDecision']['windowSafetyTokens']
    assert task['_contextBudgetDecision']['outputResolutionInputSource'] == 'safety_boundary'


@pytest.mark.parametrize('output', [8192, 120_000, 127_999])
def test_native_bound_output_never_reclamped(monkeypatch, output):
    task, _ = task_and_route(monkeypatch)
    metadata = {'owner_user_id': 7, 'context_window': 128_000,
                'provider_id': 'gateway', 'wire_model': 'gpt-5.6-sol',
                'logical_model': 'gpt-5.6-sol'}
    with native_context_scope(task, metadata, output):
        for total in (1000, 29_030, 90_000):
            hard, _ = resolve_admission_budget(task, input_tokens=total)
            assert task['_contextBudgetDecision']['resolvedOutputTokens'] == output
            assert hard == min(task['_contextBudgetDecision']['windowSafetyTokens'],
                               128_000 - output)
        foreign = dict(task, _userId=8)
        assert task_context_profile(foreign) is None
    assert 'output_tokens' not in task_context_profile(task)


def test_declared_and_learned_route_output_caps_survive(monkeypatch):
    from lib.model_info import _route_output_limit_key
    from lib.model_info import _limits
    task, slot = task_and_route(monkeypatch, output_cap=4096)
    key = _route_output_limit_key(provider_id='gateway', offering_id='',
                                  deployment_id='', protocol=slot.protocol or 'openai',
                                  model=slot.model)
    monkeypatch.setitem(_limits._LEARNED_ROUTE_LIMITS, key, 2048)
    assert task_context_profile(task)['output_cap_tokens'] == 2048
    resolve_admission_budget(task, input_tokens=29_030)
    assert task['_contextBudgetDecision']['resolvedOutputTokens'] == 2048
    # Explicit smaller ordinary cap remains a cap, never raised to a floor.
    task['config']['maxTokens'] = 512
    resolve_admission_budget(task, input_tokens=29_030)
    assert task['_contextBudgetDecision']['resolvedOutputTokens'] == 512


def test_over_safety_still_attempts_mandatory_compaction_and_refuses(monkeypatch):
    task, _ = task_and_route(monkeypatch)
    messages = [{'role': 'user', 'content': 'irreducible objective'}]
    original = deepcopy(messages)
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda *a, **k: (100_000, 'test_complete_prompt'))
    calls = []
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: calls.append(k) or False)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.try_pressure_l1',
                        lambda *a, **k: False)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.retain_oversized_tool_batch',
                        lambda *a, **k: False)
    with pytest.raises(ContextCompactionError):
        admission.enforce_dispatch_prompt_limit(
            messages, [], task, round_num=0, model='gpt-5.6-sol')
    assert calls and calls[0]['force'] is True
    assert messages == original


def test_dispatch_reason_explains_route_budget_and_measurement_scope(monkeypatch):
    task, _ = task_and_route(monkeypatch)
    counts = iter([81_307, 40_000])
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda *a, **k: (next(counts), 'test_complete_prompt'))
    monkeypatch.setattr(admission, '_tool_schema_tokens', lambda *a, **k: 20_803)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.try_pressure_l1',
                        lambda *a, **k: False)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.retain_oversized_tool_batch',
                        lambda *a, **k: False)
    calls = []
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: calls.append(k) or True)
    monkeypatch.setattr(admission, 'recompose_context_after_compaction',
                        lambda *a, **k: None)
    admission.enforce_dispatch_prompt_limit(
        [{'role': 'user', 'content': 'Continue the work'}], [{'type': 'function'}],
        task, round_num=50, model='gpt-5.6-sol')
    reason = calls[0]['_compaction_reason']
    for value in ('81,307', '80,640', '60,504', '20,803', '128,000', '47,360'):
        assert value in reason
    assert all(scope in reason.lower() for scope in ('local', 'snapshot', 'estimate'))
    assert task['_lastPromptAdmission']['action'] == 'compact_then_admit'


def test_native_near_window_output_refuses_tool_surface_without_summary(monkeypatch):
    task, _ = task_and_route(monkeypatch)
    metadata = {'owner_user_id': 7, 'context_window': 128_000,
                'provider_id': 'gateway', 'wire_model': 'gpt-5.6-sol',
                'logical_model': 'gpt-5.6-sol'}
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda *a, **k: (29_030, 'test_complete_prompt'))
    monkeypatch.setattr(admission, '_tool_schema_tokens', lambda *a, **k: 13_833)
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: pytest.fail('no summary authority'))
    with native_context_scope(task, metadata, 127_999):
        with pytest.raises(PromptTooLongError):
            admission.enforce_dispatch_prompt_limit(
                [{'role': 'user', 'content': 'objective'}], [{'type': 'function'}],
                task, round_num=0, model='gpt-5.6-sol')
        assert task['_contextBudgetDecision']['resolvedOutputTokens'] == 127_999
