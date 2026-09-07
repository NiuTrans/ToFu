"""tests/test_claude_unsigned_thinking_strip.py — unsigned Claude thinking must
never reach the wire.

PRODUCTION INCIDENT (epic pt_8ffba515096142af, forensics in JOURNAL 2026-07-26):
the sankuai OpenAI-compat gateway streams Opus 5 thinking as ``reasoning_content``
but NEVER returns a signature (0/92 thinking segments across 15 recent done
tasks; live state snapshots show assistant turns with ``reasoning_content`` and
no ``thinking_signature``).  Replaying those turns puts an UNSIGNED thinking
block on the wire, and since 2026-07-25 18:57 the upstream rejects it with
HTTP 400 ``invalid_request_error: …signature: Field required`` — classified
non-retryable, killing the whole turn (9 events / 6 tasks / 3 key slots on
07-25, 13+ on 07-26, rounds R1–R30).

THE CONTRACT: Anthropic allows omitting prior thinking blocks entirely; only a
block that IS replayed must carry its signature.  So the safe move for an
unsigned trace is to DROP it from the wire (the model re-reasons) — losing the
trace beats losing the turn.  The chokepoint is
``_inject_claude_reasoning_details`` in lib/llm/body/_model_tweaks.py, which
already owns this wire shape for Claude and runs on every request, so live-tail,
conv-replay, compaction and retry paths are all covered at once.

INVARIANTS GUARDED HERE:
  1. unsigned ``reasoning_content`` on a Claude-bound assistant turn is stripped;
  2. signed thinking still gets ``reasoning_details`` synthesised (unchanged);
  3. non-Claude models are untouched — DeepSeek's OPPOSITE rule
     (``model_requires_reasoning_content_replay``: reasoning_content MUST be
     replayed or the API 400s) must not be caught by the strip;
  4. live-tail vs conv-replay stay byte-identical after the strip (the
     {reasoning_content} cache-flip class from
     tests/test_cache_reasoning_content_replay_parity.py must NOT come back).

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_claude_unsigned_thinking_strip.py -p no:cacheprovider
"""

import copy

import pytest

pytestmark = pytest.mark.unit

from lib.llm.body import build_body
from lib.llm.body._model_tweaks import _inject_claude_reasoning_details
from lib.llm.cache import add_cache_breakpoints
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
from lib.tasks_pkg.wire_fingerprint import (
    wire_byte_field_prefix, diff_byte_field_prefix)

OPUS5 = 'yuju-claude-opus-5-evaDaily'
_TOOLS = [{'type': 'function',
           'function': {'name': 'run_command', 'description': 'r',
                        'parameters': {'type': 'object'}}}]


def _unsigned_tool_turn():
    """The exact shape that killed turns in production: assistant + tool_calls
    + reasoning_content and NO signature (what the gateway gives us for Opus 5)."""
    return {'role': 'assistant', 'content': 'let me check',
            'tool_calls': [{'id': 'c1', 'type': 'function',
                            'function': {'name': 'run_command',
                                         'arguments': '{}'}}],
            'reasoning_content': 'unsigned internal reasoning'}


# ═══════════════════════════════════════════════════════════════════════════
#  1. THE FIX: unsigned thinking never reaches the Claude wire
# ═══════════════════════════════════════════════════════════════════════════

def test_unsigned_reasoning_content_stripped_for_claude():
    msgs = [_unsigned_tool_turn()]
    _inject_claude_reasoning_details(msgs, OPUS5)
    assert 'reasoning_content' not in msgs[0], (
        'unsigned reasoning_content survived the Claude wire tweaks — this is '
        'the exact shape upstream 400s on (signature: Field required)')
    # The rest of the turn must survive intact: only the unverifiable trace goes.
    assert msgs[0].get('content') == 'let me check'
    assert msgs[0].get('tool_calls'), 'tool_calls must be preserved'


def test_unsigned_reasoning_content_stripped_through_build_body():
    """End-to-end: the outgoing request body carries no unsigned thinking."""
    msgs = [{'role': 'system', 'content': 'sys'},
            {'role': 'user', 'content': 'go'},
            _unsigned_tool_turn(),
            {'role': 'tool', 'tool_call_id': 'c1', 'content': 'r'}]
    body = build_body(OPUS5, msgs, tools=copy.deepcopy(_TOOLS),
                      max_tokens=512, thinking_enabled=True)
    for m in body['messages']:
        if m.get('role') == 'assistant':
            assert 'reasoning_content' not in m, (
                f'build_body emitted unsigned reasoning_content to {OPUS5}')


# ═══════════════════════════════════════════════════════════════════════════
#  2. Signed path unchanged
# ═══════════════════════════════════════════════════════════════════════════

def test_signed_thinking_still_synthesises_reasoning_details():
    m = {'role': 'assistant', 'content': 'x',
         'reasoning_content': 'deep thought', 'thinking_signature': 'SIG=='}
    msgs = [m]
    _inject_claude_reasoning_details(msgs, OPUS5)
    assert m.get('reasoning_details') == [{
        'type': 'thinking', 'thinking': 'deep thought', 'signature': 'SIG=='}]
    assert m.get('reasoning_content') == 'deep thought'


def test_existing_reasoning_details_untouched():
    rd = [{'type': 'thinking', 'thinking': 't', 'signature': 'S'}]
    m = {'role': 'assistant', 'content': 'x',
         'reasoning_content': 't', 'reasoning_details': rd}
    msgs = [m]
    _inject_claude_reasoning_details(msgs, OPUS5)
    assert m['reasoning_details'] is rd
    assert m.get('reasoning_content') == 't'


# ═══════════════════════════════════════════════════════════════════════════
#  3. Non-Claude untouched (DeepSeek must-replay counter-rule)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('model', ['kimi-k3', 'deepseek-v4-pro', 'glm-5v-turbo'])
def test_non_claude_keeps_unsigned_reasoning_content(model):
    msgs = [_unsigned_tool_turn()]
    _inject_claude_reasoning_details(msgs, model)
    assert msgs[0].get('reasoning_content') == 'unsigned internal reasoning', (
        f'{model}: the Claude-only strip leaked to a non-Claude model — '
        'DeepSeek-family APIs 400 when reasoning_content is REMOVED '
        '(model_requires_reasoning_content_replay)')


# ═══════════════════════════════════════════════════════════════════════════
#  4. Cache parity: live tail vs conv replay stay byte-identical AFTER strip
# ═══════════════════════════════════════════════════════════════════════════

def _wire_bytes(asst_msg):
    head = [{'role': 'system', 'content': 'S' * 80},
            {'role': 'user', 'content': 'go'}]
    msgs = head + [copy.deepcopy(asst_msg),
                   {'role': 'tool', 'tool_call_id': 'c1', 'content': 'r'}]
    body = build_body(OPUS5, msgs, tools=copy.deepcopy(_TOOLS),
                      max_tokens=512, thinking_enabled=True)
    body['_task_id'] = 't'
    add_cache_breakpoints(body, '')
    return wire_byte_field_prefix(body['messages'])


def test_live_vs_replay_byte_identical_after_strip():
    """The strip fires identically on the live-tail shape and the conv-replay
    reconstruction, so an already-cached prefix does NOT flip bytes round to
    round (the {reasoning_content} cache-miss class must not return)."""
    live = _unsigned_tool_turn()  # live tail: reasoning_content, no sig
    reco = _reconstruct_tool_call_messages([{
        'toolCallId': 'c1', 'toolName': 'run_command', 'status': 'done',
        'toolContent': 'r', 'assistantContent': 'let me check',
        'thinking': 'unsigned internal reasoning', 'llmRound': 1}])
    replay = [m for m in reco if m.get('role') == 'assistant'][0]
    b_live, b_replay = _wire_bytes(live), _wire_bytes(replay)
    shared = min(len(b_live), len(b_replay))
    diff = diff_byte_field_prefix(b_live[:shared], b_replay[:shared])
    assert not diff, (
        f'live vs replay diverge after the strip — the cache-flip class is '
        f'back: {diff}')


# ═══════════════════════════════════════════════════════════════════════════
#  5. NEUTER: pre-fix behaviour reproduces the lethal wire shape
# ═══════════════════════════════════════════════════════════════════════════

def test_neuter_prefix_behavior_emits_unsigned_thinking():
    """Emulate the PRE-FIX tweak (synthesise reasoning_details when signed;
    leave unsigned reasoning_content in place) and prove the lethal shape
    reaches the wire — i.e. the strip branch is load-bearing.  If this stops
    reproducing, the upstream contract changed and the strip can be revisited."""
    def _prefix_tweak(messages, model):
        from lib.model_info import is_claude
        if not is_claude(model):
            return
        for msg in messages:
            if msg.get('role') != 'assistant' or msg.get('reasoning_details'):
                continue
            t, s = msg.get('reasoning_content') or '', msg.get('thinking_signature') or ''
            if t and s:
                msg['reasoning_details'] = [
                    {'type': 'thinking', 'thinking': t, 'signature': s}]

    msgs = [_unsigned_tool_turn()]
    _prefix_tweak(msgs, OPUS5)
    assert msgs[0].get('reasoning_content'), (
        'pre-fix emulation no longer emits unsigned thinking — premise changed, '
        'revisit whether the strip is still needed')
