"""Autopilot run resolvers use bounded reads with exact full fallback."""

from __future__ import annotations

import pytest

from lib.conversations.repository import ConversationSnapshot
from lib.tasks_pkg import autopilot_state


pytestmark = pytest.mark.unit


def _snapshot(*, messages=(), settings=None, total=None):
    projected = [dict(message) for message in messages]
    return ConversationSnapshot(
        metadata={
            'settings': dict(settings or {}),
            'msg_count': len(projected) if total is None else total,
        },
        messages=projected,
    )


def _install_reads(monkeypatch, snapshots):
    import lib.conversations.repository as repository

    queued = list(snapshots)
    calls = []

    def get_conversation(conversation_id, **kwargs):
        calls.append((conversation_id, dict(kwargs)))
        return queued.pop(0)

    monkeypatch.setattr(repository, 'get_conversation', get_conversation)
    return calls


def test_recent_run_pin_uses_one_metadata_read(monkeypatch):
    calls = _install_reads(monkeypatch, [
        _snapshot(settings={'autopilotRunId': 'run-pinned'}, total=50_000),
    ])

    result = autopilot_state._resolve_recent_run_id('conv', user_id=7)

    assert result == 'run-pinned'
    assert calls == [('conv', {'user_id': 7, 'include_messages': False})]


def test_recent_run_tail_hit_avoids_full_fallback(monkeypatch):
    calls = _install_reads(monkeypatch, [
        _snapshot(total=50_000),
        _snapshot(
            messages=[
                {'_autopilotRunId': 'run-recent'},
                {'role': 'assistant', 'content': 'follow-up'},
            ],
            total=50_000,
        ),
    ])

    result = autopilot_state._resolve_recent_run_id('conv', user_id=7)

    assert result == 'run-recent'
    assert calls[1] == ('conv', {
        'user_id': 7,
        'message_window': 128,
    })
    assert len(calls) == 2


def test_recent_run_missing_tail_uses_exact_full_fallback(monkeypatch):
    calls = _install_reads(monkeypatch, [
        _snapshot(total=300),
        _snapshot(messages=[{'role': 'assistant'}], total=300),
        _snapshot(messages=[
            {'_autopilotRunId': 'run-old'},
            {'role': 'assistant'},
        ], total=300),
    ])

    result = autopilot_state._resolve_recent_run_id('conv', user_id=7)

    assert result == 'run-old'
    assert calls[-1] == ('conv', {'user_id': 7})


def test_anchor_tail_hit_is_complete_through_followups(monkeypatch):
    calls = _install_reads(monkeypatch, [
        _snapshot(messages=[
            {'_autopilotRunId': 'run', '_turnId': 'vu'},
            {'role': 'assistant', '_turnId': 'follow-up'},
        ], total=10_000),
    ])

    result = autopilot_state._resolve_run_anchor_turn_id(
        'conv', 'run', user_id=7)

    assert result == 'follow-up'
    assert len(calls) == 1
    assert calls[0][1]['message_window'] == 128


def test_anchor_missing_tail_falls_back_but_found_empty_anchor_does_not(
        monkeypatch):
    calls = _install_reads(monkeypatch, [
        _snapshot(messages=[{'_autopilotRunId': 'newer'}], total=300),
        _snapshot(messages=[
            {'_autopilotRunId': 'target', '_turnId': 'old-vu'},
            {'role': 'assistant', '_turnId': 'old-boundary'},
            {'role': 'user', 'content': 'new request'},
        ], total=300),
        _snapshot(messages=[{'_autopilotRunId': 'no-id'}], total=300),
    ])

    resolved = autopilot_state._resolve_run_anchor_turn_id(
        'conv', 'target', user_id=7)
    unresolved = autopilot_state._resolve_run_anchor_turn_id(
        'conv', 'no-id', user_id=7)

    assert resolved == 'old-boundary'
    assert unresolved == ''
    assert len(calls) == 3
