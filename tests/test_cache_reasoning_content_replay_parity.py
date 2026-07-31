"""tests/test_cache_reasoning_content_replay_parity.py — the thinking-no-signature
live↔replay ``{reasoning_content}`` prefix-cache flip.

THE ASYMMETRY (proven on real data — 28 rounds across 7 convs carry ``thinking``
with NO ``thinkingSignature``):

  * LIVE tail (orchestrator ``_run.py`` clean_msg): copies ``reasoning_content``
    UNCONDITIONALLY (``if assistant_msg.get('reasoning_content')``), and
    ``thinking_signature`` only when present.
  * REPLAY (``_reconstruct_tool_call_messages``): USED TO attach
    ``reasoning_content`` ONLY when BOTH ``thinking`` AND ``thinkingSignature``
    were present. So a round with thinking but no signature emitted
    ``reasoning_content`` LIVE but DROPPED it on replay → the SAME already-cached
    assistant/tool_call turn flips its ``{reasoning_content}`` bytes
    (canonical-VISIBLE as ``.thinking``) → prefix-cache miss. This is the
    historical ``.thinking`` culprit class (122 hits pre-fix).

THE FIX (symmetric, model-safe): replay mirrors the live tail's INDEPENDENT
gates — carry ``reasoning_content`` whenever thinking is present; carry
``thinking_signature`` only when present. Downstream, an UNSIGNED thinking block
is dropped identically on both paths by ``_assistant_blocks`` (Anthropic) and —
since 2026-07-26 (epic pt_8ffba515096142af) — STRIPPED from the Claude wire by
``_inject_claude_reasoning_details`` (the old "needs both, pass through" shape
was falsified in production: the sankuai gateway hard-400s unsigned Opus 5
thinking with ``signature: Field required``). For NON-Claude models the field
still reaches the wire, so live↔replay parity remains load-bearing there —
DeepSeek's ``model_requires_reasoning_content_replay`` (unsigned reasoning_content
MUST be replayed) is preserved. Signed thinking is unchanged (both fields kept).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_cache_reasoning_content_replay_parity.py -p no:cacheprovider
"""

import copy

import pytest

pytestmark = pytest.mark.unit

from lib.llm.body import build_body
from lib.llm.cache import add_cache_breakpoints
from lib.llm.anthropic_outbound._to_anthropic import openai_body_to_anthropic
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
from lib.tasks_pkg.wire_fingerprint import (
    wire_byte_field_prefix, diff_byte_field_prefix)

MODEL = 'aws.claude-opus-4.8'
_TOOLS = [{'type': 'function',
           'function': {'name': 'run_command', 'description': 'r',
                        'parameters': {'type': 'object'}}}]


def _live_tail_turn(thinking, sig):
    """The shape the orchestrator live tail (clean_msg) emits: reasoning_content
    unconditional, thinking_signature only when present."""
    m = {'role': 'assistant', 'content': 'let me check',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'run_command', 'arguments': '{}'}}]}
    if thinking:
        m['reasoning_content'] = thinking
    if sig:
        m['thinking_signature'] = sig
    return m


def _replay_turn(thinking, sig):
    """The shape _reconstruct_tool_call_messages emits from a stored round."""
    rounds = [{'toolCallId': 'c1', 'toolName': 'run_command', 'status': 'done',
               'toolContent': 'r', 'assistantContent': 'let me check',
               'llmRound': 1}]
    if thinking:
        rounds[0]['thinking'] = thinking
    if sig:
        rounds[0]['thinkingSignature'] = sig
    reco = _reconstruct_tool_call_messages(rounds)
    return [m for m in reco if m.get('role') == 'assistant'][0]


def _asst_field_bytes(asst_msg, anthropic=False):
    head = [{'role': 'system', 'content': 'S' * 80},
            {'role': 'user', 'content': 'go'}]
    msgs = head + [copy.deepcopy(asst_msg),
                   {'role': 'tool', 'tool_call_id': 'c1', 'content': 'r'}]
    body = build_body(MODEL, msgs, tools=copy.deepcopy(_TOOLS),
                      max_tokens=512, thinking_enabled=True)
    body['_task_id'] = 't'
    add_cache_breakpoints(body, '')
    if anthropic:
        body = openai_body_to_anthropic(body)
        return wire_byte_field_prefix(body.get('messages', []))
    return wire_byte_field_prefix(body['messages'])


# ═══════════════════════════════════════════════════════════════════════════
#  THE FIX: unsigned-thinking turn is byte-identical live vs replay
# ═══════════════════════════════════════════════════════════════════════════

def test_reconstruct_keeps_unsigned_reasoning_content():
    """A stored round with ``thinking`` but no ``thinkingSignature`` must
    reconstruct WITH ``reasoning_content`` (mirroring the live tail), so the
    field does not vanish on replay."""
    asst = _replay_turn('internal reasoning', None)
    assert asst.get('reasoning_content') == 'internal reasoning', (
        'reconstruction dropped unsigned reasoning_content — the live tail '
        'carries it, so this is the {reasoning_content} live↔replay flip')
    # An unsigned turn must NOT fabricate a signature.
    assert not asst.get('thinking_signature')


@pytest.mark.parametrize('shape', ['openai', 'anthropic'])
def test_unsigned_thinking_turn_byte_identical_live_vs_replay(shape):
    """The SAME unsigned-thinking turn emits byte-identical fields whether it
    came from the live tail or the replay reconstruction — no {reasoning_content}
    flip on an already-cached prefix."""
    anth = (shape == 'anthropic')
    live = _asst_field_bytes(_live_tail_turn('internal reasoning', None), anth)
    replay = _asst_field_bytes(_replay_turn('internal reasoning', None), anth)
    shared = min(len(live), len(replay))
    diff = diff_byte_field_prefix(live[:shared], replay[:shared])
    assert not diff, (
        f'[{shape}] unsigned-thinking turn flips bytes live vs replay: {diff}')


def test_signed_thinking_turn_still_byte_identical():
    """Regression guard: a SIGNED thinking turn (the common case) stays
    byte-identical live vs replay — the fix didn't disturb it."""
    live = _asst_field_bytes(_live_tail_turn('deep thought', 'SIG=='), False)
    replay = _asst_field_bytes(_replay_turn('deep thought', 'SIG=='), False)
    shared = min(len(live), len(replay))
    assert not diff_byte_field_prefix(live[:shared], replay[:shared])


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the flip is real without the fix (old BOTH-gate behavior)
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_old_both_gate_reproduces_flip():
    """NEUTER: emulate the PRE-FIX replay (drop reasoning_content unless BOTH
    thinking and signature present) and confirm it byte-diverges from the live
    tail on the {reasoning_content} field — proving the parity fix is
    load-bearing.

    ⚠️ Model choice is load-bearing (updated 2026-07-26, epic
    pt_8ffba515096142af): this NC MUST run against a NON-Claude model.  For
    Claude, ``_inject_claude_reasoning_details`` now STRIPS unsigned
    reasoning_content at build_body, so BOTH shapes converge on the wire and
    the flip can no longer fire — that is the intended upstream-400 fix, not
    a parity regression.  On non-Claude lines (DeepSeek & co., where
    reasoning_content MUST be replayed) the field still reaches the wire and
    live↔replay parity is exactly what prevents the cache flip."""
    # Pre-fix replay: no reasoning_content on an unsigned turn.
    old_replay = {'role': 'assistant', 'content': 'let me check',
                  'tool_calls': [{'id': 'c1', 'type': 'function',
                                  'function': {'name': 'run_command',
                                               'arguments': '{}'}}]}
    nc_model = 'kimi-k3'  # non-Claude: reasoning_content still reaches the wire
    head = [{'role': 'system', 'content': 'S' * 80},
            {'role': 'user', 'content': 'go'}]

    def _bytes(asst):
        msgs = head + [copy.deepcopy(asst),
                       {'role': 'tool', 'tool_call_id': 'c1', 'content': 'r'}]
        body = build_body(nc_model, msgs, tools=copy.deepcopy(_TOOLS),
                          max_tokens=512, thinking_enabled=True)
        body['_task_id'] = 't'
        add_cache_breakpoints(body, '')
        return wire_byte_field_prefix(body['messages'])

    live = _bytes(_live_tail_turn('internal reasoning', None))
    old = _bytes(old_replay)
    shared = min(len(live), len(old))
    diff = diff_byte_field_prefix(live[:shared], old[:shared])
    assert any(c.endswith('{reasoning_content}') for c in diff), (
        f'expected a {{reasoning_content}} flip under the old BOTH-gate on a '
        f'non-Claude model, got {diff}')
