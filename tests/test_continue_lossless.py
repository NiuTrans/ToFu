"""Tests for "Continue" lossless resumption — per-provider capability gating.

Covers the Continue / resume flow introduced to fix the two losses the user
called out (historical tool-call content + reasoning/thinking content):

  • OpenAI / DeepSeek / Qwen / GLM / Kimi / Doubao / MiniMax / ERNIE / LongCat
      — tool_calls + tool results must round-trip.
      — thinking / thought_signature / extra_content MUST NOT be injected
        (those APIs reject or silently strip vendor extensions).
      — contentPrefix is never injected as a trailing assistant turn.

  • Claude (extended thinking)
      — tool_calls + tool results must round-trip.
      — reasoning_content + thinking_signature MUST round-trip when the
        frontend supplied both (so Anthropic can rebuild a signed
        thinking block for tool-use continuity).
      — thinking WITHOUT signature is NOT injected (lossy fallback).

  • Gemini
      — tool_calls must carry extra_content.google.thought_signature
        verbatim or the API returns HTTP 400.

This is a pure-unit test of:
  • lib.tasks_pkg.message_builder.inject_tool_history
  • lib.tasks_pkg.conv_message_builder._reconstruct_tool_call_messages
  • lib.model_info capability probes

No real LLM calls, no Flask, no DB.
"""

from __future__ import annotations

import os
import sys

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.model_info import (
    model_requires_thinking_signature_replay,
    model_requires_thought_signature_on_tool_calls,
    model_supports_assistant_prefill,
)
from lib.tasks_pkg.conv_message_builder import _reconstruct_tool_call_messages
from lib.tasks_pkg.message_builder import inject_tool_history


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _make_task(tid: str = 'aaaabbbb' + '0' * 24,
               conv_id: str = 'convtest0' + '0' * 23) -> dict:
    return {'id': tid, 'convId': conv_id}


def _th_round(tc_id: str, name: str, args: str, result: str,
              assistant_content: str = '', thinking: str = '',
              thinking_signature: str = '',
              extra_content: dict | None = None) -> dict:
    tc = {'id': tc_id, 'name': name, 'arguments': args}
    if extra_content:
        tc['extraContent'] = extra_content
    round_dict = {
        'assistantContent': assistant_content,
        'toolCalls': [tc],
        'toolResults': [{'tool_call_id': tc_id, 'content': result}],
    }
    if thinking:
        round_dict['thinking'] = thinking
    if thinking_signature:
        round_dict['thinkingSignature'] = thinking_signature
    return round_dict


def _base_messages() -> list[dict]:
    return [
        {'role': 'system', 'content': 'be helpful'},
        {'role': 'user', 'content': 'what is 2+2?'},
    ]


# ═══════════════════════════════════════════════════════════
#  Capability probes
# ═══════════════════════════════════════════════════════════

class TestModelCapabilities:
    def test_claude_needs_thinking_signature(self):
        assert model_requires_thinking_signature_replay('claude-opus-4-7')
        assert model_requires_thinking_signature_replay('us.anthropic.claude-opus-4-6-v1')

    def test_gemini_needs_thought_signature(self):
        assert model_requires_thought_signature_on_tool_calls('gemini-3.0-pro')
        assert model_requires_thought_signature_on_tool_calls('gemini-2.5-flash')

    def test_others_do_not_need_vendor_extensions(self):
        for m in ('gpt-4o', 'deepseek-chat', 'qwen3-max', 'kimi-k2.6',
                  'glm-5', 'doubao-seed', 'minimax-m2.5', 'ernie-5.0',
                  'longcat-flash'):
            assert not model_requires_thinking_signature_replay(m), m
            assert not model_requires_thought_signature_on_tool_calls(m), m

    def test_anthropic_rejects_assistant_prefill(self):
        assert not model_supports_assistant_prefill('claude-opus-4-7')
        assert not model_supports_assistant_prefill('claude-sonnet-4-5')

    def test_others_tolerate_assistant_prefill(self):
        for m in ('gpt-4o', 'qwen3-max', 'gemini-2.5-flash', 'kimi-k2.6'):
            assert model_supports_assistant_prefill(m), m


# ═══════════════════════════════════════════════════════════
#  inject_tool_history — per-provider shape
# ═══════════════════════════════════════════════════════════

class TestInjectToolHistoryOpenAI:
    """Vanilla OpenAI-compatible path: plain tool_calls + tool results."""

    def test_basic_round_trip(self):
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'web_search',
                          '{"query":"gil"}', 'GIL mutex explanation',
                          assistant_content='Let me search.'),
            ],
        }
        n = inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert n == 1
        # Shape: system, user, assistant(tool_calls, content), tool
        assert len(messages) == 4
        asst = messages[2]
        assert asst['role'] == 'assistant'
        assert asst['content'] == 'Let me search.'
        assert asst['tool_calls'][0]['function']['name'] == 'web_search'
        # NO thinking or vendor-specific extensions
        assert 'reasoning_content' not in asst
        assert 'thinking_signature' not in asst
        assert 'extra_content' not in asst['tool_calls'][0]
        assert messages[3] == {
            'role': 'tool',
            'tool_call_id': 'tc_1',
            'content': 'GIL mutex explanation',
        }

    def test_openai_thinking_dropped_silently(self):
        """OpenAI strips reasoning_content server-side — don't even send it."""
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'grep_search', '{"pattern":"x"}', 'hit',
                          thinking='pondering...',
                          thinking_signature='sig-xyz'),
            ],
        }
        inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        asst = messages[2]
        assert 'reasoning_content' not in asst
        assert 'thinking_signature' not in asst

    def test_gemini_extra_content_dropped_for_non_gemini(self):
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'web_search', '{}', 'ok',
                          extra_content={'google': {'thought_signature': 'gem-sig'}}),
            ],
        }
        inject_tool_history(messages, cfg, _make_task(), 'deepseek-chat')
        tc = messages[2]['tool_calls'][0]
        assert 'extra_content' not in tc


class TestInjectToolHistoryClaude:
    """Claude extended-thinking path: thinking block with signature is required."""

    def test_thinking_block_round_trips(self):
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'fetch_url',
                          '{"url":"https://x"}', 'page body',
                          assistant_content='Fetching…',
                          thinking='The user asked for X so I should…',
                          thinking_signature='opaque-sig-123'),
            ],
        }
        n = inject_tool_history(messages, cfg, _make_task(), 'claude-opus-4-7')
        assert n == 1
        asst = messages[2]
        assert asst['content'] == 'Fetching…'
        assert asst['reasoning_content'] == 'The user asked for X so I should…'
        assert asst['thinking_signature'] == 'opaque-sig-123'

    def test_thinking_without_signature_not_injected(self):
        """Claude would reject a thinking block with no signature — skip it."""
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'list_dir', '{"path":"."}', '[files]',
                          thinking='only text, no sig'),
            ],
        }
        inject_tool_history(messages, cfg, _make_task(), 'claude-opus-4-6')
        asst = messages[2]
        assert 'reasoning_content' not in asst
        assert 'thinking_signature' not in asst

    def test_claude_does_not_get_extra_content(self):
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'web_search', '{}', 'ok',
                          extra_content={'google': {'thought_signature': 'gem-sig'}}),
            ],
        }
        inject_tool_history(messages, cfg, _make_task(), 'claude-opus-4-7')
        tc = messages[2]['tool_calls'][0]
        assert 'extra_content' not in tc


class TestInjectToolHistoryGemini:
    def test_thought_signature_round_trips(self):
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'web_search', '{}', 'ok',
                          extra_content={'google': {'thought_signature': 'gem-sig'}}),
            ],
        }
        inject_tool_history(messages, cfg, _make_task(), 'gemini-3.0-pro')
        tc = messages[2]['tool_calls'][0]
        assert tc['extra_content'] == {'google': {'thought_signature': 'gem-sig'}}

    def test_gemini_does_not_get_thinking_block(self):
        """Gemini uses thought_signature on tool_call, not a thinking block."""
        messages = _base_messages()
        cfg = {
            'toolHistory': [
                _th_round('tc_1', 'web_search', '{}', 'ok',
                          thinking='x', thinking_signature='sig'),
            ],
        }
        inject_tool_history(messages, cfg, _make_task(), 'gemini-2.5-flash')
        asst = messages[2]
        assert 'reasoning_content' not in asst
        assert 'thinking_signature' not in asst


class TestInjectToolHistoryEdgeCases:
    def test_empty_history_is_noop(self):
        messages = _base_messages()
        n = inject_tool_history(messages, {}, _make_task(), 'gpt-4o')
        assert n == 0
        assert len(messages) == 2

    def test_returns_total_tool_call_count_not_rounds(self):
        messages = _base_messages()
        # 2 rounds, first with 2 calls, second with 1 → total 3 calls
        cfg = {
            'toolHistory': [
                {
                    'toolCalls': [
                        {'id': 'a', 'name': 'x', 'arguments': '{}'},
                        {'id': 'b', 'name': 'y', 'arguments': '{}'},
                    ],
                    'toolResults': [
                        {'tool_call_id': 'a', 'content': '1'},
                        {'tool_call_id': 'b', 'content': '2'},
                    ],
                },
                {
                    'toolCalls': [{'id': 'c', 'name': 'z', 'arguments': '{}'}],
                    'toolResults': [{'tool_call_id': 'c', 'content': '3'}],
                },
            ],
        }
        n = inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert n == 3

    def test_tool_result_fallback_when_missing(self):
        messages = _base_messages()
        cfg = {
            'toolHistory': [{
                'toolCalls': [{'id': 'orphan', 'name': 'web_search', 'arguments': '{}'}],
                'toolResults': [],  # oops, lost
            }],
        }
        inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        tool_msg = messages[-1]
        assert tool_msg['role'] == 'tool'
        assert 'lost' in tool_msg['content'].lower()

    def test_unused_rounds_without_tool_calls_skipped(self):
        messages = _base_messages()
        cfg = {'toolHistory': [{'toolCalls': [], 'toolResults': []}]}
        n = inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert n == 0
        assert len(messages) == 2


# ═══════════════════════════════════════════════════════════
#  conv_message_builder parity
# ═══════════════════════════════════════════════════════════

class TestConvBuilderReconstructionParity:
    """The DB→messages reconstructor must emit the SAME shape inject_tool_history
    does — otherwise the debug preview diverges from the live request.

    NB: _reconstruct_tool_call_messages is provider-agnostic (it doesn't see
    the model name).  It ALWAYS attaches vendor fields when the data is
    present; the provider-specific stripping happens later via
    _strip_non_api_fields in build_body (which only keeps whitelisted
    fields).  So what we verify here is that the data is preserved end-to-
    end in the reconstruction step.
    """

    def test_thinking_and_signature_carry_through(self):
        rounds = [{
            'toolCallId': 'tc_1',
            'toolName': 'web_search',
            'toolArgs': '{"q":"x"}',
            'toolContent': 'hit',
            'status': 'done',
            'llmRound': 0,
            'assistantContent': 'Let me look it up.',
            'thinking': 'reasoning trace',
            'thinkingSignature': 'opaque-sig',
        }]
        out = _reconstruct_tool_call_messages(rounds)
        assert out is not None
        asst = out[0]
        assert asst['content'] == 'Let me look it up.'
        assert asst['reasoning_content'] == 'reasoning trace'
        assert asst['thinking_signature'] == 'opaque-sig'

    def test_thinking_without_signature_not_carried(self):
        rounds = [{
            'toolCallId': 'tc_1',
            'toolName': 'web_search',
            'toolArgs': '{}',
            'toolContent': 'hit',
            'status': 'done',
            'llmRound': 0,
            'thinking': 'unsigned',
        }]
        out = _reconstruct_tool_call_messages(rounds)
        asst = out[0]
        assert 'reasoning_content' not in asst
        assert 'thinking_signature' not in asst

    def test_extra_content_on_tool_call(self):
        rounds = [{
            'toolCallId': 'tc_1',
            'toolName': 'web_search',
            'toolArgs': '{}',
            'toolContent': 'hit',
            'status': 'done',
            'llmRound': 0,
            'extraContent': {'google': {'thought_signature': 'gem'}},
        }]
        out = _reconstruct_tool_call_messages(rounds)
        tc = out[0]['tool_calls'][0]
        assert tc['extra_content'] == {'google': {'thought_signature': 'gem'}}

    def test_legacy_rounds_without_new_fields_still_work(self):
        """Old DB rows must not crash or inject bogus fields."""
        rounds = [{
            'toolCallId': 'tc_1',
            'toolName': 'web_search',
            'toolArgs': '{"q":"x"}',
            'toolContent': 'hit',
            'status': 'done',
            'llmRound': 0,
        }]
        out = _reconstruct_tool_call_messages(rounds)
        assert out is not None
        asst = out[0]
        assert asst['role'] == 'assistant'
        assert 'reasoning_content' not in asst
        assert 'thinking_signature' not in asst
        assert 'extra_content' not in asst['tool_calls'][0]


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
