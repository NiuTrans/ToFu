"""tests/test_empty_assistant_ghost_wire.py — strict-provider empty-assistant guard.

Root cause this guards (production, 2026-07-25): three conversations
(mrwu8ml12rdpuc / mrnem0a0jatj95 / mrnejm4zdfe5ba) became UNRETRYABLE on
kimi-k3. Their stored history carried ERROR GHOSTS — assistant rows with
``content=''`` + an error envelope, persisted so the UI has a failure bubble
to render. ``_build_assistant_messages`` emitted each ghost onto the wire as
``{'role': 'assistant', 'content': ''}``; Kimi/Moonshot HARD-400s the whole
request (``the message at position N with role 'assistant' must not be
empty`` — positions 12/32/30 matched the ghosts exactly, 1-based). HTTP 400
is non-retryable, so every retry died on round 1 deterministically.
Anthropic rejects empty non-trailing assistant turns the same way.

Two-layer fix pinned here:
  * Producer layer — ``_build_assistant_messages`` returns ``[]`` for a row
    with no rounds / no content / no toolSummary (ghost + thinking-only:
    stored ``thinking`` is never replayed as wire ``reasoning_content`` on
    plain turns, so it would serialize as the same empty ghost).
  * Defense-in-depth — ``_drop_empty_assistant_messages`` in llm_sanitize,
    wired into BOTH ``build_body`` and ``apply_wire_sanitize`` at the same
    pipeline position (before the merge), catching any future producer.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest \
     tests/test_empty_assistant_ghost_wire.py -p no:cacheprovider
"""

import pytest

pytestmark = pytest.mark.unit

from lib.llm.body import build_body
from lib.llm_sanitize import _drop_empty_assistant_messages
from lib.tasks_pkg.conv_message_builder._transform import (
    _build_assistant_messages,
    _transform_messages,
)
from lib.tasks_pkg.wire_messages import apply_wire_sanitize

# The exact row shape persisted by manager/_sync.py for a failed task
# (dumped verbatim from the three production conversations).
_GHOST = {
    'role': 'assistant', 'content': '', 'thinking': '',
    'error': {'kind': 'endpoint_unreachable',
              'message': '⚠️ 模型调用失败 / LLM call failed'},
    'finishReason': 'error', '_taskId': 'mrx123', '_msgId': 'mrx123',
}


def _empty_assistants(msgs):
    return [m for m in msgs
            if m.get('role') == 'assistant' and not m.get('tool_calls')
            and (m.get('content') in ('', None) or m.get('content') == [])]


# ═══════════════════════════════════════════════════════════════════════════
#  Producer layer — _build_assistant_messages / _transform_messages
# ═══════════════════════════════════════════════════════════════════════════

def test_error_ghost_between_users_dropped_and_users_merged():
    """The production shape: user, GHOST, user → ghost gone, users merged."""
    raw = [
        {'role': 'user', 'content': 'first question', 'timestamp': 1},
        dict(_GHOST),
        {'role': 'user', 'content': 'retry the task', 'timestamp': 2},
    ]
    wire = _transform_messages(raw, {})
    assert _empty_assistants(wire) == []
    users = [m for m in wire if m['role'] == 'user']
    assert len(users) == 1, f'neighbour users not merged after drop: {users}'
    assert users[0]['content'] == 'first question\n\nretry the task'


def test_multiple_ghosts_all_dropped():
    """mrwu8ml12rdpuc had FOUR ghosts (one per failed retry) — all must go."""
    raw = []
    for i in range(4):
        raw.append({'role': 'user', 'content': f'attempt {i}', 'timestamp': i})
        raw.append(dict(_GHOST))
    # Trailing ghost is sliced off by the pre-existing trailing-exclusion;
    # the three buried ones must be dropped by the fix.
    wire = _transform_messages(raw, {})
    assert _empty_assistants(wire) == []
    assert all(m['role'] == 'user' for m in wire)


def test_thinking_only_row_dropped():
    """A thinking-only row serializes as the same empty ghost on the wire
    (stored thinking is not replayed for plain turns) → drop it too."""
    msg = {'role': 'assistant', 'content': '', 'thinking': 'deep thoughts'}
    assert _build_assistant_messages(msg) == []


def test_ghost_with_tool_summary_kept():
    """A no-rounds row with a legacy toolSummary still informs the model —
    it must survive (it is NOT empty)."""
    msg = {'role': 'assistant', 'content': '', 'thinking': '',
           'toolSummary': '[{"name": "web_search", "query": "x"}]'}
    out = _build_assistant_messages(msg)
    assert out == [{'role': 'assistant', 'content': msg['toolSummary']}]


def test_normal_assistant_row_untouched():
    msg = {'role': 'assistant', 'content': 'real answer', 'thinking': 'th'}
    assert _build_assistant_messages(msg) == [
        {'role': 'assistant', 'content': 'real answer'}]


def test_tool_rounds_row_still_reconstructs():
    """Rows with toolRounds are unaffected — structured reconstruction with
    tool_calls must survive byte-identical semantics."""
    msg = {
        'role': 'assistant', 'content': 'done', 'thinking': '',
        'toolRounds': [{'llmRound': 1, 'roundNum': 1, 'toolCallId': 'c1',
                        'toolName': 'run_command',
                        'toolArgs': '{"command": "ls"}',
                        'toolContent': 'ok', 'status': 'done'}],
    }
    out = _build_assistant_messages(msg)
    roles = [m['role'] for m in out]
    assert roles[0] == 'assistant' and 'tool' in roles
    assert out[0].get('tool_calls'), f'tool_calls lost: {out[0]}'
    assert out[-1] == {'role': 'assistant', 'content': 'done'}


# ═══════════════════════════════════════════════════════════════════════════
#  Defense-in-depth — _drop_empty_assistant_messages
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('content', ['', '   \n  ', None, [],
                                     [{'type': 'text', 'text': '  '}]])
def test_guard_drops_pure_ghost_shapes(content):
    msgs = [{'role': 'user', 'content': 'q'},
            {'role': 'assistant', 'content': content},
            {'role': 'user', 'content': 'q2'}]
    out = _drop_empty_assistant_messages(msgs)
    assert [m['role'] for m in out] == ['user', 'user']
    # input untouched (returns a new list)
    assert len(msgs) == 3


@pytest.mark.parametrize('extra', [
    {'tool_calls': [{'id': 'c1', 'type': 'function',
                     'function': {'name': 'f', 'arguments': '{}'}}]},
    {'function_call': {'name': 'f', 'arguments': '{}'}},
    {'reasoning_content': 'thinking…'},
    {'reasoning_details': [{'type': 'thinking', 'thinking': 't',
                            'signature': 'S'}]},
    {'thinking_signature': 'S'},
])
def test_guard_keeps_tool_and_reasoning_turns(extra):
    """Empty-content assistant WITH tool/reasoning fields is NOT a ghost —
    dropping it would break tool adjacency / thinking replay."""
    msg = dict({'role': 'assistant', 'content': ''}, **extra)
    out = _drop_empty_assistant_messages([msg])
    assert out == [msg]


def test_guard_keeps_content_and_other_roles():
    msgs = [{'role': 'system', 'content': ''},          # not assistant — untouched
            {'role': 'user', 'content': 'q'},
            {'role': 'assistant', 'content': 'a'},
            {'role': 'tool', 'content': ''}]           # tool empties are a
                                                       # different fixer's job
    out = _drop_empty_assistant_messages(msgs)
    assert out == msgs


# ═══════════════════════════════════════════════════════════════════════════
#  End-to-end — build_body (the kimi-k3 path that 400'd) + wire parity
# ═══════════════════════════════════════════════════════════════════════════

def test_build_body_kimi_k3_has_no_empty_assistant():
    """The exact failing path: transform → build_body('kimi-k3')."""
    raw = [
        {'role': 'user', 'content': 'first question', 'timestamp': 1},
        dict(_GHOST),
        {'role': 'user', 'content': 'retry the task', 'timestamp': 2},
    ]
    msgs = _transform_messages(raw, {})
    body = build_body('kimi-k3', msgs, max_tokens=1024)
    assert _empty_assistants(body['messages']) == []
    users = [m for m in body['messages'] if m['role'] == 'user']
    assert len(users) == 1


def test_build_body_keeps_tool_call_round_with_empty_content():
    """Regression guard: content='' + tool_calls (the normal tool round!)
    must survive build_body — only pure ghosts are dropped."""
    msgs = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': '',
         'tool_calls': [{'id': 'c1', 'type': 'function',
                         'function': {'name': 'f', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result'},
        {'role': 'user', 'content': 'next'},
    ]
    body = build_body('kimi-k3', msgs, max_tokens=1024)
    assistants = [m for m in body['messages'] if m['role'] == 'assistant']
    assert len(assistants) == 1 and assistants[0].get('tool_calls')


def test_apply_wire_sanitize_drops_ghost_parity():
    """The debug-panel cold path drops ghosts exactly like build_body —
    the documented parity contract holds."""
    msgs = [
        {'role': 'user', 'content': 'q'},
        {'role': 'assistant', 'content': ''},
        {'role': 'user', 'content': 'q2'},
    ]
    out = apply_wire_sanitize(msgs, conv_id='')
    assert _empty_assistants(out) == []
    users = [m for m in out if m['role'] == 'user']
    assert len(users) == 1 and users[0]['content'] == 'q\n\nq2'


# ═══════════════════════════════════════════════════════════════════════════
#  NEUTER — prove the two drops are the discriminator (causal proof)
# ═══════════════════════════════════════════════════════════════════════════

def test_neuter_transform_restores_ghost():
    """Revert ONLY the producer drop in a scratch copy of the real module →
    the empty assistant comes back on the wire. Proves the new ``return []``
    is what heals strict-provider requests (failing-first, in-suite)."""
    import lib.tasks_pkg.conv_message_builder._transform as real_mod
    with open(real_mod.__file__, encoding='utf-8') as f:
        src = f.read()
    anchor = ("        logger.info('[MsgBuilder] Dropping empty assistant row "
              "from wire '\n                    '(error=%s thinking=%dchars) "
              "— strict providers 400 on it',\n                    "
              "bool(msg.get('error')), len(final_thinking))\n        return []")
    assert anchor in src, 'neuter anchor drifted — update the test'
    neutered = src.replace(anchor,
                           "        return [{'role': 'assistant', 'content': ''}]")
    ns = dict(real_mod.__dict__)
    exec(compile(neutered, real_mod.__file__, 'exec'), ns)
    out = ns['_build_assistant_messages'](dict(_GHOST))
    assert out == [{'role': 'assistant', 'content': ''}], (
        f'neutered module did not restore the ghost: {out}')
    # …while the REAL function drops it.
    assert _build_assistant_messages(dict(_GHOST)) == []


def test_neuter_sanitize_guard_restores_ghost():
    """Revert ONLY the sanitize guard's filtering in a scratch copy → the
    ghost survives. Proves the guard (not luck) strips it in build_body."""
    import lib.llm_sanitize._messages as real_mod
    with open(real_mod.__file__, encoding='utf-8') as f:
        src = f.read()
    anchor = ("    kept = []\n    dropped = 0\n    for msg in messages:\n"
              "        if _is_ghost(msg):\n            dropped += 1\n"
              "            continue\n        kept.append(msg)")
    assert anchor in src, 'neuter anchor drifted — update the test'
    neutered = src.replace(anchor,
                           "    kept = list(messages)\n    dropped = 0")
    ns = dict(real_mod.__dict__)
    exec(compile(neutered, real_mod.__file__, 'exec'), ns)
    ghost = {'role': 'assistant', 'content': ''}
    out = ns['_drop_empty_assistant_messages']([ghost])
    assert out == [ghost], f'neutered guard did not pass the ghost through: {out}'
    assert _drop_empty_assistant_messages([ghost]) == []
