"""Completion effects belong to the shared terminal claim after resource release."""

import pytest

from lib.agent_core.execution_session import ExecutionPhase
from lib.agent_core import task_runtime


pytestmark = pytest.mark.unit


def running(*, persist=None):
    runtime = task_runtime.TaskRuntime('completion-commit', terminal_persist=persist)
    task = runtime.create(user_id=7)
    runtime.mark_running(task['id'])
    return runtime, task


def test_completion_releases_resources_then_commits_before_terminal_visibility():
    runtime, task = running()
    calls = []
    task['_executionSession'].hold_resource('route', lambda _: calls.append('release'))

    def commit():
        assert calls == ['release']
        assert task['_executionSession'].phase is ExecutionPhase.SETTLING
        assert runtime.get_owned(task['id'], user_id=7)['status'] == 'running'
        assert runtime.abort_owned(task['id'], user_id=7) is False
        assert runtime.finish(task['id'], error='competing finish') is False
        calls.append('commit')
        return task_runtime.TaskCompletion('stored', {'receipt': 'artifact', 'type': 'spoofed'})

    assert runtime.finish(task['id'], commit_result=commit)
    assert calls == ['release', 'commit']
    assert task['status'] == 'done' and task['result'] == 'stored'
    assert task['_executionSession'].phase is ExecutionPhase.COMPLETED
    assert task['events'][-1]['type'] == 'done'
    assert task['events'][-1]['receipt'] == 'artifact'
    assert runtime.finish(task['id'], commit_result=commit) is False
    assert calls == ['release', 'commit']


@pytest.mark.parametrize('failure', [
    'abort', 'error', 'cleanup_failed', 'cancelled', 'failed', 'timed_out',
])
def test_non_successful_execution_never_runs_completion_effects(failure):
    runtime, task = running()
    session = task['_executionSession']
    if failure == 'abort':
        runtime.abort_owned(task['id'], user_id=7)
    elif failure == 'cleanup_failed':
        def refuse(_):
            raise OSError('cannot release resource')
        session.hold_resource('route', refuse)
    elif failure in ('cancelled', 'failed', 'timed_out'):
        session.settle(ExecutionPhase(failure))

    def forbidden():
        raise AssertionError('failed execution reached publication')

    assert runtime.finish(task['id'], error='failed work' if failure == 'error' else None,
                          commit_result=forbidden, terminal_event_fields={'partial': 'retained'})
    expected = 'aborted' if failure in ('abort', 'cancelled', 'timed_out') else 'error'
    assert task['status'] == expected and task['result'] is None
    assert task['events'][-1]['type'] == expected
    assert task['events'][-1]['partial'] == 'retained'


@pytest.mark.parametrize('error_type', [ValueError, OSError, KeyboardInterrupt])
def test_commit_failure_preserves_exception_and_releases_terminal_claim(error_type):
    runtime, task = running()
    released, commits = [], []
    task['_executionSession'].hold_resource('route', lambda _: released.append('route'))
    failure = error_type('publication failed')

    def commit():
        commits.append('attempt')
        raise failure

    with pytest.raises(error_type) as caught:
        runtime.finish(task['id'], commit_result=commit)
    assert caught.value is failure
    assert task['_executionSession'].phase is ExecutionPhase.FAILED
    assert task['status'] == 'running' and not task['_executionTerminalizing']
    assert not task['events']
    assert runtime.finish(task['id'], error='publication failed', commit_result=commit)
    assert task['status'] == 'error'
    assert released == ['route'] and commits == ['attempt']


def test_invalid_completion_is_an_execution_failure_not_a_false_success():
    runtime, task = running()
    with pytest.raises(TypeError, match='TaskCompletion'):
        runtime.finish(task['id'], commit_result=lambda: {'result': 'not a completion'})
    assert task['_executionSession'].phase is ExecutionPhase.FAILED
    assert not task['_executionTerminalizing'] and not task['events']
    assert runtime.finish(task['id'], error='invalid completion')
    assert task['status'] == 'error'


def test_terminal_persistence_retry_does_not_repeat_completion_effects():
    attempts, commits = [], []

    def persist(task, candidate):
        attempts.append(candidate)
        return False if len(attempts) == 1 else None

    runtime, task = running(persist=persist)

    def commit():
        commits.append('stored')
        return task_runtime.TaskCompletion('artifact', {'receipt': 'original'})

    with pytest.raises(RuntimeError, match='not acknowledged'):
        runtime.finish(task['id'], commit_result=commit)
    assert task['_terminalPersistencePending']
    assert runtime.finish(task['id'], commit_result=commit)
    assert commits == ['stored'] and attempts[0] == attempts[1]
    assert task['result'] == 'artifact'
    assert task['events'][-1]['receipt'] == 'original'


def test_a_sealed_execution_cannot_replay_an_unretained_completion():
    runtime, task = running()
    task['_executionSession'].settle(ExecutionPhase.COMPLETED)
    calls = []
    with pytest.raises(RuntimeError, match='already settled'):
        runtime.finish(task['id'], commit_result=lambda: calls.append('unexpected'))
    assert not calls and not task['_executionTerminalizing']
    assert not task['events']
    assert runtime.finish(task['id'], error='completion was not retained')


@pytest.mark.parametrize('raises', [False, True])
def test_legacy_task_carrier_does_not_swallow_completion_value_errors(raises):
    runtime, task = running()
    session = task.pop('_executionSession')
    session.settle(ExecutionPhase.CANCELLED)
    calls = []
    failure = ValueError('bad publication')

    def commit():
        calls.append('commit')
        if raises:
            raise failure
        return task_runtime.TaskCompletion('legacy artifact', {})

    if raises:
        with pytest.raises(ValueError) as caught:
            runtime.finish(task['id'], commit_result=commit)
        assert caught.value is failure
        assert runtime.finish(task['id'], error=failure)
        assert task['status'] == 'error'
    else:
        assert runtime.finish(task['id'], commit_result=commit)
        assert task['result'] == 'legacy artifact'
    assert calls == ['commit'] and not task['_executionTerminalizing']
