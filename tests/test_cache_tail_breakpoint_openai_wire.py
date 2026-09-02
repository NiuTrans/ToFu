"""tests/test_cache_tail_breakpoint_openai_wire.py — PIN a known, unfixed defect.

WHAT IS PINNED
==============
On a NON-Anthropic wire, an agent tool loop loses its conversation-TAIL cache
breakpoint whenever the trailing assistant turn carries ``tool_calls`` with
EMPTY ``content`` — the shape a model produces when it calls a tool without
writing prose first. Only 2 of the 4 markers land (system + last tool
definition); the rolling tail marker, which is the single highest-value one
because it covers the growing prefix, is silently dropped.

Consequence: whether a round can cache its conversation body depends on whether
the model happened to narrate before calling a tool. That is not a property any
cache strategy should hinge on.

WHY IT IS UNFIXED — both routes are closed, each for a DIFFERENT reason
=======================================================================
Route A — mark the assistant turn on the OpenAI wire. **Closed by the OpenAI
  schema itself**, which is a repo-verifiable fact, not a gateway behaviour:
  ``prepare_request`` (lib/llm/_sse_core.py) only invokes
  ``openai_body_to_anthropic`` when ``api_protocol == 'anthropic'``; on every
  other wire the body is serialised VERBATIM. In that body an assistant turn's
  ``content`` is a plain ``str`` and ``tool_calls`` is a list of
  ``{id, type, function{name, arguments}}`` function-call descriptors — there is
  no content BLOCK anywhere to hang ``cache_control`` on. The marker-hoisting
  logic that would rescue it (``_assistant_blocks`` in
  lib/llm/anthropic_outbound/) lives on the Anthropic path and never runs here.
  Fabricating an empty ``[{'type':'text','text':''}]`` block is explicitly
  rejected in lib/llm/cache.py Phase 0.5 (it would be a text block the model
  never wrote, and it re-introduces the str↔block representation flip that
  Phase 0.5 exists to eliminate).

Route B — switch this provider to the Anthropic wire. **NOT settled.** Probing
  the gateway produced mutually contradictory results across several rounds,
  including a physically impossible ``prompt_tokens=2`` on a 2000+ token body,
  which means the endpoint short-circuits or de-duplicates in a way that makes
  black-box cache measurement from outside unreliable. No claim about that
  endpoint's cache behaviour is asserted here, in either direction. Deciding it
  needs the gateway team's internal observability, not more outside probing.
  (Note for whoever picks this up: the base_url in
  static/provider_templates/meituan_claude_code.json is ``/v1/anthropic``, which
  returns 404 — the reachable path has an extra segment. That template has never
  served live traffic.)

SO: this file asserts ONLY the client-side marker arithmetic, which is pure
local logic. It deliberately makes NO assertion about hit rates, cached_tokens,
or any gateway response. If a future change makes the tail marker land in the
empty-content case, `test_openai_wire_loses_the_tail_breakpoint` fails — that is
the intended signal to delete this file, not to update the number.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_cache_tail_breakpoint_openai_wire.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.llm.cache import add_cache_breakpoints  # noqa: E402

pytestmark = pytest.mark.unit

_MODEL = 'yuju-claude-opus-5-evaDaily'


def _count_markers(body: dict) -> int:
    """Total cache_control markers on the body (messages + tool defs)."""
    n = 0
    for msg in body.get('messages') or []:
        content = msg.get('content')
        if isinstance(content, list):
            n += sum(1 for b in content
                     if isinstance(b, dict) and 'cache_control' in b)
    for tool in body.get('tools') or []:
        if 'cache_control' in (tool.get('function') or {}):
            n += 1
    return n


def _marked_roles(body: dict) -> list[str]:
    out = []
    for msg in body.get('messages') or []:
        content = msg.get('content')
        if isinstance(content, list) and any(
                isinstance(b, dict) and 'cache_control' in b for b in content):
            out.append(msg.get('role'))
    for tool in body.get('tools') or []:
        if 'cache_control' in (tool.get('function') or {}):
            out.append('tool_def')
    return out


def _tool_loop(*, rounds: int = 10, prose: bool) -> dict:
    """An agent tool loop. ``prose=False`` is the shape that loses the marker:
    assistant turns carry tool_calls with empty content."""
    messages = [{'role': 'system', 'content': 'S' * 400},
                {'role': 'user', 'content': 'go'}]
    for i in range(rounds):
        messages.append({
            'role': 'assistant',
            'content': 'Let me check that.' if prose else '',
            'tool_calls': [{'id': 'c%d' % i, 'type': 'function',
                            'function': {'name': 'read_files',
                                         'arguments': '{}'}}],
        })
        messages.append({'role': 'tool', 'tool_call_id': 'c%d' % i,
                         'content': 'RESULT ' * 60})
    return {
        'model': _MODEL,
        'messages': messages,
        'tools': [{'type': 'function',
                   'function': {'name': 'read_files', 'description': 'read',
                                'parameters': {'type': 'object',
                                               'properties': {}}}}],
    }


# ── The pinned defect ───────────────────────────────────────────────────

def test_openai_wire_loses_the_tail_breakpoint():
    """KNOWN DEFECT, pinned deliberately: 2 markers instead of 3.

    If this fails because the count rose to 3, the defect was FIXED — delete
    this file rather than updating the expected number.
    """
    body = _tool_loop(prose=False)
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _count_markers(body) == 2, (
        'expected the known-defective 2 markers, got %d (roles=%s)'
        % (_count_markers(body), _marked_roles(body)))
    assert 'assistant' not in _marked_roles(body)
    assert 'tool' not in _marked_roles(body)


def test_the_same_loop_with_prose_keeps_its_tail_breakpoint():
    """The defect is shape-dependent: one sentence of prose recovers the marker.

    This contrast IS the bug report — cache coverage must not depend on whether
    the model narrated before calling a tool.
    """
    body = _tool_loop(prose=True)
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _count_markers(body) == 3
    assert 'assistant' in _marked_roles(body)


def test_anthropic_wire_marker_arithmetic_is_unaffected():
    """REGRESSION GUARD on the client-side arithmetic only.

    On the Anthropic protocol a ``tool`` message IS markable (our translator
    hoists the marker onto the emitted tool_result block), so the tail marker
    lands in BOTH shapes. Asserted because any change to the protocol gate in
    add_cache_breakpoints would silently alter it. This says nothing about what
    the gateway does with those markers.
    """
    for prose in (True, False):
        body = _tool_loop(prose=prose)
        add_cache_breakpoints(body, log_prefix='', api_protocol='anthropic')
        assert _count_markers(body) == 3, (
            'anthropic wire, prose=%s: expected 3 markers, got %d'
            % (prose, _count_markers(body)))


def test_assistant_tool_calls_carry_no_content_block_to_mark():
    """The OpenAI assistant schema offers nowhere to put cache_control.

    ``content`` is a bare str and ``tool_calls`` entries are function-call
    descriptors, not content blocks. This is the schema fact that closes route A.
    """
    body = _tool_loop(rounds=3, prose=False)
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    tails = [m for m in body['messages'] if m.get('role') == 'assistant']
    assert tails, 'fixture should contain assistant turns'
    for msg in tails:
        assert isinstance(msg.get('content'), str), (
            'empty assistant content must stay a bare str — fabricating a text '
            'block is rejected by cache.py Phase 0.5')
        for tc in msg.get('tool_calls') or []:
            assert set(tc.keys()) <= {'id', 'type', 'function'}, (
                'tool_calls entry grew a field: %s — if a content-block-like '
                'slot now exists, route A may be reopenable' % sorted(tc))
