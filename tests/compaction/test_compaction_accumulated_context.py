"""Long-lived agents must not turn superseded snapshots or read inboxes into floors."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._layer2 import _compact as compact
from lib.tasks_pkg.context_composer import ComposeRequest, context_block
from lib.tasks_pkg.context_composer._render import render_context
from lib.tasks_pkg.invocation import InvocationPolicy

pytestmark = pytest.mark.unit


def execution(index):
    return [
        {'role': 'assistant', 'content': '', 'tool_calls': [{
            'id': f'call-{index}', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': f'call-{index}', 'content': f'evidence-{index}'},
    ]


def text(message):
    value = message.get('content') or ''
    return value if isinstance(value, str) else ''.join(p.get('text', '') for p in value)


def count(messages):
    return 16000 + sum(len(text(message)) for message in messages)


def owner(kind):
    task = {'id': 'compact-owner', '_userId': 7, 'convId': 'conversation',
            'config': {'model': 'gpt-6-astra'}}
    if kind == 'child':
        task['_invocationPolicy'] = InvocationPolicy.child('parent')
    elif kind == 'transient':
        task.update(convId='', _transientRuntime=True)
    elif kind == 'storage_free_child':
        task.update(convId='', _invocationStorageFree=True,
                    _invocationPolicy=InvocationPolicy.child('parent'))
    return task


def run(messages, task, monkeypatch, *, target=80640, summary='complete', calls=None):
    def summarize(cold, *args, **kwargs):
        if calls is not None:
            calls.append(deepcopy(cold))
        return summary
    monkeypatch.setattr(compact, '_generate_query_aware_summary', summarize)
    monkeypatch.setattr(compact, '_archive_transcript', lambda *a, **k: None)
    meta = {}
    result = compact.force_compact_if_needed(
        messages, task=task, force=True, preserve_budget_tokens=16000,
        _compaction_trigger='dispatch_guard', _dispatch_prompt_target=target,
        _dispatch_prompt_measure=count, _result_meta=meta)
    return result, meta


@pytest.mark.parametrize('kind', ['root', 'child', 'transient', 'storage_free_child'])
def test_updated_project_snapshot_does_not_create_false_floor(monkeypatch, kind):
    messages = [{'role': 'system', 'content': 'Required policy'},
                {'role': 'user', 'content': 'Investigate the repository'}]
    render_context(messages, [context_block('project_scoped_rules', 'OLD ' * 9000)], ComposeRequest())
    messages.extend(execution(0))
    render_context(messages, [context_block('project_scoped_rules', 'CURRENT ' * 5000)], ComposeRequest())
    current = deepcopy(messages[-1])
    messages.extend(execution(1))
    original = deepcopy(messages)
    calls = []
    adopted, meta = run(messages, owner(kind), monkeypatch, calls=calls)
    assert adopted, meta
    assert len(calls) == 1
    assert count(messages) <= 80640
    assert current in messages
    assert not any('OLD OLD' in text(message) for message in messages)
    assert messages[0] == original[0]
    assert all(m in messages for m in original[-2:])


@pytest.mark.parametrize('kind', ['root', 'child', 'transient', 'storage_free_child'])
def test_failed_summary_preserves_snapshots_and_execution(monkeypatch, kind):
    messages = [{'role': 'system', 'content': 'Required policy'},
                {'role': 'user', 'content': 'Investigate'}]
    for i in range(3):
        render_context(messages, [context_block('project_scoped_rules', f'policy-{i}')], ComposeRequest())
        messages.extend(execution(i))
    original = deepcopy(messages)
    adopted, _ = run(messages, owner(kind), monkeypatch, summary='')
    assert not adopted
    assert messages == original


def test_consumed_agent_inbox_enters_summary_but_unread_and_human_steer_survive(monkeypatch):
    messages = [{'role': 'system', 'content': 'Required policy'},
                {'role': 'user', 'content': 'Investigate'}]
    for i in range(8):
        messages.append({'role': 'user', '_isInboxInject': True,
                         '_containsHumanSteer': False, 'content': f'agent-{i} ' * 1800})
        messages.extend(execution(i))
    human = {'role': 'user', '_isInboxInject': True, '_containsHumanSteer': True,
             'content': 'Only inspect the staging deployment'}
    unread = {'role': 'user', '_isInboxInject': True, '_containsHumanSteer': False,
              'content': 'New agent evidence that has not reached the model'}
    messages.extend([human, unread])
    original = deepcopy(messages)
    calls = []
    adopted, meta = run(messages, owner('root'), monkeypatch, target=50000, calls=calls)
    assert adopted, meta
    assert len(calls) == 1
    assert any('agent-0 agent-0' in text(message) for message in calls[0])
    from lib.tasks_pkg.compaction._layer2._prompt import _format_messages_for_summary
    summary_source = _format_messages_for_summary(calls[0])
    assert '[context] agent-0' in summary_source
    assert '[user] agent-0' not in summary_source
    assert not any('agent-0 agent-0' in text(message) for message in messages)
    assert human in messages and unread in messages
    assert all(m in messages for m in original[-4:-2])  # newest complete execution
    assert count(messages) <= 50000


def test_child_without_conversation_still_has_no_lossy_summary_recovery():
    from lib.tasks_pkg.compaction._summary_budget import bounded_summary_task
    assert bounded_summary_task(owner('storage_free_child'))


@pytest.mark.parametrize('last_summary', ['complete', 'unavailable', 'oversized', 'cancelled'])
def test_repeated_dispatch_compaction_preserves_continuity_and_rejected_source(
        monkeypatch, last_summary):
    from lib.llm.llm_errors import ContextCompactionError
    from lib.tasks_pkg.compaction import _prompt_admission as admission

    task = owner('storage_free_child')
    task['config']['maxTokens'] = 4096
    policy = {'role': 'system', 'content': 'Do not discard required evidence.'}
    objective = {'role': 'user', 'content': 'Finish the original objective.'}
    messages = [policy.copy(), objective.copy()]
    calls = []

    def summarize(cold, *args, **kwargs):
        calls.append(deepcopy(cold))
        if len(calls) == 4:
            if last_summary == 'unavailable':
                return ''
            if last_summary == 'oversized':
                return 'Unbounded summary ' * 10000
            if last_summary == 'cancelled':
                task['aborted'] = True
                return ''
        return f'### Objective\nFinish the original objective.\n### Progress\ncheckpoint-state-{len(calls)}'

    monkeypatch.setattr('lib.llm_dispatch.context_policy.task_context_profile',
                        lambda task: {'window': 128000, 'source': 'test_route'})
    monkeypatch.setattr(admission, '_count_tokens_authoritative',
                        lambda messages, *a, **k: (count(messages), 'test_complete_prompt'))
    monkeypatch.setattr(admission, '_tool_schema_tokens', lambda *a, **k: 16000)
    # Exercise semantic compaction on every pressure cycle, independently of
    # whether a reversible L1 trial could have made this synthetic text fit.
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.try_pressure_l1',
                        lambda *a, **k: False)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.retain_oversized_tool_batch',
                        lambda *a, **k: False)
    monkeypatch.setattr(admission, 'recompose_context_after_compaction', lambda *a, **k: None)
    monkeypatch.setattr(compact, '_generate_query_aware_summary', summarize)
    monkeypatch.setattr(compact, '_archive_transcript', lambda *a, **k: None)

    for cycle in range(4):
        for index in range(cycle * 12, (cycle + 1) * 12):
            messages.append({'role': 'assistant', 'content': f'finding-{index} ' * 900})
            messages.extend(execution(index))
        source, latest_batch = deepcopy(messages), deepcopy(messages[-2:])
        if cycle == 3 and last_summary != 'complete':
            with pytest.raises(ContextCompactionError):
                admission.enforce_dispatch_prompt_limit(
                    messages, [], task, round_num=(cycle + 1) * 12, model='gpt-6-astra')
            assert messages == source
        else:
            result = admission.enforce_dispatch_prompt_limit(
                messages, [], task, round_num=(cycle + 1) * 12, model='gpt-6-astra')
            assert result['totalTokens'] <= 80640
            assert task['_lastPromptAdmission']['action'] == 'compact_then_admit'
            assert any(f'checkpoint-state-{cycle + 1}' in text(m) for m in messages)
        assert len(calls) == cycle + 1  # One summary attempt per pressure cycle.
        if cycle:
            assert any(f'checkpoint-state-{cycle}' in text(m) for m in calls[-1])
        assert policy in messages and objective in messages
        assert all(message in messages for message in latest_batch)
        call_ids = {call['id'] for message in messages for call in message.get('tool_calls', [])}
        result_ids = {m['tool_call_id'] for m in messages if m.get('role') == 'tool'}
        assert call_ids == result_ids


@pytest.mark.parametrize('kind', ['root', 'child', 'transient', 'storage_free_child'])
def test_snapshot_only_pressure_needs_no_summary_or_tool_history(monkeypatch, kind):
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    task = owner(kind)
    task['config']['maxTokens'] = 4096
    messages = [{'role': 'user', 'content': 'Investigate'}]
    render_context(messages, [context_block('project_scoped_rules', 'OLD ' * 9000)], ComposeRequest())
    render_context(messages, [context_block('project_scoped_rules', 'CURRENT ' * 5000)], ComposeRequest())
    current = deepcopy(messages[-1])
    monkeypatch.setattr('lib.llm_dispatch.context_policy.task_context_profile',
                        lambda task: {'window': 128000, 'source': 'test_route'})
    monkeypatch.setattr(admission, '_measure', lambda messages, *a, **k: {
        'totalTokens': count(messages), 'messageTokens': count(messages) - 16000,
        'toolSchemaTokens': 16000, 'method': 'test', 'messageCount': len(messages)})
    monkeypatch.setattr(admission, 'force_compact_if_needed',
                        lambda *a, **k: pytest.fail('Superseded snapshots need no paid summary'))
    result = admission.enforce_dispatch_prompt_limit(
        messages, [], task, round_num=5, model='gpt-6-astra')
    assert result['totalTokens'] <= 80640
    assert current in messages
    assert not any('OLD OLD' in text(message) for message in messages)
    assert task['_lastPromptAdmission']['action'] == 'prune_then_admit'


@pytest.mark.parametrize('rejection', ['insufficient', 'cache_negative', 'cancelled'])
def test_rejected_snapshot_pressure_does_not_modify_source(monkeypatch, rejection):
    from lib.tasks_pkg.compaction._pressure import try_pressure_l1
    task = owner('root')
    messages = [{'role': 'user', 'content': 'Investigate'}]
    render_context(messages, [context_block('project_scoped_rules', 'OLD ' * 9000)], ComposeRequest())
    render_context(messages, [context_block('project_scoped_rules', 'CURRENT ' * 5000)], ComposeRequest())
    original = deepcopy(messages)
    monkeypatch.setattr('lib.tasks_pkg.compaction._cache_economics._proactive_cache_economics',
                        lambda *a, **k: {'cache_read_tokens': 50000, 'payback_rounds': 1000})
    if rejection == 'cancelled':
        task['aborted'] = True
    assert not try_pressure_l1(
        messages, task, target_tokens=1000 if rejection == 'insufficient' else 80640,
        total_tokens=count(messages), measure=count,
        mandatory=rejection != 'cache_negative')
    assert messages == original


@pytest.mark.parametrize('pressure', ['provider', 'memory'])
def test_storage_free_child_reactive_failure_keeps_full_source(monkeypatch, pressure):
    from lib.tasks_pkg.compaction import _reactive as reactive
    task = owner('storage_free_child')
    messages = [{'role': 'system', 'content': 'Required policy'},
                {'role': 'user', 'content': 'Investigate'}]
    for i in range(4):
        messages.extend(execution(i))
        messages[-1]['content'] = 'large evidence ' * 20000
    original = deepcopy(messages)
    monkeypatch.setattr(reactive, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr(reactive, 'force_compact_if_needed', lambda *a, **k: False)
    assert not reactive.reactive_compact(
        messages, task, error_text='prompt is too long: 200000 tokens',
        byte_target=1024 if pressure == 'memory' else None)
    assert messages == original
