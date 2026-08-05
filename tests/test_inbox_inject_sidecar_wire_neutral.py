"""Load-bearing guard: the inbox-inject DISPLAY-ONLY sidecar lanes
(``_inboxInjects`` / ``_peerInjects`` / ``_userSteerInjects``) must be
WIRE-NEUTRAL.

WHY (conv: "agent_inbox info disappears on refresh")
----------------------------------------------------
Swarm ``<swarm-update>`` results, peer messages, and human "steer" messages
are injected into the model's message stream mid-turn and shown to the human as
an in-timeline synthetic ``toolRound``. Those synthetic rows are DISPLAY-ONLY
and must NEVER be persisted into the DB ``toolRounds`` — because ``toolRounds``
is ALSO the wire-replay / prefix-cache source:

  * ``_build_assistant_messages`` / ``_reconstruct_tool_call_messages`` rebuild
    the ``assistant(tool_calls)+tool`` wire sequence FROM ``toolRounds``.
  * ``_reconstruct_tool_call_messages`` does a first-pass validation that
    returns ``None`` (collapsing the WHOLE assistant turn to a lossy
    ``toolSummary`` placeholder) if ANY round lacks
    ``toolCallId`` / ``toolName`` / ``toolContent`` / ``status=='done'``.

A synthetic inject row has none of those fields, so folding it into
``toolRounds`` would (a) break tool-turn continuation (the tool_call/tool
pairing is destroyed) AND (b) shift the wire prefix (structured tool messages
become one summary string) → a prefix-cache miss.

The fix persists these lanes as underscore SIDECAR fields on the message dict,
exactly like ``_relatedConversations`` / ``_memoryPrefetch``. The wire builders
read only ``role`` / ``content`` / ``toolRounds`` / ``segments`` / ``toolSummary``
— never underscore fields — so the sidecar is provably wire-neutral.

This suite pins that invariant:
  • GOLDEN: ``_build_assistant_messages`` output is BYTE-IDENTICAL with vs.
    without the sidecar fields, and ``_reconstruct_tool_call_messages`` does NOT
    return ``None`` (structured reconstruction survives).
  • NEUTER: fold a synthetic inject row INTO ``toolRounds`` (the poison the fix
    prevents) → reconstruction returns ``None`` AND the wire messages diverge →
    the test FAILS, proving the assertions are load-bearing.

Pure-unit: no Flask, no DB, no LLM.
"""

from __future__ import annotations

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
)


# ── The three DISPLAY-ONLY inject-lane sidecar fields (must stay in lock-step
#    with lib/tasks_pkg/manager/_sync.py::INBOX_INJECT_SIDECAR_FIELDS). ──
SIDECAR_FIELDS = ('_inboxInjects', '_peerInjects', '_userSteerInjects')


def _real_round(rnum, llm_round, tc_id, name, args, content):
    """A genuine, replayable tool round in the live shape."""
    return {
        'roundNum': rnum,
        'llmRound': llm_round,
        'toolCallId': tc_id,
        'toolName': name,
        'toolArgs': args,
        'toolContent': content,
        'status': 'done',
    }


def _synthetic_inbox_round(rnum):
    """A synthetic swarm-inbox-inject row — the shape the FRONTEND builds live
    in ``_handleSwarmInboxInject``. It has NO toolCallId/toolName/toolContent,
    so it would poison ``_reconstruct_tool_call_messages`` if persisted into
    ``toolRounds``. This is exactly what the sidecar keeps OUT of toolRounds."""
    return {
        'roundNum': 9000000 + rnum,
        'status': 'done',
        '_inboxInject': True,
        '_inboxKey': 'inbox:%d' % rnum,
        'inboxRound': rnum,
        'inboxCount': 2,
        'inboxAgentIds': ['a1', 'a2'],
        'inboxPreviews': [{'agentId': 'a1', 'text': '<swarm-update>done</swarm-update>'}],
    }


def _base_msg():
    """A finished assistant message with TWO real tool rounds + a final answer
    (a multi-round turn — the shape whose wire replay we must not perturb)."""
    return {
        'role': 'assistant',
        'content': 'Fixed the bug on line 42.',
        'thinking': '',
        'toolRounds': [
            _real_round(1, 0, 'tc_1', 'grep_search', '{"pattern":"bug"}', 'line 42 hit'),
            _real_round(2, 1, 'tc_2', 'apply_diff', '{"path":"b.py"}', 'ok, 1 change'),
        ],
    }


def _msg_with_sidecar():
    """The same message PLUS the three inject-lane sidecar fields populated —
    i.e. what the sync layer persists after a turn that received inbox injects."""
    msg = _base_msg()
    msg['_inboxInjects'] = [
        {'round': 1, 'count': 2, 'agentIds': ['a1', 'a2'],
         'previews': [{'agentId': 'a1', 'text': '<swarm-update>done</swarm-update>'}]},
    ]
    msg['_peerInjects'] = [
        {'round': 2, 'count': 1,
         'previews': [{'fromConv': 'sib1', 'text': 'peer note'}]},
    ]
    msg['_userSteerInjects'] = [
        {'round': 2, 'count': 1, 'previews': [{'text': 'actually, focus on X'}]},
    ]
    return msg


# ═══════════════════════════════════════════════════════════
#  GOLDEN — the sidecar is wire-neutral
# ═══════════════════════════════════════════════════════════

class TestSidecarWireNeutral:
    def test_build_assistant_messages_byte_identical_with_sidecar(self):
        """Adding the sidecar fields must NOT change the wire messages at all."""
        without = _build_assistant_messages(_base_msg())
        with_sc = _build_assistant_messages(_msg_with_sidecar())
        assert without == with_sc, (
            'inbox-inject sidecar fields leaked into the wire messages — '
            'they must be display-only and invisible to _build_assistant_messages'
        )

    def test_reconstruct_survives_with_sidecar(self):
        """The structured (tool_calls+tool) reconstruction must NOT collapse to
        the lossy summary fallback when sidecar fields are present. The sidecar
        lives OUTSIDE toolRounds, so reconstruction reads only the two real
        rounds and returns a proper structured sequence (not None)."""
        msg = _msg_with_sidecar()
        structured = _reconstruct_tool_call_messages(msg['toolRounds'])
        assert structured is not None, (
            'structured reconstruction returned None even though toolRounds holds '
            'only real rounds — the sidecar must not touch toolRounds'
        )
        # Both real tool calls survive as an assistant(tool_calls)+tool pairing.
        tool_msgs = [m for m in structured if m.get('role') == 'tool']
        assert {m['tool_call_id'] for m in tool_msgs} == {'tc_1', 'tc_2'}

    def test_sidecar_fields_are_present_on_the_message_but_off_the_wire(self):
        """Sanity: the sidecar fields DO ride on the stored message dict (so the
        frontend can rebuild the chips), while being absent from the wire."""
        msg = _msg_with_sidecar()
        for f in SIDECAR_FIELDS:
            assert msg.get(f), 'fixture should populate %s' % f
        wire = _build_assistant_messages(msg)
        import json as _json
        blob = _json.dumps(wire, ensure_ascii=False)
        # The inject-only marker text must not appear anywhere in the wire.
        assert 'swarm-update' not in blob
        assert 'peer note' not in blob
        assert 'focus on X' not in blob


# ═══════════════════════════════════════════════════════════
#  Belt-and-suspenders: a LEAKED synthetic row is still wire-neutral
# ═══════════════════════════════════════════════════════════

class TestLeakedSyntheticRowStillNeutral:
    """Even if the sidecar discipline is bypassed and a synthetic inject row
    somehow lands IN toolRounds (e.g. a legacy full-conv PUT from an old
    client), the wire-purity guard ``is_synthetic_inbox_round`` filters it out
    at reconstruction time — so the wire replay is UNCHANGED. This is the
    defense-in-depth layer that complements the sidecar."""

    def test_leaked_synthetic_row_is_filtered_from_the_wire(self):
        clean = _build_assistant_messages(_base_msg())
        leaked_msg = _base_msg()
        leaked_msg['toolRounds'].insert(1, _synthetic_inbox_round(1))
        leaked = _build_assistant_messages(leaked_msg)
        assert clean == leaked, (
            'a leaked synthetic inbox row must be filtered by the wire-purity '
            'guard so the wire bytes are identical to the clean replay'
        )
        # Structured reconstruction survives (real rounds only).
        assert _reconstruct_tool_call_messages(leaked_msg['toolRounds']) is not None
        assert any(m.get('role') == 'tool' for m in leaked)


# ═══════════════════════════════════════════════════════════
#  NEUTER — disable the wire-purity guard, prove the danger is real
# ═══════════════════════════════════════════════════════════

class TestNeuterWirePurityGuard:
    """Prove the FILTER CHAIN is load-bearing.

    A synthetic inbox row lacks every field a replayable round needs, so it is
    caught by BOTH predicates in the reconstructor —
    ``not is_synthetic_inbox_round(r)`` AND ``_is_reconstructable_round(r)``.
    The ORIGINAL NCs disabled only the first and died confused: the structural
    predicate still filtered the row, so "without the guard" nothing diverged
    (the guard looked redundant). Disabling the whole CHAIN lets the row
    through, and reconstruction DETONATES on it (measured: ``KeyError:
    'toolCallId'`` — a display-only row reaches the tool_calls builder, which
    reads fields it never had). The wire-purity filter as a whole is what
    keeps a display-only row from destroying tool-turn continuation."""

    def _disable_filter_chain(self, monkeypatch):
        import lib.tasks_pkg.segments._types as _seg_types
        import lib.tasks_pkg.conv_message_builder._toolcalls as _tc
        monkeypatch.setattr(_seg_types, 'is_synthetic_inbox_round',
                            lambda _r: False)
        monkeypatch.setattr(_tc, '_is_reconstructable_round', lambda _r: True)

    def test_NC_without_filter_chain_synthetic_row_detonates_reconstruction(self, monkeypatch):
        self._disable_filter_chain(monkeypatch)
        poisoned = _base_msg()
        poisoned['toolRounds'].insert(1, _synthetic_inbox_round(1))
        with pytest.raises(KeyError):
            _reconstruct_tool_call_messages(poisoned['toolRounds'])

    def test_NC_without_filter_chain_synthetic_row_diverges_the_wire(self, monkeypatch):
        clean = _build_assistant_messages(_base_msg())
        self._disable_filter_chain(monkeypatch)
        poisoned_msg = _base_msg()
        poisoned_msg['toolRounds'].insert(1, _synthetic_inbox_round(1))
        try:
            poisoned = _build_assistant_messages(poisoned_msg)
        except KeyError:
            poisoned = 'DETONATED'
        assert clean != poisoned, (
            'with the filter chain disabled, folding a synthetic inject row '
            'into toolRounds must change (or destroy) the wire replay — proving '
            'keeping it OUT (sidecar) + filtering it (chain) is what preserves '
            'tool-turn continuation and the prefix cache')


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
