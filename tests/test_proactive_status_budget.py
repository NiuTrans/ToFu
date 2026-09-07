"""Bounded, single-snapshot proactive poll status contracts."""

from __future__ import annotations

from unittest import mock

import pytest


pytestmark = pytest.mark.unit


def _task() -> dict:
    return {
        'id': 'proactive-budget-task',
        'user_id': 7,
        'name': 'Watch the project',
        'command': 'Act when ready',
        'condition_kind': 'llm',
        'target_conv_id': 'conversation-with-long-history',
        'poll_count': 3,
        'execution_count': 0,
    }


def test_status_reads_only_two_tail_messages(monkeypatch):
    from lib.conversations.repository import ConversationSnapshot
    from lib.scheduler import proactive
    from lib.tasks_pkg.manager.runtime import chat_task_runtime
    import lib.conversations.repository as repository

    monkeypatch.setattr(chat_task_runtime, 'snapshot_owned', lambda **_kw: [])
    calls = []

    def get_conversation(conversation_id, **kwargs):
        calls.append((conversation_id, kwargs))
        return ConversationSnapshot(
            metadata={
                'id': conversation_id,
                'title': 'Long conversation',
                'msg_count': 50_000,
            },
            messages=[
                {'role': 'user', 'content': 'tail question'},
                {'role': 'assistant', 'content': 'tail answer'},
            ],
        )

    monkeypatch.setattr(repository, 'get_conversation', get_conversation)

    status = proactive.gather_system_status(_task())

    assert calls == [(
        'conversation-with-long-history',
        {'user_id': 7, 'message_window': 2},
    )]
    assert 'Long conversation" (50000 messages)' in status
    assert '[user] tail question' in status
    assert '[assistant] tail answer' in status


def test_poll_decision_reuses_caller_snapshot(monkeypatch):
    from lib.scheduler import proactive
    import lib.llm_dispatch as llm_dispatch

    gather = mock.Mock(side_effect=AssertionError('must not rebuild status'))
    monkeypatch.setattr(proactive, 'gather_system_status', gather)
    sent = []

    def smart_chat(messages, **_kwargs):
        sent.extend(messages)
        return '{"act": false, "reason": "waiting"}', {'total_tokens': 9}

    monkeypatch.setattr(llm_dispatch, 'smart_chat', smart_chat)

    result = proactive.poll_decision(
        _task(), status_snapshot='ONE AUTHORITATIVE SNAPSHOT')

    assert result == (False, 'waiting', 9)
    gather.assert_not_called()
    assert 'ONE AUTHORITATIVE SNAPSHOT' in sent[1]['content']


def test_manager_builds_one_snapshot_for_llm_poll_and_audit(monkeypatch):
    from lib.scheduler import proactive
    from lib.scheduler.manager import ScheduledTaskManager

    gather = mock.Mock(return_value='ONE SNAPSHOT')
    decide = mock.Mock(return_value=(False, 'waiting', 5))
    record = mock.Mock()
    monkeypatch.setattr(proactive, 'gather_system_status', gather)
    monkeypatch.setattr(proactive, 'poll_decision', decide)
    monkeypatch.setattr(proactive, 'record_poll', record)
    monkeypatch.setattr(proactive, 'should_auto_disable', lambda _task: False)
    monkeypatch.setattr(proactive, 'is_task_executing', lambda _task: False)

    manager = ScheduledTaskManager()
    manager.get_task = lambda *_args, **_kwargs: {'poll_count': 3}
    manager.update_task = lambda *_args, **_kwargs: None

    manager._run_proactive_poll(_task())

    gather.assert_called_once_with(_task())
    decide.assert_called_once_with(
        _task(), status_snapshot='ONE SNAPSHOT')
    assert record.call_args.args[5] == 'ONE SNAPSHOT'
