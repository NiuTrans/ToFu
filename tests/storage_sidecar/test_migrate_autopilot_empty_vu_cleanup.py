"""Empty-VU-shell filtering at the authoritative Sidecar projection seam.

The retired direct-database migration has no runtime authority after the
turn-native storage cutover.  Its safety classifier now lives at
``turn.visible.sync``, the only compatibility boundary that can project a
legacy executor message list into durable turn rows.  It must drop ONLY
provably content-free ghost rows and keep every legitimate record
byte-identical. Pinned shapes:

  * DELETE empty VU row (role=user + _isVirtualUser + empty content).
  * DELETE the empty aborted assistant DIRECTLY after it (the ghost
    follow-up the user had to stop — an assistant answering an EMPTY user
    turn can have no other origin).
  * KEEP a real VU instruction (non-empty).
  * KEEP a human-stopped turn's aborted-empty assistant (follows a REAL user
    message — a legitimate "you stopped this turn" record).
  * KEEP an empty assistant that is NOT adjacent to a ghost (error ghosts /
    interrupted turns are history, and deleting them is not this migration's
    mandate).
  * KEEP whitespace-bearing VU content? No — whitespace-only is empty.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from lib.storage_sidecar.operations_pkg._turns import (
    _partition_visible_messages,
    _turn_visible_sync,
)


def _vu(content):
    return {'role': 'user', '_isVirtualUser': True, 'content': content,
            '_msgId': 'vu-x'}


def _assistant(content='', fr='aborted', thinking='', rounds=None):
    return {'role': 'assistant', 'content': content, 'thinking': thinking,
            'finishReason': fr, 'toolRounds': rounds or [], '_msgId': 'a-x'}


def _human(text):
    return {'role': 'user', 'content': text, '_msgId': 'h-x'}


def test_deletes_empty_vu_and_adjacent_ghost_followup():
    """The incident shape (ms9ow2tt): empty VU shell + the empty aborted
    assistant of the follow-up the user had to stop — BOTH go."""
    msgs = [_human('build it'), _assistant('done.', fr='stop'),
            _vu(''), _assistant()]
    keep, drop = _partition_visible_messages(msgs)
    assert keep == [_human('build it'), _assistant('done.', fr='stop')]
    assert drop == [_vu(''), _assistant()]


def test_keeps_real_vu_instruction():
    msgs = [_human('build it'), _vu('next, add tests')]
    keep, drop = _partition_visible_messages(msgs)
    assert keep == msgs and drop == []


def test_keeps_human_stopped_aborted_assistant():
    """A human typed a real message and stopped the turn before first token:
    the aborted-empty assistant is a legitimate record — KEEP."""
    msgs = [_human('real question'), _assistant()]
    keep, drop = _partition_visible_messages(msgs)
    assert keep == msgs and drop == []


def test_keeps_empty_assistant_not_adjacent_to_ghost():
    """An empty aborted assistant after a NON-ghost VU row (the user stopped
    a legitimate autopilot follow-up) is real history — KEEP."""
    msgs = [_human('build it'), _vu('real instruction'), _assistant()]
    keep, drop = _partition_visible_messages(msgs)
    assert keep == msgs and drop == []


def test_whitespace_vu_is_empty_but_thinking_assistant_is_not():
    msgs = [_vu('   '), _assistant(thinking='reasoning so far')]
    keep, drop = _partition_visible_messages(msgs)
    # VU shell dropped; the assistant keeps thinking content → not a ghost.
    assert drop == [_vu('   ')]
    assert keep == [_assistant(thinking='reasoning so far')]


def test_keeps_assistant_with_tool_rounds():
    """An aborted assistant that already did tool work is a partial turn,
    not a ghost — KEEP even adjacent to a shell."""
    partial = _assistant(rounds=[{'toolName': 'read_files'}])
    msgs = [_vu(''), partial]
    keep, drop = _partition_visible_messages(msgs)
    assert drop == [_vu('')] and keep == [partial]


def test_visible_sync_stops_before_storage_when_only_ghosts_remain():
    """The operation seam, not a detached helper, owns the filter."""

    class StorageMustNotRun:
        def __getattr__(self, name):
            raise AssertionError(f'storage touched for empty projection: {name}')

    result = _turn_visible_sync(StorageMustNotRun(), {
        'conversation_id': 'conv-ghost',
        'user_id': 7,
        'attempt_id': 'attempt-ghost',
        'root_turn_id': 'turn-ghost',
        'messages': [_vu(''), _assistant()],
    })

    assert result is None


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))
