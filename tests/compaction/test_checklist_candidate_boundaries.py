"""Checklist projection commits with its context candidate, never before admission."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest, compose_context, context_block, render_context,
)
from lib.tasks_pkg.context_composer.checklist import (
    CHECKLIST_HEADING, build_checklist_context_block,
)
from lib.tools.todo import apply_todo_write_to_task

pytestmark = pytest.mark.unit


def _text(messages):
    return '\n'.join(
        message['content'] if isinstance(message.get('content'), str) else
        ''.join(part.get('text', '') for part in message.get('content') or [])
        for message in messages
    )


def _stale_mixed_carrier():
    task = {'id': 'candidate-checklist', 'convId': 'candidate-checklist',
            '_userId': 7, '_contextManifest': [], 'config': {'model': 'test-model'}}
    apply_todo_write_to_task(task, {'todos': [{'content': 'Obsolete plan', 'status': 'pending'}]})
    carriers = []
    render_context(carriers, [build_checklist_context_block([], task),
                             context_block('modified_files', 'Sibling evidence')],
                   ComposeRequest(task=task))
    mixed = {**carriers[0], 'content': _text(carriers),
             '_contextBlocks': [block for row in carriers for block in row['_contextBlocks']]}
    apply_todo_write_to_task(task, {'todos': [{'content': 'Accepted plan', 'status': 'pending'}],
                                   'explanation': 'Accepted update reason'})
    return task, mixed


def _projection_state(task):
    return deepcopy({key: value for key, value in task.items()
                     if key in ('_todoState', '_todos')
                     or key.startswith('_context') or 'injected' in key.lower()})


@pytest.mark.parametrize('boundary', ['compose', 'append'])
def test_cancelled_composition_does_not_withdraw_stale_carrier(monkeypatch, boundary):
    import lib.tasks_pkg.context_composer as composer

    task, mixed = _stale_mixed_carrier()
    messages = [{'role': 'user', 'content': 'Continue.'}, mixed]
    original = deepcopy(messages)
    state = _projection_state(task)

    def collect(*args):
        task['aborted'] = True
        return [build_checklist_context_block(messages, task)]

    monkeypatch.setattr(composer, 'collect_context_blocks', collect)
    if boundary == 'compose':
        result = compose_context(messages, ComposeRequest(task=task))
    else:
        task['aborted'] = True
        result = composer.append_context_blocks(
            messages, [build_checklist_context_block(messages, task)], ComposeRequest(task=task))
    assert result.manifest == []
    assert messages == original
    assert _projection_state(task) == state


@pytest.mark.parametrize('decision', ['reject', 'cancel'])
def test_l2_candidate_checklist_cleanup_never_mutates_rejected_live_prefix(monkeypatch, decision):
    from lib.tasks_pkg.compaction._layer2 import _compact as layer2
    from lib.tasks_pkg.context_composer import checklist

    task, mixed = _stale_mixed_carrier()
    messages = [
        {'role': 'system', 'content': 'Preserve user intent.'},
        {'role': 'user', 'content': 'Investigate the failure.'},
        {'role': 'assistant', 'content': 'Old evidence ' * 8000},
        {'role': 'user', 'content': 'Continue validation.'},
        mixed,
        {'role': 'assistant', 'content': 'Recent evidence ' * 2000},
    ]
    original = deepcopy(messages)
    state = _projection_state(task)
    candidates = []
    real_carry = checklist.carry_checklist_through_compaction

    def carry(candidate, candidate_task, *args, **kwargs):
        assert any(row is mixed for row in candidate), 'exercise a shared retained carrier'
        real_carry(candidate, candidate_task, *args, **kwargs)
        assert 'Obsolete plan' not in _text(candidate)
        assert 'Sibling evidence' in _text(candidate)
        assert _text(candidate).count(CHECKLIST_HEADING) == 1
        assert build_checklist_context_block(candidate, task) is None
        candidates.append(deepcopy(candidate))
        if decision == 'cancel':
            task['aborted'] = True

    monkeypatch.setattr(checklist, 'carry_checklist_through_compaction', carry)
    monkeypatch.setattr(layer2, '_generate_query_aware_summary', lambda *a, **k: 'Faithful summary')
    economics_calls = []
    def economics(*args, **kwargs):
        economics_calls.append(kwargs)
        # Permit summary generation, then reject the fully rendered candidate.
        return {'dropped_tokens': kwargs['tokens_before'] if len(economics_calls) == 1 else 0,
                'cache_read_tokens': 0, 'payback_rounds': 0}
    monkeypatch.setattr(layer2, '_proactive_cache_economics', economics)
    monkeypatch.setattr(layer2, '_archive_transcript',
                        lambda *a, **k: pytest.fail('unaccepted candidate archived'))
    meta = {}
    layer2.execute_compact_tool(
        messages, task=task, preserve_budget_tokens=10000,
        _proactive_economic=decision == 'reject', _result_meta=meta,
    )
    assert len(candidates) == 1
    assert not meta.get('compacted')
    assert messages == original
    assert _projection_state(task) == state


def test_pressure_measures_carried_checklist_before_rejecting_candidate(monkeypatch):
    from lib.tasks_pkg.compaction import _steps
    from lib.tasks_pkg.compaction._pressure import try_pressure_l1

    task, mixed = _stale_mixed_carrier()
    messages = [{'role': 'user', 'content': 'Continue.'}, mixed,
                {'role': 'assistant', 'content': '', 'tool_calls': [
                    {'id': 'read-1', 'type': 'function',
                     'function': {'name': 'read_files', 'arguments': '{}'}}]},
                {'role': 'tool', 'tool_call_id': 'read-1', 'name': 'read_files',
                 'content': 'Old evidence ' * 1000}]
    original = deepcopy(messages)
    state = _projection_state(task)

    def compact(steps, context, **kwargs):
        context.messages[-1] = {**context.messages[-1], 'content': 'Compacted evidence'}
        return 1000

    measured = []
    def measure(candidate):
        text = _text(candidate)
        assert 'Accepted plan' in text and 'Obsolete plan' not in text
        assert 'Sibling evidence' in text
        assert build_checklist_context_block(candidate, task) is None
        measured.append(len(text))
        return len(text)

    monkeypatch.setattr(_steps, 'run_steps', compact)
    assert not try_pressure_l1(messages, task, target_tokens=1, total_tokens=20000,
                               measure=measure, mandatory=True)
    assert len(measured) == 1
    assert task['_lastPressureL1']['candidateTokens'] == measured[0]
    assert messages == original
    assert _projection_state(task) == state
