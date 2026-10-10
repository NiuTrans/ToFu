"""lib/llm/body/_canonical_wire.py — canonical wire-order normalization.

Why this exists (the class-③ prefix-cache miss root fix)
========================================================
The wire bytes a request sends are ``json.dumps(body, sort_keys=False)`` — so
the KEY INSERTION ORDER of each message dict is part of the bytes the gateway
prompt-cache matches on. Two code paths build a semantically-identical
assistant/tool_call message with DIFFERENT key order:

  * live stream  (``lib/llm/_sse_core.py`` finalize):
        role → reasoning_content → thinking_signature → tool_calls → content
  * history replay (``lib/tasks_pkg/conv_message_builder/_toolcalls.py``):
        role → tool_calls → content → reasoning_content → thinking_signature

So the SAME already-cached turn is re-serialized with different bytes on the
next round — the ``WIRE PREFIX CHANGED`` / ``WIRE BYTES DIVERGED while canonical
fingerprint matched`` signature: the semantic (canonical) fingerprint matches,
but the raw bytes differ, so the gateway sees a changed prefix and re-writes
the whole thing (a full ``cache_creation`` bill, read collapses to the floor).
Observed systematically, every round, in fast tool-loop conversations — the
dominant residual floor-miss after the write-visibility settle + byte-freeze
fixes.

The fix (per the send-time-canonicalization directive)
======================================================
Do NOT hand-align the two build paths (fragile — the next new field re-drifts).
Instead, as the LAST message transform before the body is handed to transport,
rewrite EACH message dict with keys inserted in ONE canonical order (recursively
for the ``tool_calls`` entries and their nested ``function``). Values are copied
verbatim — only the key ORDER is normalized — so ``json.dumps(sort_keys=False)``
emits byte-identical output regardless of which path built the message.

This is intentionally value-preserving and order-only: it never adds, drops, or
rewrites a value, so it cannot change what the model sees — only the byte layout
the cache matches on. Unknown/future keys are appended in sorted order after the
known ones, so a new field added tomorrow stays deterministic without a code
change here.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['canonicalize_message_order', 'canonicalize_messages_inplace']

# Canonical key order for a message dict. Anything not listed is appended after,
# sorted, so the layout is deterministic even for keys we don't know about yet.
_MESSAGE_KEY_ORDER = (
    'role',
    'content',
    'reasoning_content',
    'thinking_signature',
    'reasoning_details',
    'tool_calls',
    'tool_call_id',
    'name',
    'extra_content',
)

# Canonical key order for a single tool_call entry and its nested function.
_TOOLCALL_KEY_ORDER = ('id', 'type', 'function', 'extra_content')
_FUNCTION_KEY_ORDER = ('name', 'arguments')
# Canonical key order for a reasoning_details block (OpenRouter thinking shape).
_REASONING_DETAIL_KEY_ORDER = ('type', 'thinking', 'signature')


def _reorder(d: dict, order: tuple[str, ...]) -> dict:
    """Return a NEW dict with ``order`` keys first (when present), then any
    remaining keys sorted. Values are copied by reference (order-only)."""
    out: dict = {}
    for k in order:
        if k in d:
            out[k] = d[k]
    for k in sorted(d):
        if k not in out:
            out[k] = d[k]
    return out


def canonicalize_message_order(msg: dict) -> dict:
    """Return a NEW message dict with keys in canonical wire order.

    Recurses into ``tool_calls`` (and each entry's nested ``function``) so a
    tool_call built by different paths serializes to identical bytes. Values
    are otherwise untouched. Non-dict input is returned unchanged."""
    if not isinstance(msg, dict):
        return msg
    # Normalize reasoning_details inner blocks (same semantic thinking block is
    # built with different key order by the live vs replay paths).
    rd = msg.get('reasoning_details')
    if isinstance(rd, list):
        msg = dict(msg)
        msg['reasoning_details'] = [
            _reorder(b, _REASONING_DETAIL_KEY_ORDER) if isinstance(b, dict) else b
            for b in rd
        ]
    tcs = msg.get('tool_calls')
    if isinstance(tcs, list):
        new_tcs = []
        for tc in tcs:
            if isinstance(tc, dict):
                tc2 = _reorder(tc, _TOOLCALL_KEY_ORDER)
                fn = tc2.get('function')
                if isinstance(fn, dict):
                    tc2['function'] = _reorder(fn, _FUNCTION_KEY_ORDER)
                new_tcs.append(tc2)
            else:
                new_tcs.append(tc)
        # Reorder the message first, then overwrite tool_calls with the
        # per-entry-canonicalized list (keeping the canonical slot position).
        out = _reorder(msg, _MESSAGE_KEY_ORDER)
        out['tool_calls'] = new_tcs
        return out
    return _reorder(msg, _MESSAGE_KEY_ORDER)


def canonicalize_messages_inplace(messages: list) -> None:
    """Replace each dict in ``messages`` with its canonical-key-order form.

    In-place (mutates the list entries) so it slots in as the final message
    transform in ``build_body`` without changing the list identity. No-op on
    non-list / empty input or non-dict entries."""
    if not isinstance(messages, list):
        return
    for i, msg in enumerate(messages):
        if isinstance(msg, dict):
            messages[i] = canonicalize_message_order(msg)
