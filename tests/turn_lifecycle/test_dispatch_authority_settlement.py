"""Durable Turn settlement for project-dispatch authority stops.

The grant-withdrawal cancel port (``abort_project_attempt``) records the
revoked-grant cause in the settled Turn; an ordinary human Stop of the same
kind of attempt still settles ``interrupted/user_abort``.
"""
from __future__ import annotations

import pytest

from tests.storage.test_storage_application_contract import (
    application_storage as application_storage,
    application_storage_owner_id as application_storage_owner_id,
)
from tests.turn_lifecycle.test_turn_lifecycle import (
    _create, _new_conversation_id, _running_task,
)

# Keep the imported fixtures' registered aliases visible to static tooling.
__all__ = ('application_storage', 'application_storage_owner_id')

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)


@pytest.fixture(autouse=True)
def _cleanup_runtime_tasks():
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    before = set(chat_task_runtime.task_ids())
    yield
    for task_id in set(chat_task_runtime.task_ids()) - before:
        chat_task_runtime.discard(task_id)


def _commands():
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    return ConversationTurnCommandService(
        build_user_message=lambda message, *a: message, was_aborted_after=lambda *a: False,
        start_task=lambda *a, **kw: None)


def _bound_attempt(prefix):
    from lib.turn_lifecycle import bind_task, record_task_event
    conversation_id = _new_conversation_id(prefix)
    created = _create(conversation_id)
    attempt_id = created['attempt']['attemptId']
    task_id = f'{prefix}-task'
    bind_task(attempt_id, task_id, user_id=1)
    assert record_task_event(_running_task(attempt_id, task_id), {'type': 'tool_result'}) is True
    return conversation_id, created['turn']['turnId'], attempt_id, task_id


def _settled(conversation_id, turn_id):
    from lib.turn_lifecycle import get_turn
    turn = get_turn(conversation_id, turn_id, user_id=1)
    return turn['status'], turn['settlement']


def test_revoked_grant_without_a_live_executor_settles_grant_revoked(chat_sidecar):
    conversation_id, turn_id, attempt_id, _task_id = _bound_attempt('revoked-lost')
    _commands().abort_project_attempt(attempt_id, 1)
    status, settlement = _settled(conversation_id, turn_id)
    assert status == 'failed'
    assert (settlement['outcome'], settlement['cause'], settlement['evidence']) == (
        'failed', 'dispatch_grant_revoked', 'external_authority')
    assert settlement['error']['kind'] == 'dispatch_grant_revoked'
    assert settlement['error']['retryable'] is False


@pytest.mark.api
def test_revoked_grant_settlement_is_durable_on_every_authority(application_storage):
    conversation_id, turn_id, attempt_id, _task_id = _bound_attempt('revoked-authority')
    assert _commands().abort_project_attempt(attempt_id, 1).value['status'] == 'abort_signaled'
    status, settlement = _settled(conversation_id, turn_id)
    assert status == 'failed'
    assert (settlement['outcome'], settlement['cause'], settlement['evidence']) == (
        'failed', 'dispatch_grant_revoked', 'external_authority')
    assert settlement['error']['kind'] == 'dispatch_grant_revoked'
    assert settlement['error']['retryable'] is False


def test_revoked_grant_of_a_live_executor_settles_grant_revoked(chat_sidecar):
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
    from lib.turn_lifecycle import record_task_event
    conversation_id, turn_id, attempt_id, task_id = _bound_attempt('revoked-live')
    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    chat_task_runtime.mark_running(task_id)
    task.update(_running_task(attempt_id, task_id))
    assert _commands().abort_project_attempt(attempt_id, 1).value['status'] == 'abort_signaled'
    assert task['aborted'] is True and task['_abort_reason'] == 'dispatch_grant_revoked'
    finish = _settle_post_loop_finish_reason(
        task, 'stop', loop_exit_reason='user_abort', abort_detected_phase='llm_round_0',
        model='gpt-4o', tid=task_id)
    task.update(status='error', finishReason=finish)
    assert record_task_event(task, {'type': 'done', 'finishReason': finish,
                                    'error': task['error']}) is True
    status, settlement = _settled(conversation_id, turn_id)
    assert status == 'failed' and settlement['cause'] == 'dispatch_grant_revoked'
    assert settlement['evidence'] == 'external_authority'
    assert settlement['error']['kind'] == 'dispatch_grant_revoked'


def test_human_stop_of_a_live_executor_keeps_user_abort(chat_sidecar):
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    from lib.tasks_pkg.orchestrator._finalize import _settle_post_loop_finish_reason
    from lib.turn_lifecycle import record_task_event
    conversation_id, turn_id, attempt_id, task_id = _bound_attempt('human-stop')
    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    chat_task_runtime.mark_running(task_id)
    task.update(_running_task(attempt_id, task_id))
    assert _commands().abort_attempt(attempt_id, 1).value['status'] == 'abort_signaled'
    # The grant-withdrawal port arriving after the Stop cannot rename its cause.
    _commands().abort_project_attempt(attempt_id, 1)
    assert task['aborted'] is True and '_abort_reason' not in task
    finish = _settle_post_loop_finish_reason(
        task, 'stop', loop_exit_reason='user_abort', abort_detected_phase='llm_round_0',
        model='gpt-4o', tid=task_id)
    assert finish == 'aborted'
    task.update(status='aborted', finishReason=finish)
    assert record_task_event(task, {'type': 'done', 'finishReason': finish}) is True
    status, settlement = _settled(conversation_id, turn_id)
    assert status == 'interrupted' and settlement['cause'] == 'user_abort'
    assert settlement['error'] is None
