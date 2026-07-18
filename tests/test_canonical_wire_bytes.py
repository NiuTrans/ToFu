"""Tests for lib/llm/body/_canonical_wire.py — canonical wire-order normalization.

Guards the class-③ prefix-cache root fix: a semantically-identical
assistant/tool_call message built by the LIVE-STREAM path and by the
HISTORY-REPLAY path (which insert keys in different orders) must serialize to
BYTE-IDENTICAL wire bytes after canonicalization, so an already-cached turn is
not re-written every round (the WIRE PREFIX CHANGED / WIRE BYTES DIVERGED
signature).

Pure functions — no DB, no network.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit

from lib.llm.body._canonical_wire import (
    canonicalize_message_order,
    canonicalize_messages_inplace,
)


def _wire(msg: dict) -> str:
    """Serialize exactly as the transport does (insertion order preserved)."""
    return json.dumps(msg, ensure_ascii=False, sort_keys=False)


# The SAME semantic assistant/tool_call turn, built two ways.
def _live_shape() -> dict:
    """Key order as _sse_core.py finalize builds it:
    role → reasoning_content → thinking_signature → tool_calls → content."""
    return {
        'role': 'assistant',
        'reasoning_content': 'let me think',
        'thinking_signature': 'sig-abc',
        'tool_calls': [{
            'id': 'call_1', 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{"path": "x.py"}'},
        }],
        'content': 'reading now',
        'reasoning_details': [{
            'type': 'thinking', 'thinking': 'let me think', 'signature': 'sig-abc',
        }],
    }


def _replay_shape() -> dict:
    """Key order as _toolcalls.py replay builds it:
    role → tool_calls → content → reasoning_content → thinking_signature,
    and the nested function/tool_call keys in a different order too."""
    return {
        'role': 'assistant',
        'tool_calls': [{
            'function': {'arguments': '{"path": "x.py"}', 'name': 'read_files'},
            'type': 'function', 'id': 'call_1',
        }],
        'content': 'reading now',
        'reasoning_content': 'let me think',
        'thinking_signature': 'sig-abc',
        'reasoning_details': [{
            'signature': 'sig-abc', 'thinking': 'let me think', 'type': 'thinking',
        }],
    }


def test_divergent_shapes_diverge_without_canon():
    """Precondition: the two build shapes really DO serialize to different bytes
    (this is the bug). If this ever stops holding, the fix guards nothing."""
    assert _wire(_live_shape()) != _wire(_replay_shape())


def test_canonicalized_shapes_are_byte_identical():
    """THE fix: after canonicalization the two shapes emit identical wire bytes,
    including the nested tool_calls/function key order."""
    lb = _wire(canonicalize_message_order(_live_shape()))
    rb = _wire(canonicalize_message_order(_replay_shape()))
    assert lb == rb, f'canonical bytes differ:\n live={lb}\n repl={rb}'


def test_NEUTER_skip_canon_still_diverges():
    """NEUTER: skipping canonicalization (identity) leaves the bytes divergent —
    proving the normalization is what makes them equal, not something else."""
    def _identity(m):
        return m
    lb = _wire(_identity(_live_shape()))
    rb = _wire(_identity(_replay_shape()))
    assert lb != rb, 'NEUTER: without canon the two shapes must still diverge'


def test_values_preserved_exactly():
    """Order-only: canonicalization must NOT add, drop, or change any value —
    only reorder keys. The model must see exactly the same message."""
    src = _live_shape()
    out = canonicalize_message_order(src)
    # Same key set, same values (compare as order-insensitive structures).
    assert set(out) == set(src)
    assert out == src  # dict equality is order-insensitive
    assert out['tool_calls'][0]['function']['arguments'] == '{"path": "x.py"}'
    assert out['reasoning_details'] == src['reasoning_details']


def test_canonical_key_order_is_fixed():
    """The emitted key order is the fixed canonical sequence, regardless of input
    order — so both build paths converge on it."""
    keys = list(canonicalize_message_order(_replay_shape()).keys())
    # role first, content next, thinking fields, then tool_calls.
    assert keys[0] == 'role'
    assert keys.index('content') < keys.index('tool_calls')
    assert keys.index('reasoning_content') < keys.index('tool_calls')


def test_unknown_future_key_is_deterministic():
    """A key we don't know about is appended in sorted order after the known
    ones — deterministic, so a future field can't silently reintroduce drift."""
    a = {'role': 'assistant', 'content': 'x', 'zeta_future': 1, 'alpha_future': 2}
    b = {'alpha_future': 2, 'content': 'x', 'zeta_future': 1, 'role': 'assistant'}
    assert _wire(canonicalize_message_order(a)) == _wire(canonicalize_message_order(b))
    keys = list(canonicalize_message_order(a).keys())
    assert keys == ['role', 'content', 'alpha_future', 'zeta_future']


def test_non_dict_and_plain_messages_untouched():
    """Non-dict entries pass through; a plain user string message is stable."""
    assert canonicalize_message_order('not a dict') == 'not a dict'
    user = {'role': 'user', 'content': 'hello'}
    assert canonicalize_message_order(user) == user


def test_inplace_helper_normalizes_list():
    """canonicalize_messages_inplace rewrites each dict entry in place and makes
    a live+replay pair converge; non-list input is a no-op."""
    msgs = [_replay_shape(), {'role': 'user', 'content': 'hi'}]
    canonicalize_messages_inplace(msgs)
    assert _wire(msgs[0]) == _wire(canonicalize_message_order(_live_shape()))
    assert msgs[1] == {'role': 'user', 'content': 'hi'}
    # no-op on non-list
    canonicalize_messages_inplace(None)  # must not raise


def test_build_body_applies_canonicalization():
    """End-to-end: build_body emits canonical key order so the two shapes,
    fed through the real builder, produce byte-identical message bytes."""
    from lib.llm.body import build_body
    live = build_body('aws.claude-opus-4.8', [_live_shape(),
                      {'role': 'user', 'content': 'go'}])
    replay = build_body('aws.claude-opus-4.8', [_replay_shape(),
                        {'role': 'user', 'content': 'go'}])
    # The assistant/tool_call message (index 0) must be byte-identical.
    assert _wire(live['messages'][0]) == _wire(replay['messages'][0])
