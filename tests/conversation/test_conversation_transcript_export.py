"""Complete transcript export: every message, one consistent revision.

The clone command is atomic and therefore bounded; a conversation larger than
one authority document cannot be copied at all. The export route is the
recovery path, so it must never inherit the model-facing reference read's
HEAD+TAIL window or its 80,000-character clamp — a saved file that silently
drops messages is data loss (2026-10-22 report).

These tests drive the real module against a fake authority that pages exactly
like the repository does, and assert the properties that make the file a
record: complete message set, ascending order, one revision, explicit refusal
when the read cannot be complete.
"""

from __future__ import annotations

import pytest

from lib.conv_ref import TranscriptExportError, render_full_transcript
from lib.conv_ref import _export as export_module


pytestmark = pytest.mark.unit


class _FakeAuthority:
    """Serve ``message_window`` / ``before_sequence`` pages like the repository."""

    def __init__(self, messages, *, rev=7, short_page=False):
        self.messages = messages
        self.rev = rev
        self.short_page = short_page
        self.calls = []

    def __call__(self, conversation_id, *, user_id, **projection):
        if conversation_id != 'conv-1':
            return None
        window = projection.get('message_window')
        before = projection.get('before_sequence')
        end = len(self.messages) if before is None else max(0, int(before))
        start = 0 if window is None else max(0, end - int(window))
        page = self.messages[start:end]
        self.calls.append((start, end))
        if self.short_page:
            page = page[:-1]
        return {
            'id': conversation_id,
            'rev': self.rev,
            'msg_count': len(self.messages),
            'messages': [dict(message) for message in page],
        }


def _messages(count):
    return [
        {
            'role': 'user' if index % 2 == 0 else 'assistant',
            'content': f'message-{index}',
        }
        for index in range(count)
    ]


def test_every_message_survives_a_transcript_larger_than_one_page(monkeypatch):
    authority = _FakeAuthority(_messages(523))
    monkeypatch.setattr(export_module, '_read_conversation_snapshot', authority)

    text, count = render_full_transcript('conv-1', user_id=1)

    assert count == 523
    for index in range(523):
        assert f'message-{index}\n' in text, f'message {index} missing'
    assert len(authority.calls) > 1, 'the export read the whole transcript at once'


def test_export_keeps_ascending_order_across_page_boundaries(monkeypatch):
    authority = _FakeAuthority(_messages(400))
    monkeypatch.setattr(export_module, '_read_conversation_snapshot', authority)

    text, _ = render_full_transcript('conv-1', user_id=1)

    positions = [text.index(f'message-{index}\n') for index in range(400)]
    assert positions == sorted(positions), 'pages were stitched out of order'


def test_a_short_page_fails_instead_of_publishing_a_shorter_file(monkeypatch):
    authority = _FakeAuthority(_messages(300), short_page=True)
    monkeypatch.setattr(export_module, '_read_conversation_snapshot', authority)

    with pytest.raises(TranscriptExportError) as raised:
        render_full_transcript('conv-1', user_id=1)
    assert str(raised.value) == 'conversation_export_incomplete'


def test_a_revision_change_during_export_is_refused(monkeypatch):
    messages = _messages(300)
    state = {'page': 0}

    def reader(conversation_id, *, user_id, **projection):
        state['page'] += 1
        window = projection.get('message_window')
        before = projection.get('before_sequence')
        end = len(messages) if before is None else max(0, int(before))
        start = max(0, end - int(window))
        return {
            'id': conversation_id,
            # The second page observes a newer conversation revision.
            'rev': 7 if state['page'] == 1 else 8,
            'msg_count': len(messages),
            'messages': [dict(message) for message in messages[start:end]],
        }

    monkeypatch.setattr(export_module, '_read_conversation_snapshot', reader)

    with pytest.raises(TranscriptExportError) as raised:
        render_full_transcript('conv-1', user_id=1)
    assert str(raised.value) == 'conversation_changed_during_export'


def test_missing_conversation_is_reported_not_rendered_empty(monkeypatch):
    monkeypatch.setattr(
        export_module, '_read_conversation_snapshot',
        lambda conversation_id, *, user_id, **projection: None,
    )

    with pytest.raises(TranscriptExportError) as raised:
        render_full_transcript('conv-1', user_id=1)
    assert str(raised.value) == 'conversation_missing'


def test_an_absurd_message_count_is_refused_before_paging(monkeypatch):
    authority = _FakeAuthority([])
    authority.messages = []

    def reader(conversation_id, *, user_id, **projection):
        return {
            'id': conversation_id,
            'rev': 1,
            'msg_count': export_module.MAX_EXPORT_MESSAGES + 1,
            'messages': [],
        }

    monkeypatch.setattr(export_module, '_read_conversation_snapshot', reader)

    with pytest.raises(TranscriptExportError) as raised:
        render_full_transcript('conv-1', user_id=1)
    assert str(raised.value) == 'conversation_too_large'


def test_tool_details_are_omitted_only_when_the_caller_asks(monkeypatch):
    messages = [{
        'role': 'assistant',
        'content': 'answer',
        'thinking': 'private reasoning',
        'toolRounds': [{
            'toolName': 'run_command',
            'status': 'done',
            'args': {'command': 'ls'},
            'toolContent': 'a.txt',
        }],
    }]
    monkeypatch.setattr(
        export_module, '_read_conversation_snapshot', _FakeAuthority(messages),
    )

    full, _ = render_full_transcript('conv-1', user_id=1, include_tool_details=True)
    lean, _ = render_full_transcript('conv-1', user_id=1, include_tool_details=False)

    # Tool NAMES and statuses stay in both: they are the record of what ran.
    # Arguments and results are the detail a lean export drops.
    assert 'run_command' in full and 'run_command' in lean
    assert 'private reasoning' in full
    assert 'private reasoning' not in lean
    assert 'command: ls' in full
    assert 'command: ls' not in lean
    assert 'answer' in lean
