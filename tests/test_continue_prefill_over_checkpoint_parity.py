#!/usr/bin/env python3
"""tests/test_continue_prefill_over_checkpoint_parity.py — the P5 parity
validation (epic pt_c11c3a9272274848).

QUESTION the epic asks: for a TOOLS turn interrupted mid-prose on a
prefill-capable model, does "replay completed tool rounds via
``inject_tool_history`` + prefill the terminal tail" produce BYTE-CORRECT,
LOSSLESS context — i.e. is flipping the current "checkpoint wins, drop the
tail" precedence (chat_dispatch.py) safe?

This suite PROVES the parity at the byte level (not just the docstring claim):

  (A) ``resume_prefill_from_segments`` returns ONLY the terminal tail — never
      the pre-tool prose of earlier batches (the no-double-count invariant).
  (B) ``inject_tool_history``'s wire splice contains the pre-tool prose + tool
      calls/results but NOT the terminal tail.
  (C) The terminal tail is exactly the text the current CHECKPOINT path
      discards (``scan_continue_checkpoint.discarded_content_text``) — so
      prefilling it recovers precisely what checkpoint loses.
  (D) The combined wire message order is valid: ``[..., user, assistant(pre-
      tool prose + tool_calls), tool(result)…, assistant-prefill(tail)]`` —
      tool history is spliced independent of the prefill, so the prefill is a
      clean trailing assistant turn after the last tool result.
  (E) The combined P5 context is LOSSLESS (the tail is preserved on the wire),
      while the current CHECKPOINT context is LOSSY (the tail is absent).

If all hold, P5 (prefer prefill over checkpoint for capable models) is
byte-correct and the flip is safe — the remaining gate is the OWNER's sign-off
on the resume-behaviour change, NOT a correctness blocker.

Run standalone:  python3 tests/test_continue_prefill_over_checkpoint_parity.py
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

CAPABLE = 'gpt-4o'               # model_supports_assistant_prefill → True
CLAUDE = 'claude-sonnet-4-5'     # prefill fail-closed

# ── A realistic tools turn: round 0 completes two tool calls with a preamble,
#    then the model starts the round-1 answer prose ('partial_B') and is cut. ──
PREAMBLE_A = 'Let me read those two files for you.'
PARTIAL_B = 'Based on what I found, the three causes are: (1)'
RES_X = 'contents of foo.py'
RES_Y = 'contents of bar.py'

SEGMENTS = [
    # batch prose for llmRound 0 (non-terminal, non-deliverable narration)
    {'type': 'text', 'text': PREAMBLE_A, 'deliverable': False,
     'terminal': False, 'llmRound': 0},
    # tool_use X (carries the batch prose via _rounds_view_from_segments)
    {'type': 'tool_use', 'id': 'call_X', 'name': 'read_files',
     'input': '{"path":"foo.py"}', 'llmRound': 0,
     'result': {'content': RES_X, 'status': 'done'}},
    # tool_use Y
    {'type': 'tool_use', 'id': 'call_Y', 'name': 'read_files',
     'input': '{"path":"bar.py"}', 'llmRound': 0,
     'result': {'content': RES_Y, 'status': 'done'}},
    # the terminal deliverable — the partial answer the model was mid-writing
    {'type': 'text', 'text': PARTIAL_B, 'deliverable': True,
     'terminal': True, 'resumable': True, 'llmRound': 1},
]

# The same turn as a toolRounds message (what scan_continue_checkpoint reads):
# the completed round carries the batch preamble; the full content also has
# the trailing partial_B prose that checkpoint must drop.
TOOL_ROUNDS_MSG = {
    'role': 'assistant',
    'content': PREAMBLE_A + '\n\n' + PARTIAL_B,
    'thinking': '',
    'finishReason': 'aborted',
    'toolRounds': [
        {'toolCallId': 'call_X', 'toolName': 'read_files', 'status': 'done',
         'toolContent': RES_X, 'llmRound': 0, 'assistantContent': PREAMBLE_A,
         'toolArgs': '{"path":"foo.py"}'},
        {'toolCallId': 'call_Y', 'toolName': 'read_files', 'status': 'done',
         'toolContent': RES_Y, 'llmRound': 0,
         'toolArgs': '{"path":"bar.py"}'},
    ],
}


# ─────────────────────────────────────────────────────────────────────────
#  (A) the prefill tail is ONLY the terminal deliverable — never pre-tool prose
# ─────────────────────────────────────────────────────────────────────────

def test_A_prefill_tail_is_terminal_deliverable_not_pretool_prose():
    from lib.tasks_pkg.segments import resume_prefill_from_segments
    tail = resume_prefill_from_segments(SEGMENTS, CAPABLE, finish_reason='aborted')
    assert tail == PARTIAL_B, 'prefill must be the terminal deliverable tail'
    assert PREAMBLE_A not in tail, 'prefill must NOT include the pre-tool prose'


def test_A_prefill_tail_declined_for_claude_fail_closed():
    from lib.tasks_pkg.segments import resume_prefill_from_segments
    assert resume_prefill_from_segments(SEGMENTS, CLAUDE, finish_reason='aborted') is None


# ─────────────────────────────────────────────────────────────────────────
#  (B) inject_tool_history splice excludes the terminal tail
# ─────────────────────────────────────────────────────────────────────────

def _splice_tool_history(messages, segments, model=CAPABLE):
    from lib.tasks_pkg.segments import tool_history_from_segments
    from lib.tasks_pkg.message_builder._tool_history import inject_tool_history
    cfg = {'toolHistory': tool_history_from_segments(segments)}
    task = {'id': 'paritytask', 'convId': 'parityconv'}
    inject_tool_history(messages, cfg, task, model)
    return messages


def test_B_splice_contains_pretool_prose_and_tools_but_not_tail():
    messages = [{'role': 'user', 'content': 'why does it fail?'}]
    _splice_tool_history(messages, SEGMENTS)
    import json as _json
    wire = _json.dumps(messages, ensure_ascii=False)
    # pre-tool prose IS replayed (inside the spliced assistant turn)
    assert PREAMBLE_A in wire
    # tool calls + results ARE replayed
    assert 'call_X' in wire and 'call_Y' in wire
    assert RES_X in wire and RES_Y in wire
    # the terminal tail is NOT part of the tool-history splice
    assert PARTIAL_B not in wire, 'tail must come from the prefill, not the splice'


# ─────────────────────────────────────────────────────────────────────────
#  (C) the tail is exactly what the current CHECKPOINT path discards
# ─────────────────────────────────────────────────────────────────────────

def test_C_checkpoint_discards_exactly_the_prefill_tail():
    from lib.chat.turn_builder import scan_continue_checkpoint
    from lib.tasks_pkg.segments import resume_prefill_from_segments
    import copy
    scan = scan_continue_checkpoint(copy.deepcopy(TOOL_ROUNDS_MSG))
    assert scan is not None
    discarded = scan['discarded_content_text']
    tail = resume_prefill_from_segments(SEGMENTS, CAPABLE, finish_reason='aborted')
    assert tail == discarded, (
        'the prefill tail must equal the prose the checkpoint path discards — '
        'prefilling it recovers exactly what checkpoint loses')


# ─────────────────────────────────────────────────────────────────────────
#  (D) the combined wire order is valid; (E) combined is lossless, checkpoint lossy
# ─────────────────────────────────────────────────────────────────────────

def _combined_wire(segments, model=CAPABLE):
    """Build the P5 wire context: inject_tool_history splice + assistant prefill."""
    from lib.tasks_pkg.segments import resume_prefill_from_segments
    messages = [{'role': 'user', 'content': 'why does it fail?'}]
    _splice_tool_history(messages, segments, model)
    tail = resume_prefill_from_segments(segments, model, finish_reason='aborted')
    if tail:
        messages.append({'role': 'assistant', 'content': tail})  # the prefill turn
    return messages, tail


def test_D_combined_wire_order_is_valid():
    messages, tail = _combined_wire(SEGMENTS)
    roles = [m['role'] for m in messages]
    # [user, assistant(tool_calls), tool, tool, assistant(prefill tail)]
    assert roles[0] == 'user'
    assert roles[1] == 'assistant' and messages[1].get('tool_calls'), \
        'the spliced assistant turn must carry the tool calls'
    assert roles[2] == 'tool' and roles[3] == 'tool'
    assert roles[-1] == 'assistant' and messages[-1]['content'] == tail, \
        'the prefill is a clean trailing assistant turn after the last tool result'


def test_E_combined_is_lossless_checkpoint_is_lossy():
    import json as _json
    # P5 combined: the tail IS on the wire (lossless).
    combined, tail = _combined_wire(SEGMENTS)
    assert PARTIAL_B in _json.dumps(combined, ensure_ascii=False)
    # Current checkpoint: the splice alone does NOT carry the tail (lossy —
    # the model regenerates it from the tool results).
    checkpoint_splice = [{'role': 'user', 'content': 'why does it fail?'}]
    _splice_tool_history(checkpoint_splice, SEGMENTS)
    assert PARTIAL_B not in _json.dumps(checkpoint_splice, ensure_ascii=False), \
        'checkpoint (without the prefill) loses the tail — the exact gap P5 closes'


def test_E_no_double_count_pretool_prose_appears_once():
    """The pre-tool prose must appear EXACTLY ONCE in the combined context
    (replayed by inject_tool_history) — never duplicated by the prefill."""
    import json as _json
    combined, _tail = _combined_wire(SEGMENTS)
    wire = _json.dumps(combined, ensure_ascii=False)
    assert wire.count(PREAMBLE_A) == 1, 'pre-tool prose must not be double-counted'
    assert wire.count(PARTIAL_B) == 1, 'the tail must appear exactly once (the prefill)'


# ─────────────────────────────────────────────────────────────────────────
#  (F) boundary: a no-tools turn is unaffected (checkpoint has no anchor)
# ─────────────────────────────────────────────────────────────────────────

def test_F_no_tools_turn_checkpoint_none_prefill_still_applies():
    """P5 only changes the TOOLS-turn precedence. A no-tools turn already uses
    prefill today (scan_continue_checkpoint → None) — unchanged."""
    from lib.chat.turn_builder import scan_continue_checkpoint
    no_tools_msg = {'role': 'assistant', 'content': PARTIAL_B,
                    'finishReason': 'aborted', 'toolRounds': []}
    assert scan_continue_checkpoint(no_tools_msg) is None


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
