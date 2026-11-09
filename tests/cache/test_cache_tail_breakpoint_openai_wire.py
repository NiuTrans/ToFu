"""tests/cache/test_cache_tail_breakpoint_openai_wire.py — tail breakpoint in OpenAI-wire tool loops.

WHAT IS SPECIFIED
=================
On a NON-Anthropic wire two turn shapes cannot carry ``cache_control``:

* ``tool`` results — the gateway's OpenAI→Anthropic translation would carry
  the marker into ``tool_result.content`` (vendor HTTP 400, see
  test_cache_tool_marker_protocol.py);
* assistant turns that call a tool WITHOUT prose — there is no content block
  to hang a marker on (route A below).

An agent tool loop is a run of exactly those two shapes, so the nearest
markable turn (the latest user turn, or the latest assistant turn that wrote
prose) can sit arbitrarily far behind the tail. The tail scan therefore walks
back until it finds one, stopping at a system message. The former fixed
5-message window dropped the conversation breakpoint outright once a loop held
>= 5 such messages (three no-prose rounds, or one fan-out of five parallel tool
calls): only the system + tool-definition markers landed, so whether a round
could cache its conversation body hinged on whether the model happened to
narrate before calling a tool.

The Anthropic wire keeps its historical placement: our translator hoists the
marker onto the tool_result / tool_use block, so the tail itself is markable.

WHY THE MARKER CANNOT SIT ON THE TAIL ITSELF
============================================
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
or any gateway response.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/cache/test_cache_tail_breakpoint_openai_wire.py -v
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from lib.llm.body._model_tweaks import CLAUDE_PREFILL_SENTINEL  # noqa: E402
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


def _marked_indexes(body: dict) -> list[int]:
    return [i for i, msg in enumerate(body.get('messages') or [])
            if isinstance(msg.get('content'), list) and any(
                isinstance(b, dict) and 'cache_control' in b
                for b in msg['content'])]


def _tool_call(i: int) -> dict:
    return {'id': 'c%d' % i, 'type': 'function',
            'function': {'name': 'read_files', 'arguments': '{}'}}


def _tool_result(i: int) -> dict:
    return {'role': 'tool', 'tool_call_id': 'c%d' % i,
            'content': 'RESULT ' * 60}


def _tool_loop(*, rounds: int = 10, prose: bool) -> dict:
    """An agent tool loop. ``prose=False`` is the shape whose trailing turns
    are ALL unmarkable on the OpenAI wire: assistant turns carry tool_calls
    with empty content."""
    messages = [{'role': 'system', 'content': 'S' * 400},
                {'role': 'user', 'content': 'go'}]
    for i in range(rounds):
        messages.append({
            'role': 'assistant',
            'content': 'Let me check that.' if prose else '',
            'tool_calls': [_tool_call(i)],
        })
        messages.append(_tool_result(i))
    return {
        'model': _MODEL,
        'messages': messages,
        'tools': [{'type': 'function',
                   'function': {'name': 'read_files', 'description': 'read',
                                'parameters': {'type': 'object',
                                               'properties': {}}}}],
    }


# ── Walk-back to the nearest markable turn ──────────────────────────────

def test_no_prose_tool_loop_keeps_its_conversation_breakpoint():
    """20 trailing unmarkable messages: the tail marker walks back past all of
    them to the user turn — system + tool def + conversation = 3 markers."""
    body = _tool_loop(prose=False)
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _count_markers(body) == 3, (
        'expected system + tool-def + conversation markers, got %d (roles=%s)'
        % (_count_markers(body), _marked_roles(body)))
    assert _marked_indexes(body) == [0, 1]
    assert 'assistant' not in _marked_roles(body)
    assert 'tool' not in _marked_roles(body)


def test_walk_back_lands_on_the_latest_markable_turn():
    """With earlier history the marker lands on the NEWEST markable turn before
    the loop (the follow-up user turn), not the first one — it must cover as
    much of the growing prefix as the wire allows."""
    body = _tool_loop(rounds=6, prose=False)
    body['messages'][2:2] = [
        {'role': 'assistant', 'content': 'Here is the plan.'},
        {'role': 'user', 'content': 'continue'},
    ]
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _marked_indexes(body) == [0, 3]


def test_parallel_tool_fan_out_keeps_the_prose_breakpoint():
    """One assistant turn fanning out to six parallel tool calls: six trailing
    tool results outrun any short window, yet the prose turn right before them
    is markable and must carry the conversation marker."""
    body = _tool_loop(rounds=0, prose=False)
    body['messages'].append({'role': 'assistant',
                             'content': 'Reading all six files.',
                             'tool_calls': [_tool_call(i) for i in range(6)]})
    body['messages'].extend(_tool_result(i) for i in range(6))
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _marked_indexes(body) == [0, 2]
    assert _count_markers(body) == 3


def test_walk_back_still_skips_a_trailing_prefill_conversion():
    """The volatile prefill-converted turn stays unmarkable however deep the
    walk goes: the marker lands on the stable user turn behind the loop."""
    body = _tool_loop(rounds=3, prose=False)
    body['messages'].append({
        'role': 'user',
        'content': CLAUDE_PREFILL_SENTINEL + '\nhalf-written answer'})
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _marked_indexes(body) == [0, 1]


def test_walk_back_stops_at_a_system_message(caplog):
    """A system message bounds the walk (the system phase owns its marker):
    the user turn BEFORE it is never chosen, and the miss is logged."""
    body = _tool_loop(rounds=5, prose=False)
    body['messages'].insert(2, {'role': 'system',
                                'content': 'Mid-conversation system note.'})
    caplog.set_level(logging.DEBUG, logger='lib.llm.cache')
    add_cache_breakpoints(body, log_prefix='[t]', api_protocol='openai')
    marked = _marked_indexes(body)
    assert [body['messages'][i]['role'] for i in marked] == ['system', 'system']
    assert 'BP4 tail breakpoint could not be placed' in caplog.text


def test_the_same_loop_with_prose_marks_the_latest_prose_turn():
    """With prose the nearest markable turn is the last assistant turn, right
    before the trailing (unmarkable) tool result."""
    body = _tool_loop(prose=True)
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    assert _count_markers(body) == 3
    assert _marked_indexes(body) == [0, len(body['messages']) - 2]
    assert 'assistant' in _marked_roles(body)


def test_anthropic_wire_marker_arithmetic_is_unaffected():
    """REGRESSION GUARD on the client-side arithmetic only.

    On the Anthropic protocol a ``tool`` message IS markable (our translator
    hoists the marker onto the emitted tool_result block), so the tail marker
    lands on the LAST message in BOTH shapes. Asserted because any change to
    the protocol gate in add_cache_breakpoints would silently alter it. This
    says nothing about what the gateway does with those markers.
    """
    for prose in (True, False):
        body = _tool_loop(prose=prose)
        add_cache_breakpoints(body, log_prefix='', api_protocol='anthropic')
        assert _count_markers(body) == 3, (
            'anthropic wire, prose=%s: expected 3 markers, got %d'
            % (prose, _count_markers(body)))
        assert _marked_indexes(body) == [0, len(body['messages']) - 1]


def test_assistant_tool_calls_carry_no_content_block_to_mark():
    """The OpenAI assistant schema offers nowhere to put cache_control.

    ``tool_calls`` entries are function-call descriptors, not content blocks —
    and since cache.py gen 7 the EMPTY ``content`` key is omitted outright
    (present-vs-absent is canonical-invisible but byte-divergent, the
    ``{content}`` floor-miss), matching the single-source builder's
    no-content shape. This is the schema fact that closes route A.
    """
    body = _tool_loop(rounds=3, prose=False)
    add_cache_breakpoints(body, log_prefix='', api_protocol='openai')
    tails = [m for m in body['messages'] if m.get('role') == 'assistant']
    assert tails, 'fixture should contain assistant turns'
    for msg in tails:
        assert 'content' not in msg, (
            'empty assistant content key must be omitted — neither a bare '
            "''/null nor a fabricated text block survives cache.py Phase 0.5 "
            f'— got {msg.get("content")!r}')
        for tc in msg.get('tool_calls') or []:
            assert set(tc.keys()) <= {'id', 'type', 'function'}, (
                'tool_calls entry grew a field: %s — if a content-block-like '
                'slot now exists, route A may be reopenable' % sorted(tc))
