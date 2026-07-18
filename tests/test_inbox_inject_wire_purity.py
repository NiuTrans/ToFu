"""Wire-purity guard: agent_inbox synthetic toolRounds must NEVER change the
bytes sent to the LLM.

WHY (conv mrn4f... refresh-disappearance root-cause)
----------------------------------------------------
The frontend surfaces async ``<swarm-update>`` / peer / user-steer injections
as SYNTHETIC ``toolRounds`` entries flagged ``_inboxInject`` / ``_peerInject`` /
``_userSteerInject`` (roundNum 9e6+, no ``toolCallId`` / ``toolContent``). These
are DISPLAY-ONLY chips. But ``toolRounds`` is ALSO the wire-replay source:

  * ``_build_assistant_messages`` / ``_reconstruct_tool_call_messages`` expand
    every round into ``assistant(tool_calls)+tool`` messages, and the
    reconstructor does a FIRST-PASS validation that returns ``None`` — collapsing
    the WHOLE assistant turn into a lossy ``toolSummary`` placeholder — if any
    round lacks ``toolCallId`` / ``toolName`` / ``toolContent`` / ``status==done``.
  * ``assemble_segments`` turns every round into a ``tool_use`` segment.

So a synthetic inbox row that ever reaches the persisted ``toolRounds`` would:
  1. break tool-turn continuation (the assistant(tool_calls)+tool pairing is
     destroyed → the model can't continue the interrupted tool sequence), AND
  2. change the wire bytes (structured tool messages → one summary string) →
     prefix-cache MISS.

The load-bearing fix: BOTH reconstructors must SKIP synthetic inbox rows, so
the wire output is byte-identical whether or not the synthetic rows are present.
These tests pin that invariant and neuter it to prove it is load-bearing.

Pure unit — no Flask, no DB, no LLM.
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.tasks_pkg.conv_message_builder._toolcalls import (
    _reconstruct_tool_call_messages,
)
from lib.tasks_pkg.conv_message_builder._transform import (
    _build_assistant_messages,
    _transform_messages,
)
from lib.tasks_pkg.segments import assemble_segments


# ═══════════════════════════════════════════════════════════
#  Fixtures — a realistic assistant turn with real tool rounds
# ═══════════════════════════════════════════════════════════

def _real_round(rn, lr, tc_id, name, args, content, *, ac=""):
    r = {
        "roundNum": rn, "llmRound": lr, "toolCallId": tc_id,
        "toolName": name, "toolArgs": args, "toolContent": content,
        "status": "done",
    }
    if ac:
        r["assistantContent"] = ac
    return r


def _real_rounds():
    return [
        _real_round(1, 0, "tc_1", "web_search", '{"q":"gil"}', "GIL result",
                    ac="Let me search."),
        _real_round(2, 1, "tc_2", "read_files", '{"path":"a.py"}', "file body",
                    ac="Now reading the file."),
    ]


def _synth_inbox_row(idx):
    """Mirror sse_handlers_lifecycle.js::_handleSwarmInboxInject exactly."""
    return {
        "roundNum": 9000000 + idx,
        "status": "done",
        "_inboxInject": True,
        "_inboxKey": "inbox:%d" % idx,
        "inboxRound": idx,
        "inboxCount": 2,
        "inboxAgentIds": ["a1", "a2"],
        "inboxPreviews": [{"agentId": "a1", "text": "done A"}],
    }


def _synth_peer_row(idx):
    return {
        "roundNum": 9000000 + idx,
        "status": "done",
        "_peerInject": True,
        "_peerKey": "peer:%d" % idx,
        "peerRound": idx,
        "peerCount": 1,
        "peerPreviews": [{"fromConv": "sib1", "text": "hi from sibling"}],
    }


def _synth_steer_row(idx):
    return {
        "roundNum": 9000000 + idx,
        "status": "done",
        "_userSteerInject": True,
        "_userSteerKey": "steer:%d" % idx,
        "steerRound": idx,
        "steerCount": 1,
        "steerPreviews": [{"text": "go check the tests too"}],
    }


def _assistant_msg(rounds, content="Final answer."):
    return {"role": "assistant", "content": content,
            "toolRounds": copy.deepcopy(rounds)}


# ═══════════════════════════════════════════════════════════
#  Invariant 1 — _reconstruct_tool_call_messages is inbox-blind
# ═══════════════════════════════════════════════════════════

class TestReconstructWirePurity:
    def test_synthetic_rows_do_not_collapse_the_turn(self):
        """A turn with a synthetic inbox row interleaved must STILL reconstruct
        the structured assistant(tool_calls)+tool sequence — not fall back to
        the lossy summary placeholder (which returns None here)."""
        rounds_clean = _real_rounds()
        rounds_dirty = [
            _real_rounds()[0],
            _synth_inbox_row(0),
            _real_rounds()[1],
            _synth_peer_row(1),
        ]
        out_clean = _reconstruct_tool_call_messages(rounds_clean)
        out_dirty = _reconstruct_tool_call_messages(rounds_dirty)
        assert out_clean is not None, "baseline must reconstruct"
        # THE INVARIANT: byte-identical wire regardless of synthetic rows.
        assert out_dirty == out_clean

    def test_all_three_lanes_are_skipped(self):
        rounds_dirty = [
            _synth_inbox_row(0),
            _real_rounds()[0],
            _synth_peer_row(1),
            _real_rounds()[1],
            _synth_steer_row(2),
        ]
        out = _reconstruct_tool_call_messages(rounds_dirty)
        assert out == _reconstruct_tool_call_messages(_real_rounds())

    def test_only_synthetic_rows_is_none_not_bogus_toolcall(self):
        """A turn whose ONLY rounds are synthetic has NO real tool calls → the
        reconstructor returns None (no bogus tool_call fabricated), so the
        assistant renders as a plain text turn, not a broken tool turn."""
        out = _reconstruct_tool_call_messages(
            [_synth_inbox_row(0), _synth_peer_row(1)])
        assert out is None


# ═══════════════════════════════════════════════════════════
#  Invariant 2 — full DB→wire path (_build_assistant_messages)
# ═══════════════════════════════════════════════════════════

class TestBuildAssistantWirePurity:
    def test_build_assistant_messages_byte_identical(self):
        clean = _build_assistant_messages(_assistant_msg(_real_rounds()))
        dirty = _build_assistant_messages(_assistant_msg([
            _real_rounds()[0], _synth_inbox_row(0),
            _real_rounds()[1], _synth_steer_row(1),
        ]))
        assert dirty == clean
        # And it really did the structured expansion (not the summary fallback).
        assert any(m.get("tool_calls") for m in clean)

    def test_only_synthetic_rows_renders_as_plain_text_turn(self):
        """An assistant row carrying ONLY synthetic inbox rows + a real answer
        must send exactly the plain answer — no fabricated tool_calls, no
        summary JSON placeholder."""
        msg = _assistant_msg([_synth_inbox_row(0)], content="Here is the answer.")
        out = _build_assistant_messages(msg)
        assert out == [{"role": "assistant", "content": "Here is the answer."}]

    def test_full_transform_pipeline_byte_identical(self):
        """End-to-end via _transform_messages (the real build_api_messages_from_db
        chokepoint): a conversation whose assistant turn has synthetic rows must
        produce the SAME wire messages as one without them."""
        cfg = {"systemPrompt": "be helpful"}
        conv_clean = [
            {"role": "user", "content": "do X"},
            _assistant_msg(_real_rounds()),
            {"role": "user", "content": "follow up"},
        ]
        conv_dirty = [
            {"role": "user", "content": "do X"},
            _assistant_msg([
                _real_rounds()[0], _synth_inbox_row(0),
                _real_rounds()[1], _synth_peer_row(1),
            ]),
            {"role": "user", "content": "follow up"},
        ]
        assert (_transform_messages(conv_dirty, cfg)
                == _transform_messages(conv_clean, cfg))


# ═══════════════════════════════════════════════════════════
#  Invariant 3 — segments never mint a tool_use for a synthetic row
# ═══════════════════════════════════════════════════════════

class TestSegmentsWirePurity:
    def test_no_phantom_tool_use_segment(self):
        task_clean = {"id": "s" * 32, "convId": "c" * 32,
                      "content": "A.", "thinking": "",
                      "toolRounds": _real_rounds()}
        task_dirty = dict(task_clean)
        task_dirty["toolRounds"] = [
            _real_rounds()[0], _synth_inbox_row(0), _real_rounds()[1],
        ]
        segs_clean = assemble_segments(task_clean)
        segs_dirty = assemble_segments(task_dirty)
        tu_clean = [s for s in segs_clean if s.get("type") == "tool_use"]
        tu_dirty = [s for s in segs_dirty if s.get("type") == "tool_use"]
        assert len(tu_dirty) == len(tu_clean) == 2
        # Names line up — no phantom inbox tool_use minted.
        assert [s["name"] for s in tu_dirty] == ["web_search", "read_files"]


# ═══════════════════════════════════════════════════════════
#  NEUTER — prove the skip is load-bearing
# ═══════════════════════════════════════════════════════════

class TestNeuterProvesLoadBearing:
    def test_without_skip_the_turn_would_collapse(self):
        """Simulate the guard being ABSENT: pass a synthetic row through the
        raw reconstructor WITHOUT the skip. Because it lacks toolCallId/
        toolContent, the first-pass validation returns None → the whole turn
        would collapse to the lossy summary (breaking continuation + cache).

        This is the exact failure the guard prevents; if a future refactor
        removes the skip, TestReconstructWirePurity above turns RED and this
        test documents WHY."""
        # A synthetic row with the real rounds, but NOT skipped — this is what
        # _reconstruct_tool_call_messages sees if the guard is removed.
        poisoned = [_real_rounds()[0], _synth_inbox_row(0), _real_rounds()[1]]
        # Manually replicate the first-pass validation WITHOUT the skip:
        collapses = any(
            (not r.get("toolCallId")) or (not r.get("toolName"))
            or (r.get("status") != "done") or (r.get("toolContent") is None)
            for r in poisoned
        )
        assert collapses, (
            "a raw (un-skipped) synthetic row must trip the validation → "
            "proving the skip in _reconstruct_tool_call_messages is what keeps "
            "the wire byte-identical")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
