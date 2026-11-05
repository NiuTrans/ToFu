"""Report test contexts retire tasks even if their caller never runs the worker."""

import pytest

from lib.agent_core.execution_session import execution_session_for_task
from tests.support.paper_report import isolated_report_case


pytestmark = pytest.mark.unit


@pytest.mark.parametrize('caller_failed', [False, True])
def test_report_context_retires_unrun_execution(monkeypatch, caller_failed):
    runtime = task = None
    try:
        try:
            with isolated_report_case(monkeypatch) as (_, task, worker):
                runtime = worker._report_runtime
                session = execution_session_for_task(task)
                assert runtime.get_owned(task['task_id'], user_id=17) is task
                assert not session.is_terminal
                if caller_failed:
                    raise RuntimeError('caller failed before worker dispatch')
        except RuntimeError as exc:
            assert caller_failed and str(exc) == 'caller failed before worker dispatch'
        assert session.is_terminal
        assert runtime.get_owned(task['task_id'], user_id=17) is None
    finally:
        # Keep the failing specification itself isolated before the fix lands.
        if runtime is not None and task is not None:
            runtime.discard(task['task_id'])


def test_report_contexts_have_independent_execution_identity(monkeypatch):
    with isolated_report_case(monkeypatch) as (_, first, worker):
        first_runtime = worker._report_runtime
        first_session = execution_session_for_task(first)
        first_repository = worker.PaperArtifactRepository
        with monkeypatch.context() as nested_patch:
            with isolated_report_case(nested_patch) as (_, second, worker):
                second_runtime = worker._report_runtime
                second_session = execution_session_for_task(second)
                assert first['task_id'] != second['task_id']
                assert not first_session.is_terminal and not second_session.is_terminal
        assert second_session.is_terminal
        assert second_runtime.get_owned(second['task_id'], user_id=17) is None
        assert not first_session.is_terminal
        assert worker._report_runtime is first_runtime
        assert worker.PaperArtifactRepository is first_repository
    assert first_session.is_terminal
    assert first_runtime.get_owned(first['task_id'], user_id=17) is None


def test_report_context_preserves_a_completed_receipt(monkeypatch):
    with isolated_report_case(monkeypatch) as (_, task, worker):
        runtime = worker._report_runtime
        assert runtime.finish(task['task_id'], result='already completed')
        session = execution_session_for_task(task)
        phase = session.phase
    assert task['status'] == 'done'
    assert task['result'] == 'already completed'
    assert session.is_terminal and session.phase == phase
    assert runtime.get_owned(task['task_id'], user_id=17) is None


def test_report_fixture_and_caller_overrides_share_one_patch_lifetime():
    from lib.paper.report_engine import worker

    original = worker.PaperArtifactRepository
    try:
        with pytest.MonkeyPatch.context() as patch:
            with isolated_report_case(patch) as (_, _, current):
                patch.setattr(current, 'PaperArtifactRepository', lambda owner: None)
        assert worker.PaperArtifactRepository is original
    finally:
        # Retire the independently reproduced leak even before its fix lands.
        worker.PaperArtifactRepository = original
