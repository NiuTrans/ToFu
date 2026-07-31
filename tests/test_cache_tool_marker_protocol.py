#!/usr/bin/env python3
"""tests/test_cache_tool_marker_protocol.py — tool-message cache marker protocol guard.

Production incident 2026-07-25 (``yuju-claude-opus-5-evaDaily`` via the
sankuai toio gateway, OPENAI protocol, conv task R7): ``add_cache_breakpoints``
stamps the rolling tail breakpoint on a ``tool`` message's content block.
The OpenAI wire serialises the body verbatim (``session.post(json=body)``),
so the gateway's OpenAI→Anthropic translation carries the marker INTO
``tool_result.content[*]`` — which the vendor hard-rejects::

    HTTP 400 "…tool_result.content.0.cache_control: cache_control may not be
    specified within `tool_result.content`. Instead, place it directly on
    `tool_result`"   (logs/app.log 2026-07-25 13:48, request id
    toio20260725134814178521797ycz745lq)

The masked variant of the same vendor 400 surfaces as the generic
「请求失败，请稍后再尝试」 (ext.source=UPSTREAM_VENDOR, upstreamStatus=400 —
the 13:13 incident). The Anthropic-protocol path is IMMUNE:
``openai_body_to_anthropic`` hoists the marker onto the tool_result block
itself. So the stamper must be protocol-aware:

  * api_protocol != 'anthropic' → a ``tool`` message is UNMARKABLE; the
    tail/mid scans walk past it to the assistant/user turn.
  * api_protocol == 'anthropic' (and the no-arg default) → historical
    behaviour preserved byte-for-byte (the translator hoists later).
"""

import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from lib.llm import add_cache_breakpoints  # noqa: E402

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_MODEL = 'yuju-claude-opus-5-evaDaily'


def _tool_loop_body(rounds=1, assistant_prose=''):
    """[system, user, (assistant+tool_calls, tool) × rounds]."""
    messages = [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': 'query'},
    ]
    for i in range(rounds):
        messages.append({
            'role': 'assistant', 'content': assistant_prose,
            'tool_calls': [{'id': f'tc_{i}', 'type': 'function',
                            'function': {'name': 'read_files',
                                         'arguments': json.dumps({'path': f'f{i}.py'})}}],
        })
        messages.append({
            'role': 'tool', 'tool_call_id': f'tc_{i}',
            'content': f'result from round {i} ' + 'x' * 60,
        })
    return {'model': _MODEL, 'messages': messages,
            'tools': [{'type': 'function', 'function': {
                'name': 'read_files', 'description': 'Read.',
                'parameters': {'type': 'object', 'properties': {}}}}]}


def _markers_in(msg):
    """Count cache_control markers anywhere inside a message's content."""
    found = []
    content = msg.get('content')
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('cache_control'):
                found.append(block['cache_control'])
    if isinstance(msg.get('cache_control'), dict):
        found.append(msg['cache_control'])
    return found


def _tool_messages(body):
    return [m for m in body['messages'] if m.get('role') == 'tool']


@pytest.mark.unit
class TestOpenAIProtocolNeverMarksTool:

    def test_tool_tail_unmarked_on_openai(self):
        """THE incident shape: tool result is the conversation tail."""
        body = _tool_loop_body(rounds=1)
        add_cache_breakpoints(body, api_protocol='openai')
        for msg in _tool_messages(body):
            assert not _markers_in(msg), (
                'OpenAI wire must NOT carry cache_control inside a tool '
                'message — the gateway maps it into tool_result.content '
                'and the vendor 400s')

    def test_tool_tail_unmarked_across_rounds(self):
        body = _tool_loop_body(rounds=8)
        for _ in range(4):
            add_cache_breakpoints(body, api_protocol='openai')
        for msg in _tool_messages(body):
            assert not _markers_in(msg)

    def test_tail_marker_walks_to_assistant_prose(self):
        """Skipping the tool message must not LOSE the tail breakpoint —
        it walks back to the assistant turn's text block."""
        body = _tool_loop_body(rounds=1, assistant_prose='Let me read that file.')
        add_cache_breakpoints(body, api_protocol='openai')
        assistant = body['messages'][2]
        assert _markers_in(assistant), (
            'tail breakpoint should land on the assistant prose block')
        assert not _markers_in(body['messages'][3])

    def test_tail_marker_walks_to_user_when_no_prose(self):
        body = _tool_loop_body(rounds=1, assistant_prose='')
        add_cache_breakpoints(body, api_protocol='openai')
        assert _markers_in(body['messages'][1]), (
            'with empty assistant content the tail marker walks to the user turn')

    def test_mid_anchor_never_marks_tool_on_openai(self, monkeypatch):
        """TOFU_CACHE_MID_MODE=current arms the stepping-stone; the invariant
        (no tool message marked) must hold with mid + tail both active."""
        monkeypatch.setenv('TOFU_CACHE_MID_MODE', 'current')
        body = _tool_loop_body(rounds=30)
        add_cache_breakpoints(body, api_protocol='openai')
        for msg in _tool_messages(body):
            assert not _markers_in(msg), (
                'mid-anchor scan must also skip tool messages on OpenAI protocol')

    def test_markers_still_placed_elsewhere(self):
        """Guarding tool messages must not kill caching wholesale: system,
        tool-definition and tail markers still land."""
        body = _tool_loop_body(rounds=1, assistant_prose='prose')
        add_cache_breakpoints(body, api_protocol='openai')
        n_marked = sum(1 for m in body['messages'] if _markers_in(m))
        assert n_marked >= 2, (  # system + tail (assistant prose)
            f'expected system+tail markers at minimum, got {n_marked}')
        fn = body['tools'][-1]['function']
        assert fn.get('cache_control'), 'tool definition marker lost'


@pytest.mark.unit
class TestAnthropicProtocolByteParity:

    def test_default_call_keeps_tool_tail_marker(self):
        """No-arg call == historical behaviour (existing suites depend on it)."""
        body = _tool_loop_body(rounds=1)
        add_cache_breakpoints(body)
        assert _markers_in(body['messages'][-1]), (
            'default must keep the tool-tail marker (translator hoists it)')

    def test_explicit_anthropic_keeps_tool_tail_marker(self):
        body = _tool_loop_body(rounds=1)
        add_cache_breakpoints(body, api_protocol='anthropic')
        assert _markers_in(body['messages'][-1])

    def test_anthropic_translation_hoists_marker_out_of_content(self):
        """End-to-end for the IMMUNE path: after openai_body_to_anthropic the
        marker sits ON the tool_result block, never inside its content —
        exactly what the vendor demands."""
        from lib.llm.anthropic_outbound import openai_body_to_anthropic
        body = _tool_loop_body(rounds=1)
        add_cache_breakpoints(body, api_protocol='anthropic')
        ab = openai_body_to_anthropic(body)
        tool_results = [b for m in ab['messages'] if m['role'] == 'user'
                        for b in (m['content'] if isinstance(m['content'], list) else [])
                        if isinstance(b, dict) and b.get('type') == 'tool_result']
        assert tool_results, 'expected at least one tool_result block'
        for tr in tool_results:
            inner = tr.get('content')
            if isinstance(inner, list):
                for sub in inner:
                    assert 'cache_control' not in sub, (
                        'vendor 400 shape: cache_control inside '
                        'tool_result.content')


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
