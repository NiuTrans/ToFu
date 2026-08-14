"""Contracts for the shared chat-task error terminal boundary."""

import pytest

from lib.tasks_pkg.manager._terminal import (
    finalize_chat_task_error,
    stamp_chat_task_terminal,
)


pytestmark = pytest.mark.unit


def test_error_terminal_updates_state_event_persistence_and_busy_projection():
    events = []
    persisted = []
    notified = []
    task = {
        'id': 'terminal-task-0001',
        'status': 'running',
        'endpoint_mode': True,
        '_endpoint_phase': 'working',
        'model': 'test-model',
    }
    envelope = {'kind': 'bad_request', 'message': 'missing flow'}

    event = finalize_chat_task_error(
        task,
        envelope,
        endpoint_reason='definition_unavailable',
        append_event_fn=lambda owner, item: events.append((owner, item)),
        persist_task_result_fn=lambda owner: persisted.append(owner),
        notify_terminal_fn=lambda owner: notified.append(owner),
    )

    assert task['status'] == 'error'
    assert task['finishReason'] == 'error'
    assert task['_endpoint_phase'] == 'done'
    assert task['_endpoint_stop_reason'] == 'definition_unavailable'
    assert task['finished_at'] > 0
    assert event['type'] == 'done'
    assert event['finishReason'] == 'error'
    assert event['error'] is envelope
    assert events == [(task, event)]
    assert persisted == [task]
    assert notified == [task]


def test_error_terminal_attempts_persistence_when_event_delivery_fails():
    persisted = []
    notified = []

    def fail_event(_task, _event):
        raise RuntimeError('push unavailable')

    finalize_chat_task_error(
        {'id': 'terminal-task-0002', 'status': 'running'},
        {'kind': 'internal', 'message': 'failure'},
        append_event_fn=fail_event,
        persist_task_result_fn=lambda owner: persisted.append(owner),
        notify_terminal_fn=lambda owner: notified.append(owner),
    )

    assert len(persisted) == 1
    assert notified == persisted


def test_terminal_stamp_is_idempotent_and_rejects_outcome_rewrites():
    task = {'id': 'terminal-task-0003', 'status': 'running'}

    assert stamp_chat_task_terminal(
        task, status='done', finish_reason='stop',
        endpoint_reason='verified_complete',
    ) is True
    finished_at = task['finished_at']
    assert stamp_chat_task_terminal(
        task, status='done', finish_reason='stop',
        endpoint_reason='verified_complete',
    ) is False
    assert stamp_chat_task_terminal(
        task, status='error', finish_reason='error', endpoint_reason='fatal',
    ) is False

    assert task['status'] == 'done'
    assert task['finishReason'] == 'stop'
    assert task['_endpoint_stop_reason'] == 'verified_complete'
    assert task['finished_at'] == finished_at


def test_error_finalizer_emits_only_once():
    events = []
    persisted = []
    task = {'id': 'terminal-task-0004', 'status': 'running'}
    kwargs = {
        'append_event_fn': lambda owner, item: events.append((owner, item)),
        'persist_task_result_fn': lambda owner: persisted.append(owner),
        'notify_terminal_fn': lambda _owner: None,
    }

    assert finalize_chat_task_error(
        task, {'kind': 'internal', 'message': 'first'}, **kwargs,
    ) is not None
    assert finalize_chat_task_error(
        task, {'kind': 'internal', 'message': 'duplicate'}, **kwargs,
    ) is None
    assert len(events) == 1
    assert persisted == [task]
    assert task['error']['message'] == 'first'
