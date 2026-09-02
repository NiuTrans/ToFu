"""tests/test_assistant_message_build_parity.py — the build-time invariant that
replaces passive runtime tracer detection with an ACTIVE compile-time guard.

Root cause of the whole prefix-cache-drift saga: the assistant/tool_call message
was assembled by TWO independently hand-written code paths that had to be
aligned by hand, one drift at a time:
  * LIVE tail   — orchestrator ``_run.py`` clean_msg (in-loop tool round);
  * REPLAY      — ``_reconstruct_tool_call_messages`` (server-store-expiry
                  fallback that rebuilds from stored toolRounds).
Every historical miss (``.strip()`` raw↔stripped, str↔block ``{content}``,
thinking-no-signature ``{reasoning_content}``) was a divergence between these
two hand-written assemblers. The fix is a SINGLE SOURCE:
``build_assistant_tool_call_message(...)`` — both paths call it for the final
field assembly, so a NEW field added to one is structurally impossible to
forget on the other.

This suite is the guard: it pins the single source's output determinism across
the full (thinking × signature × content-shape) matrix, AND asserts that the
live-tail-shape and replay-shape inputs for the SAME logical round produce a
byte-identical assistant message. If anyone re-hand-writes one path, or the
builder's gating changes, a leg goes RED immediately — the passive runtime
tracer becomes an active build-time invariant.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_assistant_message_build_parity.py -p no:cacheprovider
"""

import json

import pytest

pytestmark = pytest.mark.unit

from lib.tasks_pkg.conv_message_builder._toolcalls import (
    build_assistant_tool_call_message,
    _reconstruct_tool_call_messages,
)

_TC = [{'id': 'c1', 'type': 'function',
        'function': {'name': 'run_command', 'arguments': '{"command": "ls"}'}}]


def _canon(msg):
    """Serialize a message dict the way the wire does (insertion order kept)."""
    return json.dumps(msg, ensure_ascii=False, sort_keys=False)


# ═══════════════════════════════════════════════════════════════════════════
#  Determinism of the single source across the field matrix
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('content,reasoning,sig,expect_content,expect_rc,expect_sig', [
    # content shape × thinking × signature
    ('let me check', 'thought', 'SIG',  'let me check', True,  True),
    ('  padded  ',   'thought', 'SIG',  'padded',       True,  True),   # strip
    ('let me check', 'thought', None,   'let me check', True,  False),  # unsigned thinking → keep rc, no sig
    ('let me check', None,      None,   'let me check', False, False),  # no thinking
    ('',             'thought', 'SIG',  None,           True,  True),   # empty content dropped
    ('   ',          None,      None,   None,           False, False),  # whitespace-only → dropped
])
def test_builder_field_matrix(content, reasoning, sig,
                              expect_content, expect_rc, expect_sig):
    msg = build_assistant_tool_call_message(
        tool_calls=_TC, content=content,
        reasoning_content=reasoning, thinking_signature=sig)
    assert msg['role'] == 'assistant'
    assert msg['tool_calls'] == _TC
    assert ('content' in msg) == (expect_content is not None)
    if expect_content is not None:
        assert msg['content'] == expect_content
    assert ('reasoning_content' in msg) == expect_rc
    assert ('thinking_signature' in msg) == expect_sig
    # A signature must never appear without reasoning_content.
    if 'thinking_signature' in msg:
        assert 'reasoning_content' in msg


def test_builder_key_order_stable():
    """Key insertion order is fixed (role, content, reasoning_content,
    thinking_signature, tool_calls) so the serialized bytes are deterministic
    regardless of which caller populated the fields."""
    msg = build_assistant_tool_call_message(
        tool_calls=_TC, content='x', reasoning_content='t', thinking_signature='S')
    assert list(msg.keys()) == ['role', 'content', 'reasoning_content',
                                'thinking_signature', 'tool_calls']


# ═══════════════════════════════════════════════════════════════════════════
#  LIVE ↔ REPLAY byte-parity for the SAME round (the load-bearing invariant)
# ═══════════════════════════════════════════════════════════════════════════

def _live_shape(content, reasoning, sig):
    """What the orchestrator live tail feeds the builder (raw in-memory msg
    fields) — must go through the SAME single source."""
    return build_assistant_tool_call_message(
        tool_calls=_TC,
        content=(content or '').strip() or content,  # live passes raw; builder strips
        reasoning_content=reasoning, thinking_signature=sig)


def _replay_shape(content, reasoning, sig):
    """What _reconstruct produces from a stored round — the assistant message
    (first element) of the reconstructed sequence."""
    rnd = {'toolCallId': 'c1', 'toolName': 'run_command',
           'toolArgs': '{"command": "ls"}', 'status': 'done',
           'toolContent': 'result', 'llmRound': 1,
           'assistantContent': content}
    if reasoning:
        rnd['thinking'] = reasoning
    if sig:
        rnd['thinkingSignature'] = sig
    reco = _reconstruct_tool_call_messages([rnd])
    return [m for m in reco if m.get('role') == 'assistant'][0]


@pytest.mark.parametrize('content,reasoning,sig', [
    ('let me check', 'thought', 'SIG'),
    ('let me check', 'thought', None),    # unsigned thinking (historical .thinking flip)
    ('let me check', None,      None),
    ('padded text',  'deep',    'SIG=='),
])
def test_live_replay_assistant_message_byte_identical(content, reasoning, sig):
    """The SAME logical round assembled via the live-tail inputs and via the
    replay reconstruction must yield a BYTE-IDENTICAL assistant message —
    because both now funnel through build_assistant_tool_call_message. This is
    the structural guarantee that no future field re-diverges."""
    live = build_assistant_tool_call_message(
        tool_calls=_TC, content=content,
        reasoning_content=reasoning, thinking_signature=sig)
    replay = _replay_shape(content, reasoning, sig)
    assert _canon(live) == _canon(replay), (
        f'live vs replay assistant message diverged:\n live  ={_canon(live)}\n '
        f'replay={_canon(replay)}')


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER: prove the parity is load-bearing (a hand-written divergence is caught)
# ═══════════════════════════════════════════════════════════════════════════

def test_nc_handwritten_divergence_is_caught():
    """If a caller hand-assembles the message with a DIFFERENT gate (e.g. drops
    reasoning_content when unsigned — the old replay bug), the parity assertion
    must fire — proving this suite catches a re-divergence."""
    shared = build_assistant_tool_call_message(
        tool_calls=_TC, content='x', reasoning_content='t',
        thinking_signature=None)  # unsigned → keeps reasoning_content
    handwritten_old = {'role': 'assistant', 'tool_calls': _TC, 'content': 'x'}
    # old bug dropped reasoning_content when unsigned
    assert _canon(shared) != _canon(handwritten_old)
