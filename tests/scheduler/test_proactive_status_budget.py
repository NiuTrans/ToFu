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


def test_auto_disable_expires_aware_and_naive():
    from datetime import datetime, timedelta, timezone

    from lib.scheduler import proactive

    past_naive = (datetime.now() - timedelta(hours=1)).isoformat()
    past_aware = (datetime.now(timezone(timedelta(hours=8)))
                  - timedelta(hours=1)).isoformat()
    future_aware = (datetime.now(timezone(timedelta(hours=8)))
                    + timedelta(hours=1)).isoformat()

    assert proactive.should_auto_disable(
        {**_task(), 'expires_at': past_naive}) is True
    assert proactive.should_auto_disable(
        {**_task(), 'expires_at': past_aware}) is True
    assert proactive.should_auto_disable(
        {**_task(), 'expires_at': future_aware}) is False
    assert proactive.should_auto_disable(_task()) is False

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


@pytest.mark.parametrize('kind', ['hybrid', 'code'])
def test_predicate_polls_decide_and_record_the_predicate_evidence(monkeypatch, kind):
    """A hybrid poll LLM has no tools: unless the snapshot carries the
    predicate's verdict and output, its 'authoritative' answer is blind and
    agreement with the predicate is chance. One evaluation feeds the LLM, the
    reconcile and the ledger."""
    from lib.scheduler import proactive
    from lib.scheduler._shared import PredicateResult, ReconcileOutcome
    from lib.scheduler.manager import ScheduledTaskManager

    result = PredicateResult(matched=True, output='NEW google/example-2', exit_code=0)
    evaluate = mock.Mock(return_value=result)
    decide = mock.Mock(return_value=(True, 'the watch lists a new model', 5))
    reconcile = mock.Mock(return_value=ReconcileOutcome(
        authoritative_ready=False, tier='llm', new_kind=kind, new_streak=1,
        new_fallback_streak=0, predicate_matched=1, llm_agreed=1, note='agrees'))
    record = mock.Mock()
    monkeypatch.setattr(proactive, 'gather_system_status', lambda _task: 'ONE SNAPSHOT')
    monkeypatch.setattr(proactive, 'evaluate_condition_predicate', evaluate)
    monkeypatch.setattr(proactive, 'poll_decision', decide)
    monkeypatch.setattr(proactive, 'apply_reconcile_poll', reconcile)
    monkeypatch.setattr(proactive, 'record_poll', record)
    monkeypatch.setattr(proactive, 'should_auto_disable', lambda _task: False)
    monkeypatch.setattr(proactive, 'is_task_executing', lambda _task: False)
    manager = ScheduledTaskManager()
    manager.get_task = lambda *_args, **_kwargs: {'poll_count': 3}
    manager.update_task = lambda *_args, **_kwargs: None

    manager._run_proactive_poll({**_task(), 'condition_kind': kind,
                                 'condition_command': 'release-watch'})

    evaluate.assert_called_once()
    assert reconcile.call_args.args[1] is result
    snapshot = record.call_args.args[5]
    assert snapshot.startswith('ONE SNAPSHOT')
    assert 'Condition predicate: matched (exit=0)' in snapshot
    assert 'NEW google/example-2' in snapshot
    if kind == 'hybrid':
        assert decide.call_args.kwargs['status_snapshot'] == snapshot
    else:
        decide.assert_not_called()
