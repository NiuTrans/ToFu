"""tests/test_slot_adapt_body_idempotent.py

Root-cause regression guard for the mrne3bqe R4/R5 prefix-cache miss.

REPRODUCED cause (byte level): ``_adapt_stream_body_for_slot`` on the
``is_body`` path did ``body = dict(body_or_messages)`` — a SHALLOW copy, so
``body['messages']`` aliased the task-shared messages list. The model-specific
IN-PLACE rewrites it then runs (``_strip_trailing_assistant_for_claude`` /
``_downscale_oversized_images`` / ``_inject_gemini_thought_signatures``) mutated
that shared list. On a 503/429 slot-swap the dispatch loop re-adapts the SAME
shared body for a different slot: a Gemini attempt bakes
``tool_calls[0].extra_content.google.thought_signature`` into a PREFIX
tool_call, then the recovered Claude attempt re-serializes the polluted prefix
→ different wire bytes → prompt-cache key shift → full prefix miss. On the
OpenAI transport path (sankuai gateway, ``api_protocol='openai'``) the body is
serialized VERBATIM (no Anthropic allowlist rebuild), so the pollution reaches
the wire — matching mrne3bqe (aws.claude-opus-4.8 via sankuai) hitting 503→429
slot swaps at R4/R5.

Fix: deep-copy ``body['messages']`` before the per-slot in-place rewrites so
each adaptation is idempotent and never leaks back onto the caller / next slot.

The assertions are on ACTUAL serialized bytes, and every guard has a neuter
negative control that reintroduces the shallow copy and MUST flip.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest -q \
        tests/test_slot_adapt_body_idempotent.py
"""

import copy
import json

import pytest

from lib.llm import build_body
from lib.llm_dispatch.api import _adapt_stream_body_for_slot


_MODEL = 'aws.claude-opus-4.8'


class _Slot:
    def __init__(self, model, provider_id='sankuai', thinking_format='',
                 protocol=''):
        self.model = model
        self.provider_id = provider_id
        self.thinking_format = thinking_format
        self.protocol = protocol


def _tools():
    return [{'type': 'function', 'function': {
        'name': 'grep_search', 'description': 'Search.',
        'parameters': {'type': 'object', 'properties': {'pattern': {'type': 'string'}}}}}]


def _history():
    """Tool-loop history whose assistant turn carries a signed thinking block +
    a tool_call — the shape a Gemini swap injects extra_content onto."""
    return [
        {'role': 'system', 'content': 'You are helpful.\n' + ('S' * 500)},
        {'role': 'user', 'content': 'investigate'},
        {'role': 'assistant', 'content': 'Let me search.',
         'reasoning_content': 'grep first', 'thinking_signature': 'sig-' + ('a' * 40),
         'tool_calls': [{'id': 'toolu_1', 'type': 'function',
                         'function': {'name': 'grep_search',
                                      'arguments': json.dumps({'pattern': 'x'})}}]},
        {'role': 'tool', 'tool_call_id': 'toolu_1', 'content': 'match: def x(): ...'},
        {'role': 'user', 'content': 'continue'},
    ]


def _prebuilt_body():
    body = build_body(_MODEL, _history(), max_tokens=2048, thinking_enabled=True,
                      thinking_depth='medium', tools=_tools(), stream=True,
                      provider_id='sankuai')
    body['_task_id'] = 'taskX'
    return body


def _adapt(slot, body):
    return _adapt_stream_body_for_slot(
        slot, body, True, tools=_tools(), max_tokens=2048, temperature=1.0,
        thinking_enabled=True, preset='medium', effort='medium')


def _openai_wire(body):
    """OpenAI-path wire bytes: body serialized verbatim (no Anthropic rebuild),
    _task_id stripped at the serialization boundary."""
    b = dict(body)
    b.pop('_task_id', None)
    return json.dumps({'messages': b.get('messages')}, ensure_ascii=False).encode('utf-8')


# ── Shallow-copy neuter (pre-fix behaviour) for the negative controls. ──
def _adapt_shallow(slot, body_or_messages):
    """Reconstructs the PRE-FIX shallow-copy path: dict() without the messages
    deep-copy, then the same per-slot in-place rewrites. Used only to prove the
    guards have teeth."""
    from lib.llm import (_downscale_oversized_images,
                         _strip_trailing_assistant_for_claude, is_claude)
    from lib.llm.body import (_validate_image_blocks,
                              _inject_gemini_thought_signatures)
    from lib.model_info import is_gemini as _is_gemini

    body = dict(body_or_messages)   # SHALLOW — messages aliased (the bug)
    body['model'] = slot.model
    body['tools'] = _tools()
    if is_claude(slot.model) and body.get('messages'):
        _strip_trailing_assistant_for_claude(body['messages'], slot.model)
        _validate_image_blocks(body['messages'])
        _downscale_oversized_images(body['messages'], slot.model)
    if _is_gemini(slot.model) and body.get('messages'):
        _inject_gemini_thought_signatures(body['messages'], slot.model)
    return body


@pytest.mark.unit
def test_adapt_does_not_mutate_caller_messages():
    """A per-slot adaptation must NOT mutate the caller's shared messages list."""
    body = _prebuilt_body()
    before = copy.deepcopy(body['messages'])
    _adapt(_Slot('gemini-3-pro', 'vertex'), body)   # Gemini would inject extra_content
    assert body['messages'] == before, (
        '_adapt_stream_body_for_slot mutated the caller-shared messages list — '
        'a slot-swap retry then pollutes the cached prefix')


@pytest.mark.unit
def test_adapt_returns_independent_messages_list():
    """The adapted body's messages must be a distinct object, not the caller's."""
    body = _prebuilt_body()
    adapted = _adapt(_Slot(_MODEL), body)
    assert adapted['messages'] is not body['messages'], (
        'adapted messages IS the caller list object — in-place rewrites leak')


@pytest.mark.unit
def test_openai_wire_stable_across_claude_gemini_claude_swap():
    """The exact R4/R5 sequence: Claude attempt → 503 swap to Gemini → 429
    recover to Claude, all re-adapting the SAME prebuilt body. The OpenAI-path
    wire bytes of the two Claude attempts must be byte-identical."""
    body = _prebuilt_body()
    wire_a = _openai_wire(_adapt(_Slot(_MODEL), body))
    _adapt(_Slot('gemini-3-pro', 'vertex'), body)          # 503 → gemini slot
    wire_b = _openai_wire(_adapt(_Slot(_MODEL), body))      # 429 → back to claude
    assert wire_a == wire_b, (
        'OpenAI-path prefix wire bytes flipped across a claude→gemini→claude '
        'slot swap — the prompt-cache key shifts → full prefix miss (R4/R5)')


@pytest.mark.unit
def test_neuter_shallow_copy_reintroduces_the_flip():
    """Negative control: the PRE-FIX shallow-copy path MUST pollute the caller
    and flip the OpenAI-path wire across the same swap — proving the guards bite."""
    body = _prebuilt_body()
    before = copy.deepcopy(body['messages'])
    wire_a = _openai_wire(_adapt_shallow(_Slot(_MODEL), body))
    _adapt_shallow(_Slot('gemini-3-pro', 'vertex'), body)  # mutates shared list
    wire_b = _openai_wire(_adapt_shallow(_Slot(_MODEL), body))
    assert body['messages'] != before, (
        'neuter did not mutate the caller — the repro no longer exercises the bug')
    assert wire_a != wire_b, (
        'neuter (shallow copy) did not flip the wire — the guard is not '
        'actually exercising the shared-list pollution vector')
