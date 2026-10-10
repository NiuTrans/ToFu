"""Cancellation is a stage boundary, not rollback of already-adopted history."""
from copy import deepcopy

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('stop_at', [
    'entry', 'hooks', 'l1', 'l2', 'recompose', 'analytics', 'advanced',
])
def test_pipeline_starts_no_later_stage_after_owner_stop(monkeypatch, stop_at):
    from lib.tasks_pkg.compaction import _advanced, _pipeline as pipeline
    from lib.tasks_pkg import tool_hooks

    task = {'id': 'pipeline-stop', '_userId': 1, 'aborted': stop_at == 'entry',
            'config': {'compaction': {'advanced_steps': ['synthetic-step']}}}
    messages = [{'role': 'user', 'content': 'Scoped work'}]
    calls = []
    stages = ['hooks', 'l1', 'l2', 'recompose', 'analytics', 'advanced']

    def stage(name, result):
        def run(*args, **kwargs):
            assert not task['aborted'], f'{name} started after owner Stop'
            calls.append(name)
            if name == 'l2':
                messages.append({'role': 'assistant', 'content': 'accepted earlier state'})
            if name == stop_at:
                task['aborted'] = True
            return result
        return run

    monkeypatch.setattr(tool_hooks, 'run_pre_compact_hooks', stage('hooks', None))
    monkeypatch.setattr(pipeline, 'micro_compact', stage('l1', 0))
    monkeypatch.setattr(pipeline, 'force_compact_if_needed', stage('l2', True))
    monkeypatch.setattr(pipeline, 'recompose_context_after_compaction', stage('recompose', None))
    monkeypatch.setattr(pipeline, '_emit_compaction_analytics', stage('analytics', 10))
    monkeypatch.setattr(_advanced, 'advanced_compact', stage('advanced', 0))
    monkeypatch.setattr(pipeline, '_maybe_record_context_budget', stage('diagnostic', None))
    pipeline.run_compaction_pipeline(messages, 4, task)
    expected = [] if stop_at == 'entry' else stages[:stages.index(stop_at) + 1]
    assert calls == expected
    # Stop after adoption cannot rewrite the already accepted projection.
    assert len(messages) == (2 if 'l2' in expected else 1)


@pytest.mark.parametrize('cancel_before_read', [False, True])
def test_composer_does_not_adopt_context_acquired_after_stop(monkeypatch, cancel_before_read):
    import lib.tasks_pkg.context_composer as composer

    task = {'id': 'context-stop', 'aborted': cancel_before_read, 'config': {}}
    messages = [{'role': 'user', 'content': 'Scoped work'}]
    before = deepcopy(messages)
    reads = []

    def collect(*args):
        reads.append('collect')
        task['aborted'] = True
        return [composer.context_block('environment', 'must not be installed')]

    monkeypatch.setattr(composer, 'collect_context_blocks', collect)
    result = composer.compose_context(messages, composer.ComposeRequest(task=task))
    assert messages == before and result.messages is messages
    assert result.manifest == []
    assert reads == ([] if cancel_before_read else ['collect'])


def test_reactive_retry_stops_before_checklist_and_body_when_restoration_is_cancelled(monkeypatch):
    from lib.llm_errors import PromptTooLongError
    from lib.tasks_pkg.compaction import api, _pipeline
    from lib.tasks_pkg.context_composer import checklist
    from lib.tasks_pkg.llm_fallback import _call

    task = {'id': 'stop-restoration', 'aborted': False, 'config': {}}
    messages = [{'role': 'user', 'content': 'Scoped work'}]
    before = deepcopy(messages)
    calls = []
    monkeypatch.setattr(api, 'reactive_compact', lambda *a, **k: True)

    def stop(*args, **kwargs):
        task['aborted'] = True

    monkeypatch.setattr(_pipeline, 'recompose_context_after_compaction', stop)
    monkeypatch.setattr(checklist, 'restore_checklist_context', lambda *a, **k: calls.append('checklist'))
    monkeypatch.setattr(_call, 'build_body', lambda *a, **k: calls.append('body') or {})
    assert _call._prepare_reactive_retry_body(
        task, {}, 'test-model', messages, [], 'medium', False,
        PromptTooLongError('too long'), 'stop-restoration') is None
    assert messages == before and calls == []


def test_registered_steps_stop_between_methods_and_decline_late_summary(monkeypatch):
    from lib.tasks_pkg.compaction import _steps as owner

    task = {'aborted': False}
    calls = []
    ctx = owner.CompactionContext(messages=[], task=task)

    def stop_step(context):
        calls.append('first')
        context.task['aborted'] = True
        return 3

    def forbidden_step(context):
        calls.append('forbidden')
        return 4

    monkeypatch.setitem(owner._STEP_REGISTRY, 'stop-test',
                        owner._StepSpec(stop_step, owner.STEP_KIND_TRANSFORM, ()))
    monkeypatch.setitem(owner._STEP_REGISTRY, 'later-test',
                        owner._StepSpec(forbidden_step, owner.STEP_KIND_TRANSFORM, ()))
    assert owner.run_steps(['stop-test', 'later-test'], ctx) == 3
    assert calls == ['first']
    ctx.summarize_fn = lambda *a, **k: calls.append('paid') or 'new summary'
    assert ctx.summarize('history') == ''
    assert calls == ['first']
    task['aborted'] = False

    def late_summary(*args, **kwargs):
        task['aborted'] = True
        return 'result arriving after Stop'

    ctx.summarize_fn = late_summary
    assert ctx.summarize('history') == ''
