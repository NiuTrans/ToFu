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

import json
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
from lib.chat.turn_builder import scan_continue_checkpoint
from lib.tasks_pkg.conv_message_builder._toolcalls import _reconstruct_tool_call_messages
from lib.tasks_pkg.conv_message_builder._transform import _build_assistant_messages
from lib.tasks_pkg.message_builder import inject_tool_history, prepare_tool_history
from lib.tasks_pkg.message_builder._tool_history import (
    ContinueToolHistoryProtocolError,
)

pytestmark = pytest.mark.unit


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

    def test_prepared_history_is_read_only_and_injected_without_reparse(self):
        cfg = {'toolHistory': [
            _th_round('same-id', 'read_files', '{"path":"x"}', 'same'),
            _th_round('same-id', 'read_files', '{"path":"x"}', 'same'),
        ]}
        original = json.loads(json.dumps(cfg))

        prepared = prepare_tool_history(cfg, _make_task(), 'gpt-4o')

        assert cfg == original
        assert prepared.injected_calls == 2
        assert prepared.injected_rounds == 2
        messages = _base_messages()
        assert inject_tool_history(
            messages, {}, _make_task(), 'gpt-4o',
            prepared_history=prepared,
        ) == 2
        assert [message['tool_call_id'] for message in messages
                if message.get('role') == 'tool'] == ['same-id', 'same-id']

        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(
                _base_messages(), {}, _make_task(), 'claude-opus-4-7',
                prepared_history=prepared,
            )

    def test_duplicate_legacy_ids_keep_results_by_occurrence(self):
        messages = _base_messages()
        cfg = {'toolHistory': [{
            'toolCalls': [
                {'id': 'reused', 'name': 'read_files',
                 'arguments': '{"path":"old.py"}'},
                {'id': 'reused', 'name': 'read_files',
                 'arguments': '{"path":"new.py"}'},
            ],
            'toolResults': [
                {'tool_call_id': 'reused', 'content': 'old bytes'},
                {'tool_call_id': 'reused', 'content': 'new bytes'},
            ],
        }]}

        assert inject_tool_history(
            messages, cfg, _make_task(), 'gpt-4o') == 2

        assert [message['content'] for message in messages
                if message.get('role') == 'tool'] == [
            'old bytes', 'new bytes',
        ]

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

    def test_missing_tool_result_rejects_checkpoint_before_injection(self):
        messages = _base_messages()
        original = [dict(message) for message in messages]
        cfg = {
            'toolHistory': [{
                'toolCalls': [{'id': 'orphan', 'name': 'web_search', 'arguments': '{}'}],
                'toolResults': [],  # oops, lost
            }],
        }
        with pytest.raises(ContinueToolHistoryProtocolError) as raised:
            inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert raised.value.status_code == 422
        assert messages == original

    def test_unused_rounds_without_tool_calls_skipped(self):
        messages = _base_messages()
        cfg = {'toolHistory': [{'toolCalls': [], 'toolResults': []}]}
        n = inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert n == 0
        assert len(messages) == 2

    def test_malformed_history_shapes_fail_before_partial_injection(self):
        messages = _base_messages()
        original = [dict(message) for message in messages]
        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(
                messages, {'toolHistory': {'toolCalls': []}},
                _make_task(), 'gpt-4o')
        assert messages == _base_messages()

        cfg = {'toolHistory': [
            None,
            {'toolCalls': 'not-a-list'},
            {
                'toolCalls': [
                    None,
                    {'id': 'ok', 'name': 'read_files', 'arguments': '{}'},
                ],
                'toolResults': [
                    None,
                    {'tool_call_id': 'ok', 'content': 'valid sibling'},
                ],
            },
        ]}
        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert messages == original

    @pytest.mark.parametrize(('round_field', 'round_value'), [
        ('assistantContent', []),
        ('thinking', {}),
        ('thinkingSignature', 7),
    ])
    def test_malformed_round_metadata_is_not_silently_dropped(
            self, round_field, round_value):
        messages = _base_messages()
        history_round = _th_round(
            'call', 'read_files', '{"path":"x"}', 'bytes')
        history_round[round_field] = round_value

        with pytest.raises(ContinueToolHistoryProtocolError):
            prepare_tool_history(
                {'toolHistory': [history_round]}, _make_task(), 'gpt-4o')

        assert messages == _base_messages()

    def test_malformed_provider_extension_is_not_silently_dropped(self):
        messages = _base_messages()
        history_round = _th_round(
            'call', 'read_files', '{"path":"x"}', 'bytes')
        history_round['toolCalls'][0]['extraContent'] = []

        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(
                messages, {'toolHistory': [history_round]},
                _make_task(), 'gemini-3.0-pro')

        assert messages == _base_messages()

    def test_oversized_history_is_replayed_without_size_rejection(self):
        # Resume carries no size ceiling: a checkpoint beyond the retired
        # 1024-round / 4096-occurrence / 8M-char budgets still replays in
        # full; boundedness comes from working-set compaction, not rejection.
        history = [_th_round(f'c{i}', 'read_files', '{}', 'ok')
                   for i in range(1025)]
        history.append(_th_round('big', 'read_files', '{}', 'y' * 8_000_001))

        prepared = prepare_tool_history(
            {'toolHistory': history}, _make_task(), 'gpt-4o')

        assert prepared.injected_rounds == 1026
        assert prepared.injected_calls == 1026

    def test_invalid_caller_rejects_checkpoint_without_shifting_duplicate_id(self):
        messages = _base_messages()
        original = [dict(message) for message in messages]
        cfg = {'toolHistory': [{
            'toolCalls': [
                {
                    'id': 'reused', 'name': 'read_files', 'arguments': '{}',
                    'caller': {'type': 'multi_agent'},
                },
                {'id': 'reused', 'name': 'read_files', 'arguments': '{}'},
            ],
            'toolResults': [
                {'tool_call_id': 'reused', 'content': 'invalid owner result'},
                {'tool_call_id': 'reused', 'content': 'valid root result'},
            ],
        }]}

        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert messages == original

    def test_result_only_caller_is_applied_to_both_sides(self):
        messages = _base_messages()
        caller = {'type': 'multi_agent', 'agent_name': ' /worker '}
        cfg = {'toolHistory': [{
            'toolCalls': [
                {'id': 'child', 'name': 'read_files', 'arguments': '{}'},
            ],
            'toolResults': [{
                'tool_call_id': 'child', 'content': 'worker result',
                'caller': caller,
            }],
        }]}

        assert inject_tool_history(
            messages, cfg, _make_task(), 'gpt-4o') == 1
        expected = {'type': 'multi_agent', 'agent_name': '/worker'}
        assert messages[2]['tool_calls'][0]['caller'] == expected
        assert messages[3]['caller'] == expected

    def test_structured_result_becomes_valid_json_tool_content(self):
        messages = _base_messages()
        cfg = {'toolHistory': [{
            'toolCalls': [
                {'id': 'structured', 'name': 'read_files', 'arguments': '{}'},
            ],
            'toolResults': [{
                'tool_call_id': 'structured',
                'content': {'ok': True, 'paths': ['a.py', 'b.py']},
            }],
        }]}

        assert inject_tool_history(
            messages, cfg, _make_task(), 'gpt-4o') == 1
        assert json.loads(messages[-1]['content']) == {
            'ok': True, 'paths': ['a.py', 'b.py'],
        }

    def test_disagreeing_call_and_result_authorities_reject_checkpoint(self):
        messages = _base_messages()
        original = [dict(message) for message in messages]
        cfg = {'toolHistory': [{
            'toolCalls': [{
                'id': 'child', 'name': 'read_files', 'arguments': '{}',
                'caller': {'type': 'multi_agent', 'agent_name': '/worker-a'},
            }],
            'toolResults': [{
                'tool_call_id': 'child', 'content': 'wrong worker result',
                'caller': {'type': 'multi_agent', 'agent_name': '/worker-b'},
            }],
        }]}

        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert messages == original

    def test_non_object_arguments_reject_checkpoint_instead_of_becoming_empty(self):
        messages = _base_messages()
        original = [dict(message) for message in messages]
        cfg = {'toolHistory': [{
            'toolCalls': [{
                'id': 'bad-args', 'name': 'read_files',
                'arguments': ['not', 'an', 'object'],
            }],
            'toolResults': [{
                'tool_call_id': 'bad-args', 'content': 'must not be replayed',
            }],
        }]}

        with pytest.raises(ContinueToolHistoryProtocolError):
            inject_tool_history(messages, cfg, _make_task(), 'gpt-4o')
        assert messages == original

    def test_duplicate_ids_pair_distinct_authorities_by_occurrence(self):
        messages = _base_messages()
        callers = [
            {'type': 'program', 'caller_id': 'program-a'},
            {'type': 'multi_agent', 'agent_name': '/worker-b'},
        ]
        cfg = {'toolHistory': [{
            'toolCalls': [
                {
                    'id': 'reused', 'name': 'read_files', 'arguments': '{}',
                    'caller': caller,
                }
                for caller in callers
            ],
            'toolResults': [
                {
                    'tool_call_id': 'reused', 'content': f'result-{index}',
                    'caller': caller,
                }
                for index, caller in enumerate(callers)
            ],
        }]}

        assert inject_tool_history(
            messages, cfg, _make_task(), 'gpt-4o') == 2
        assert [call['caller'] for call in messages[2]['tool_calls']] == callers
        assert [message['caller'] for message in messages[3:]] == callers
        assert [message['content'] for message in messages[3:]] == [
            'result-0', 'result-1',
        ]


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

    def test_thinking_without_signature_carries_reasoning_not_signature(self):
        """Unsigned thinking → reasoning_content IS carried, signature is NOT.

        The provider-agnostic reconstructor (per this class's docstring) ALWAYS
        preserves vendor fields when the data is present — the model-specific
        stripping happens later in build_body / openai_body_to_anthropic. Since
        commit 8ecbbcf ("freeze the thinking-no-signature {reasoning_content}
        live↔replay flip"), build_assistant_tool_call_message carries
        reasoning_content whenever thinking text is present INDEPENDENT of
        signature — mirroring the live tail — so replay↔live can't re-diverge
        on this field. The UNSIGNED thinking block is dropped safely downstream
        (proven by TestAnthropicOutboundReplay::test_unsigned_thinking_block_dropped),
        so no HTTP 400, while DeepSeek's reasoning_content replay is preserved.

        thinking_signature is still NOT carried when absent (a signature without
        reasoning text — or here, no signature at all — is meaningless).
        """
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
        # reasoning_content carried (independent of signature — the deliberate
        # 8ecbbcf contract).
        assert asst['reasoning_content'] == 'unsigned'
        # signature dropped — none was present.
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

    def test_structured_arguments_match_continue_canonical_form(self):
        rounds = [{
            'toolCallId': 'tc_structured', 'toolName': 'read_files',
            'toolArgs': {'path': 'a.py', 'line_start': 7},
            'toolContent': 'body', 'status': 'done', 'llmRound': 0,
        }]

        out = _reconstruct_tool_call_messages(rounds)

        assert out[0]['tool_calls'][0]['function']['arguments'] == (
            '{"line_start":7,"path":"a.py"}')

    def test_same_local_round_from_two_attempts_is_not_one_provider_batch(self):
        rounds = [
            {
                'attemptId': 'attempt-old', 'taskId': 'task-old',
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'old-call',
                'toolName': 'read_files', 'toolArgs': '{}',
                'toolContent': 'old result', 'status': 'done',
            },
            {
                'attemptId': 'attempt-new', 'taskId': 'task-new',
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'new-call',
                'toolName': 'grep_search', 'toolArgs': '{}',
                'toolContent': 'new result', 'status': 'done',
            },
        ]
        out = _reconstruct_tool_call_messages(rounds)
        assistant_batches = [message for message in out or []
                             if message.get('tool_calls')]
        assert [[call['id'] for call in message['tool_calls']]
                for message in assistant_batches] == [
            ['old-call'], ['new-call'],
        ]

    def test_invalid_authority_stops_before_later_dependent_siblings(self):
        rounds = [
            None,
            {
                'llmRound': 0, 'toolCallId': 'safe-prefix',
                'toolName': 'read_files', 'toolArgs': '{}',
                'toolContent': 'safe result',
            },
            {
                'llmRound': 1, 'toolCallId': 'owned',
                'toolName': 'read_files', 'toolArgs': '{}',
                'toolContent': 'must not become root',
                'caller': {'type': 'multi_agent'},
            },
            {
                'llmRound': 2, 'toolCallId': 'depends-on-gap',
                'toolName': 'read_files', 'toolArgs': '{}',
                'toolContent': 'must not cross gap',
            },
        ]

        out = _reconstruct_tool_call_messages(rounds)
        assert out is not None
        assert [call['id'] for call in out[0]['tool_calls']] == ['safe-prefix']
        assert out[1]['content'] == 'safe result'

    def test_only_invalid_caller_round_is_not_replayed_as_root(self):
        assert _reconstruct_tool_call_messages([{
            'llmRound': 0, 'toolCallId': 'owned',
            'toolName': 'read_files', 'toolArgs': '{}',
            'toolContent': 'must not become root',
            'caller': 'broken-envelope',
        }]) is None

    def test_malformed_assistant_sidecars_do_not_poison_reconstruction(self):
        rounds = [{
            'llmRound': 0, 'toolCallId': 'valid',
            'toolName': 'read_files', 'toolArgs': '{}',
            'toolContent': 'ok',
            'assistantContent': {'not': 'text'},
            'thinking': ['not', 'text'],
            'thinkingSignature': {'not': 'text'},
            '_responsesItems': {'not': 'a list'},
            '_anthropicContentBlocks': 'not-a-list',
        }]

        out = _reconstruct_tool_call_messages(rounds)
        assert out is not None
        assert out[0] == {
            'role': 'assistant',
            'tool_calls': [{
                'id': 'valid', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'},
            }],
        }

    def test_malformed_cold_row_falls_back_without_crashing(self):
        out = _build_assistant_messages({
            'content': {'not': 'text'},
            'thinking': ['not', 'text'],
            'segments': {'not': 'a list'},
            'toolRounds': [None, 'not-a-round'],
            'toolSummary': {'not': 'text'},
            '_responsesItems': {'not': 'a list'},
            '_anthropicContentBlocks': 'not-a-list',
        })
        assert out == [{'role': 'assistant', 'content': '[]'}]


class TestContinueCheckpointAttemptIdentity:
    @staticmethod
    def _round(attempt, task, round_num, llm_round, call_id):
        return {
            'attemptId': attempt, 'taskId': task,
            'roundNum': round_num, 'llmRound': llm_round,
            'toolCallId': call_id, 'toolName': 'read_files',
            'toolArgs': '{}', 'toolContent': f'result:{call_id}',
            'status': 'done',
        }

    def test_continue_replay_preserves_attempt_chronology(self):
        rounds = [
            self._round('attempt-old', 'task-old', 1, 0, 'old-r0'),
            self._round('attempt-old', 'task-old', 2, 1, 'old-r1'),
            self._round('attempt-new', 'task-new', 1, 0, 'new-r0-a'),
            self._round('attempt-new', 'task-new', 2, 0, 'new-r0-b'),
        ]
        checkpoint = scan_continue_checkpoint({'toolRounds': rounds})
        assert checkpoint is not None
        assert [[call['id'] for call in batch['toolCalls']]
                for batch in checkpoint['tool_history']] == [
            ['old-r0'], ['old-r1'], ['new-r0-a', 'new-r0-b'],
        ]

    def test_legacy_counter_reset_is_a_new_occurrence(self):
        rounds = [
            self._round('', '', 1, 0, 'legacy-old-r0'),
            self._round('', '', 2, 1, 'legacy-old-r1'),
            self._round('', '', 1, 0, 'legacy-new-r0'),
        ]
        checkpoint = scan_continue_checkpoint({'toolRounds': rounds})
        assert checkpoint is not None
        assert [[call['id'] for call in batch['toolCalls']]
                for batch in checkpoint['tool_history']] == [
            ['legacy-old-r0'], ['legacy-old-r1'], ['legacy-new-r0'],
        ]

    def test_legacy_new_prose_splits_adjacent_reused_round_counter(self):
        """Narration is a second legacy provider-response boundary.

        Some pre-attempt rows continued the tool ordinal instead of resetting
        it, so roundNum alone cannot separate adjacent R0 occurrences.
        """
        rounds = [
            {
                **self._round('', '', 7, 0, 'legacy-first'),
                'assistantContent': 'first response',
            },
            {
                **self._round('', '', 8, 0, 'legacy-second'),
                'assistantContent': 'resumed response',
            },
        ]
        checkpoint = scan_continue_checkpoint({'toolRounds': rounds})
        assert checkpoint is not None
        assert [[call['id'] for call in batch['toolCalls']]
                for batch in checkpoint['tool_history']] == [
            ['legacy-first'], ['legacy-second'],
        ]

    def test_malformed_round_identities_are_isolated_not_hashed_or_merged(self):
        rounds = [
            self._round({'bad': 'attempt'}, '', 1, {}, 'malformed-one'),
            self._round('', '', 2, [], 'malformed-two'),
        ]
        checkpoint = scan_continue_checkpoint({'toolRounds': rounds})
        assert checkpoint is not None
        assert [[call['id'] for call in batch['toolCalls']]
                for batch in checkpoint['tool_history']] == [
            ['malformed-one'], ['malformed-two'],
        ]

    def test_synthetic_and_malformed_display_rows_never_enter_replay(self):
        rounds = [
            None,
            self._round('', '', 1, 0, 'real-a'),
            {
                'roundNum': 9_000_001, '_inboxInject': True,
                'toolName': 'agent_inbox', 'status': 'done',
            },
            'malformed-display-row',
            self._round('', '', 2, 1, 'real-b'),
        ]

        checkpoint = scan_continue_checkpoint({'toolRounds': rounds})
        assert checkpoint is not None
        assert [round_entry['toolCallId']
                for round_entry in checkpoint['kept_rounds']] == [
            'real-a', 'real-b',
        ]
        assert [[call['id'] for call in batch['toolCalls']]
                for batch in checkpoint['tool_history']] == [
            ['real-a'], ['real-b'],
        ]

    def test_unreplayable_authority_stops_prefix_before_later_calls(self):
        rounds = [
            self._round('', '', 1, 0, 'safe-prefix'),
            {
                **self._round('', '', 2, 1, 'invalid-owner'),
                'caller': {'type': 'multi_agent'},
            },
            self._round('', '', 3, 2, 'depends-on-gap'),
        ]

        checkpoint = scan_continue_checkpoint({'toolRounds': rounds})
        assert checkpoint is not None
        assert [round_entry['toolCallId']
                for round_entry in checkpoint['kept_rounds']] == [
            'safe-prefix',
        ]
        assert checkpoint['discarded_rounds'] == 2

    def test_display_metadata_never_fabricates_a_model_visible_result(self):
        source = self._round('', '', 1, 0, 'recovered')
        source['toolContent'] = None
        source['results'] = [
            {'snippet': {'not': 'text'}, 'title': 'safe title'},
            {'content': 'safe content'},
        ]

        checkpoint = scan_continue_checkpoint({
            'toolRounds': [source],
            'content': {'not': 'text'},
            'thinking': ['not', 'text'],
        })
        assert checkpoint is None
        assert source['toolContent'] is None

    @pytest.mark.parametrize('terminal_status', [
        'error', 'rejected', 'aborted',
    ])
    def test_terminal_failure_with_exact_result_is_a_replayable_fact(
        self, terminal_status,
    ):
        failed = self._round('', '', 1, 0, f'{terminal_status}-call')
        failed['status'] = terminal_status
        failed['toolContent'] = f'{terminal_status}: exact model receipt'

        checkpoint = scan_continue_checkpoint({'toolRounds': [failed]})

        assert checkpoint is not None
        assert checkpoint['kept_rounds'][0]['status'] == terminal_status
        assert checkpoint['tool_history'][0]['toolResults'][0]['content'] == (
            f'{terminal_status}: exact model receipt')

    def test_discarded_provider_attempt_is_transparent_to_later_real_call(self):
        discarded = self._round('', '', 1, 0, 'discarded-call')
        discarded.update({
            'status': 'aborted',
            'toolContent': None,
            '_providerAttemptDiscarded': True,
            'results': [{'badge': 'superseded', 'fetched': False,
                         'fetchedChars': 0}],
        })
        adopted = self._round('', '', 2, 0, 'adopted-call')

        checkpoint = scan_continue_checkpoint({
            'toolRounds': [discarded, adopted],
        })

        assert checkpoint is not None
        assert [item['toolCallId'] for item in checkpoint['kept_rounds']] == [
            'adopted-call',
        ]
        assert checkpoint['discarded_rounds'] == 1

    def test_structured_arguments_are_canonicalized_not_erased(self):
        source = self._round('', '', 1, 0, 'structured-args')
        source['toolArgs'] = {'path': 'a.py', 'line_start': 7}

        checkpoint = scan_continue_checkpoint({'toolRounds': [source]})

        assert checkpoint is not None
        assert checkpoint['tool_history'][0]['toolCalls'][0]['arguments'] == (
            '{"line_start":7,"path":"a.py"}')
        # Scanning is copy-on-write; durable audit data remains untouched.
        assert source['toolArgs'] == {'path': 'a.py', 'line_start': 7}

    def test_unserializable_structured_arguments_stop_the_causal_prefix(self):
        prefix = self._round('', '', 1, 0, 'safe-prefix')
        invalid = self._round('', '', 2, 1, 'invalid-args')
        invalid['toolArgs'] = {'bad': {object()}}
        dependent = self._round('', '', 3, 2, 'depends-on-gap')

        checkpoint = scan_continue_checkpoint({
            'toolRounds': [prefix, invalid, dependent],
        })

        assert checkpoint is not None
        assert [item['toolCallId'] for item in checkpoint['kept_rounds']] == [
            'safe-prefix',
        ]


class TestReconstructOrphanImmunity:
    """Known discarded-attempt artifacts are transparent; unknown gaps stop.

    A result-less early announcement used to collapse every completed call in
    the Turn. New producers stamp discarded provider attempts explicitly, so
    those artifacts can be ignored without guessing. An unmarked
    identity-bearing row remains an unknown causal gap and must prevent later
    calls from being replayed across it.
    """

    def _good(self, tc_id, name, lr, content='ok'):
        return {'toolCallId': tc_id, 'toolName': name, 'toolArgs': '{}',
                'toolContent': content, 'status': 'done', 'llmRound': lr}

    def _orphan(self, tc_id, name, lr, *, discarded=False):
        # An early-announced round that never got a result: no toolContent,
        # swept to 'aborted' by the dangling sweep.
        row = {'toolCallId': tc_id, 'toolName': name, 'query': name,
               'status': 'aborted', 'llmRound': lr,
               'results': [{'badge': 'interrupted', 'source': 'Interrupted'}]}
        if discarded:
            row['_providerAttemptDiscarded'] = True
        return row

    def test_orphan_round_does_not_collapse_turn(self):
        rounds = [
            self._good('tc_1', 'read_files', 0),
            self._orphan('tc_orphan', 'grep_search', 0, discarded=True),
            self._good('tc_2', 'grep_search', 1),
        ]
        out = _reconstruct_tool_call_messages(rounds)
        # The explicit transport-artifact marker proves the missing row did not
        # execute, so both real calls survive on either side.
        assert out is not None
        tool_call_ids = [tc['id']
                         for m in out if m.get('role') == 'assistant'
                         for tc in m.get('tool_calls', [])]
        assert tool_call_ids == ['tc_1', 'tc_2']
        assert 'tc_orphan' not in tool_call_ids
        # Every tool_use has its paired tool_result (no orphan tool role).
        tool_ids = [m['tool_call_id'] for m in out if m.get('role') == 'tool']
        assert sorted(tool_ids) == ['tc_1', 'tc_2']

    def test_unmarked_resultless_occurrence_stops_before_dependent_calls(self):
        rounds = [
            self._good('tc_1', 'read_files', 0),
            self._orphan('unknown-gap', 'grep_search', 1),
            self._good('must-not-cross-gap', 'read_files', 2),
        ]

        out = _reconstruct_tool_call_messages(rounds)

        assert out is not None
        tool_call_ids = [tc['id']
                         for message in out if message.get('role') == 'assistant'
                         for tc in message.get('tool_calls', [])]
        assert tool_call_ids == ['tc_1']

    def test_interrupted_round_WITH_result_is_kept(self):
        """A genuine interruption that DID capture a real result (toolContent
        present) forms a legal pair and must be PRESERVED — the filter keys on
        field completeness, not on the 'aborted' label."""
        rounds = [
            self._good('tc_1', 'read_files', 0),
            {'toolCallId': 'tc_partial', 'toolName': 'run_command',
             'toolArgs': '{}', 'toolContent': 'partial output before stop',
             'status': 'aborted', 'llmRound': 1},
        ]
        out = _reconstruct_tool_call_messages(rounds)
        assert out is not None
        tool_call_ids = [tc['id']
                         for m in out if m.get('role') == 'assistant'
                         for tc in m.get('tool_calls', [])]
        assert tool_call_ids == ['tc_1', 'tc_partial']
        partial_result = [m for m in out
                          if m.get('role') == 'tool'
                          and m['tool_call_id'] == 'tc_partial'][0]
        assert partial_result['content'] == 'partial output before stop'

    def test_all_orphans_returns_none(self):
        """If NOTHING is reconstructable, still return None (caller uses the
        legacy summary fallback) — the empty-after-filter contract."""
        rounds = [
            self._orphan('o1', 'read_files', 0),
            self._orphan('o2', 'grep_search', 1),
        ]
        assert _reconstruct_tool_call_messages(rounds) is None


# ═══════════════════════════════════════════════════════════
#  Anthropic Messages API: signature CAPTURE + outbound replay
# ═══════════════════════════════════════════════════════════
#
# Root cause of the "thinking but NO signature" lossy-continuation warning:
# models that stream through the Anthropic Messages API (e.g.
# aws.claude-opus-4.8) emit the opaque thinking-block signature as a
# `signature_delta` event, which the SSE translator used to ignore. Without
# capture there is nothing to persist, so Continue could never replay a
# signed thinking block. These tests pin the capture + replay round-trip.

import json as _json
import time as _time

from lib.llm.anthropic_outbound import (
    AnthropicSSETranslator,
    anthropic_response_to_openai,
    openai_body_to_anthropic,
)


class TestAnthropicSignatureCapture:
    def test_translator_surfaces_signature_delta(self):
        tr = AnthropicSSETranslator(model='aws.claude-opus-4.8')
        out = tr.translate(_json.dumps({
            'type': 'content_block_delta', 'index': 0,
            'delta': {'type': 'signature_delta', 'signature': 'ErcBSIG=='},
        }))
        assert out == [{'choices': [{'delta': {'thinking_signature': 'ErcBSIG=='}}]}]

    def test_streaming_accumulator_captures_signature(self):
        from lib.llm._sse_core import SSEAccumulator
        from lib.llm.diagnostics import RawSSEDumper

        body = {'model': 'aws.claude-opus-4.8', 'messages': []}
        acc = SSEAccumulator(
            body, 'trace', RawSSEDumper('aws.claude-opus-4.8', 'trace', body),
            AnthropicSSETranslator(model='aws.claude-opus-4.8'), _time.time())
        lines = [
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'thinking', 'thinking': ''}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'thinking_delta', 'thinking': 'Reasoning.'}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'signature_delta', 'signature': 'ErcBSIG=='}},
            {'type': 'content_block_stop', 'index': 0},
            {'type': 'content_block_start', 'index': 1,
             'content_block': {'type': 'tool_use', 'id': 'tu_1',
                               'name': 'read_files', 'input': {}}},
            {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': '{}'}},
            {'type': 'content_block_stop', 'index': 1},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'},
             'usage': {'output_tokens': 5}},
            {'type': 'message_stop'},
        ]
        for ev in lines:
            if acc.feed_line('data: ' + _json.dumps(ev)):
                break
        msg, finish, _usage = acc.finalize()
        assert msg['reasoning_content'] == 'Reasoning.'
        assert msg['thinking_signature'] == 'ErcBSIG=='
        assert finish == 'tool_calls'
        assert msg['tool_calls'][0]['function']['name'] == 'read_files'

    def test_nonstreaming_captures_signature(self):
        resp = {
            'content': [
                {'type': 'thinking', 'thinking': 'Hmm', 'signature': 'NONSTREAM'},
                {'type': 'text', 'text': 'hi'},
            ],
            'stop_reason': 'end_turn',
            'usage': {'input_tokens': 1, 'output_tokens': 1},
        }
        msg = anthropic_response_to_openai(resp)['choices'][0]['message']
        assert msg['thinking_signature'] == 'NONSTREAM'


class TestOpenAICompatReasoningDetails:
    """The PRODUCTION path for aws.claude-opus-4.8: the sankuai gateway is
    OpenAI-compat (no protocol=anthropic), and streams the Claude thinking
    signature OpenRouter-style as `reasoning_details:[{type:thinking,
    signature:...}]`. Capture it on the way in, rebuild it on replay."""

    def _acc(self):
        from lib.llm._sse_core import SSEAccumulator
        from lib.llm.diagnostics import RawSSEDumper
        body = {'model': 'aws.claude-opus-4.8', 'messages': []}
        return SSEAccumulator(
            body, 'tr', RawSSEDumper('aws.claude-opus-4.8', 'tr', body),
            None, _time.time())

    def test_capture_signature_from_reasoning_details(self):
        acc = self._acc()
        lines = [
            {'choices': [{'delta': {'role': 'assistant', 'content': '',
                                    'reasoning_content': 'Let me '}}]},
            {'choices': [{'delta': {'content': '', 'reasoning_details': [
                {'type': 'thinking', 'thinking': 'think.'}]}}]},
            {'choices': [{'delta': {'role': 'assistant', 'content': '',
                                    'reasoning_details': [
                {'type': 'thinking', 'signature': 'EtIG_SIG_'}]}}]},
            {'choices': [{'delta': {'tool_calls': [{'index': 0, 'id': 'tu_1',
                'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'}}]}}]},
            {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
        ]
        for ln in lines:
            acc.feed_line('data: ' + _json.dumps(ln))
        acc.feed_line('data: [DONE]')
        msg, fr, _u = acc.finalize()
        assert msg['reasoning_content'] == 'Let me think.'
        assert msg['thinking_signature'] == 'EtIG_SIG_'
        assert fr == 'tool_calls'

    def test_reasoning_details_text_only_no_signature(self):
        acc = self._acc()
        acc.feed_line('data: ' + _json.dumps(
            {'choices': [{'delta': {'reasoning_details': [
                {'type': 'thinking', 'thinking': 'just text'}]}}]}))
        acc.feed_line('data: [DONE]')
        msg, _fr, _u = acc.finalize()
        assert msg['reasoning_content'] == 'just text'
        assert 'thinking_signature' not in msg

    def test_build_body_rebuilds_reasoning_details(self):
        from lib.llm.body import build_body
        msgs = [
            {'role': 'user', 'content': 'do X'},
            {'role': 'assistant', 'reasoning_content': 'thought',
             'thinking_signature': 'EtIG_SIG_',
             'tool_calls': [{'id': 'tu_1', 'type': 'function',
                             'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tu_1', 'content': 'body'},
            {'role': 'user', 'content': 'continue'},
        ]
        b = build_body('aws.claude-opus-4.8', msgs, thinking_enabled=True)
        asst = [m for m in b['messages'] if m.get('role') == 'assistant'][0]
        assert asst['reasoning_details'] == [
            {'type': 'thinking', 'thinking': 'thought', 'signature': 'EtIG_SIG_'}]

    def test_build_body_no_rebuild_without_signature(self):
        from lib.llm.body import build_body
        msgs = [
            {'role': 'user', 'content': 'do X'},
            {'role': 'assistant', 'reasoning_content': 'thought',
             'tool_calls': [{'id': 'tu_1', 'type': 'function',
                             'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tu_1', 'content': 'body'},
            {'role': 'user', 'content': 'continue'},
        ]
        b = build_body('aws.claude-opus-4.8', msgs, thinking_enabled=True)
        asst = [m for m in b['messages'] if m.get('role') == 'assistant'][0]
        assert 'reasoning_details' not in asst

    def test_build_body_skips_non_claude(self):
        from lib.llm.body import build_body
        msgs = [
            {'role': 'user', 'content': 'do X'},
            {'role': 'assistant', 'reasoning_content': 'thought',
             'thinking_signature': 'sig',
             'tool_calls': [{'id': 'tu_1', 'type': 'function',
                             'function': {'name': 'read_files', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 'tu_1', 'content': 'body'},
            {'role': 'user', 'content': 'continue'},
        ]
        b = build_body('gpt-4o', msgs)
        asst = [m for m in b['messages'] if m.get('role') == 'assistant'][0]
        assert 'reasoning_details' not in asst


class TestAnthropicOutboundReplay:
    """openai_body_to_anthropic must re-emit a signed thinking block on the
    replayed assistant turn (Continue), or drop it when no signature."""

    def _assistant_turn(self, with_sig: bool) -> dict:
        asst = {
            'role': 'assistant',
            'reasoning_content': 'I should read files',
            'tool_calls': [{'id': 'tu_1', 'type': 'function',
                            'function': {'name': 'read_files',
                                         'arguments': '{"path":"a.py"}'}}],
        }
        if with_sig:
            asst['thinking_signature'] = 'ErcBSIG=='
        body = {'model': 'aws.claude-opus-4.8', 'max_tokens': 4096, 'messages': [
            {'role': 'user', 'content': 'do X'},
            asst,
            {'role': 'tool', 'tool_call_id': 'tu_1', 'content': 'file body'},
        ]}
        out = openai_body_to_anthropic(body)
        return [m for m in out['messages'] if m['role'] == 'assistant'][0]

    def test_signed_thinking_block_replayed_first(self):
        asst = self._assistant_turn(with_sig=True)
        types = [b['type'] for b in asst['content']]
        assert types == ['thinking', 'tool_use']
        think = asst['content'][0]
        assert think['thinking'] == 'I should read files'
        assert think['signature'] == 'ErcBSIG=='

    def test_unsigned_thinking_block_dropped(self):
        asst = self._assistant_turn(with_sig=False)
        types = [b['type'] for b in asst['content']]
        assert types == ['tool_use']


# ═══════════════════════════════════════════════════════════
#  STEP 4 — segment-driven reconstruction parity (epic pt_cb8f98b0cb9b47fb)
# ═══════════════════════════════════════════════════════════
#
# Proves the SEGMENT model can drive the exact wire messages the toolRounds
# path produces today — the strangler-fig reader migration. Every case builds
# segments via assemble_segments, then compares
# reconstruct_tool_messages_from_segments(...) BYTE-IDENTICAL to
# _reconstruct_tool_call_messages(rounds). Invariants #2/#3/#4/#6 pinned +
# neutered.

from lib.tasks_pkg.segments import (  # noqa: E402
    assemble_segments,
    reconstruct_tool_messages_from_segments,
    rehydrate_segments,
    resume_prefill_from_segments,
    segments_to_json,
    tool_history_from_segments,
)


def _task_with_rounds(rounds, content='', thinking=''):
    return {'id': 'seg' + '0' * 29, 'convId': 'c' * 32,
            'content': content, 'thinking': thinking, 'toolRounds': rounds}


class TestSegmentReconstructionParity:
    """reconstruct_tool_messages_from_segments == _reconstruct_tool_call_messages."""

    def _round(self, rn, lr, tc_id, name, args, content, *,
               ac='', think='', sig='', extra=None):
        r = {'roundNum': rn, 'llmRound': lr, 'toolCallId': tc_id,
             'toolName': name, 'toolArgs': args, 'toolContent': content,
             'status': 'done'}
        if ac:
            r['assistantContent'] = ac
        if think:
            r['thinking'] = think
        if sig:
            r['thinkingSignature'] = sig
        if extra:
            r['extraContent'] = extra
        return r

    def test_single_round_parity(self):
        rounds = [self._round(1, 0, 'tc_1', 'web_search', '{"q":"x"}', 'hit',
                              ac='Let me search.')]
        task = _task_with_rounds(rounds, content='Answer.')
        segs = assemble_segments(task)
        from_seg = reconstruct_tool_messages_from_segments(segs)
        from_rounds = _reconstruct_tool_call_messages(rounds)
        assert from_seg == from_rounds
        assert from_seg is not None

    def test_projection_stops_at_invalid_arguments_without_inventing_them(self):
        from lib.tool_history_projection import build_tool_history_round

        projected = build_tool_history_round([
            None,
            'corrupt',
            {
                'toolCallId': 'safe', 'toolName': 'read_files',
                'toolArgs': {'path': 'safe.py'},
                'toolContent': 'safe body',
            },
            {
                'toolCallId': 'invalid', 'toolName': 'read_files',
                'toolArgs': ['not', 'wire', 'json'],
                'toolContent': 'ok', 'assistantContent': {'bad': 'text'},
            },
            {
                'toolCallId': 'dependent', 'toolName': 'read_files',
                'toolArgs': '{}', 'toolContent': 'must not cross gap',
            },
        ])
        assert projected['assistantContent'] == ''
        assert projected['toolCalls'] == [{
            'id': 'safe', 'name': 'read_files',
            'arguments': '{"path":"safe.py"}',
        }]

        assert resume_prefill_from_segments(
            [None, {'type': 'text', 'terminal': False}, {
                'type': 'text', 'terminal': True, 'deliverable': True,
                'text': 'partial answer',
            }],
            'gpt-4o', finish_reason='aborted') == 'partial answer'
        assert resume_prefill_from_segments(
            [{'type': 'text', 'terminal': True, 'deliverable': True,
              'text': {'bad': 'text'}}],
            'gpt-4o', finish_reason='aborted') is None

    def test_assemble_ignores_non_text_sidecars(self):
        task = _task_with_rounds([{
            'roundNum': 1, 'llmRound': 0, 'toolCallId': 'valid',
            'toolName': 'read_files', 'toolArgs': '{}',
            'toolContent': 'ok', 'status': 'done',
            'assistantContent': ['bad'], 'thinking': {'bad': True},
            'thinkingSignature': ['bad'],
        }], content={'bad': 'terminal'}, thinking=['bad'])

        segments = assemble_segments(task)

        assert [segment['type'] for segment in segments] == ['tool_use']

    def test_multi_call_batch_parity(self):
        rounds = [
            self._round(1, 0, 'tc_1', 'grep_search', '{"p":"a"}', 'hitA',
                        ac='Searching.', think='reason', sig='sig-0'),
            self._round(2, 0, 'tc_2', 'read_files', '{"path":"b"}', 'bodyB'),
            self._round(3, 1, 'tc_3', 'apply_diff', '{"path":"b"}', 'ok',
                        ac='Applying fix.'),
        ]
        task = _task_with_rounds(rounds, content='Done.', thinking='final')
        segs = assemble_segments(task)
        assert reconstruct_tool_messages_from_segments(segs) == \
            _reconstruct_tool_call_messages(rounds)

    def test_gemini_extra_content_parity_after_rehydrate(self):
        """Invariant: Gemini extraContent (thin-stripped) is recovered via
        rehydrate before reconstruction → parity holds."""
        rounds = [self._round(1, 0, 'tc_1', 'web_search', '{}', 'ok',
                              extra={'google': {'thought_signature': 'gem'}})]
        task = _task_with_rounds(rounds, content='A.')
        segs = assemble_segments(task)
        # Simulate the persist→read boundary: thin then rehydrate against rounds.
        thin = segments_to_json(segs)
        rehydrated = rehydrate_segments(thin, rounds)
        from_seg = reconstruct_tool_messages_from_segments(rehydrated)
        from_rounds = _reconstruct_tool_call_messages(rounds)
        assert from_seg == from_rounds
        assert from_seg[0]['tool_calls'][0]['extra_content'] == \
            {'google': {'thought_signature': 'gem'}}

    def test_thin_without_rehydrate_drops_extra_content(self):
        """NC-ish: reconstructing from the THIN segments (no rehydrate) loses
        extraContent — proving rehydrate is load-bearing for Gemini replay."""
        rounds = [self._round(1, 0, 'tc_1', 'web_search', '{}', 'ok',
                              extra={'google': {'thought_signature': 'gem'}})]
        task = _task_with_rounds(rounds, content='A.')
        thin = segments_to_json(assemble_segments(task))
        from_thin = reconstruct_tool_messages_from_segments(thin)
        assert 'extra_content' not in from_thin[0]['tool_calls'][0]


class TestSegmentContinueGroundTruth:
    """Continue = checkpoint rounds + current rounds. The segment path merges
    them via _merge_tool_rounds (checkpoint+current, no double-count, inv #6)
    and drives byte-identical wire messages vs the toolRounds path."""

    def _mk_continue_task(self):
        ckpt = [{
            'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc_a',
            'toolName': 'web_search', 'toolArgs': '{"q":"x"}',
            'toolContent': 'result a', 'status': 'done',
            'assistantContent': 'Searching (pre-checkpoint).',
            'thinking': 'ckpt reason', 'thinkingSignature': 'ckpt-sig',
        }]
        cur = [{
            'roundNum': 2, 'llmRound': 1, 'toolCallId': 'tc_b',
            'toolName': 'fetch_url', 'toolArgs': '{"url":"https://x"}',
            'toolContent': 'page body', 'status': 'done',
            'assistantContent': 'Fetching the top hit.',
        }]
        return {'id': 'cont' + '0' * 28, 'convId': 'c' * 32,
                'content': 'Final synthesis.', 'thinking': '',
                '_checkpointToolRounds': ckpt, 'toolRounds': cur}

    def test_continue_segment_rebuild_matches_toolrounds(self):
        from lib.tasks_pkg.manager._persist import _merge_tool_rounds
        task = self._mk_continue_task()
        merged = _merge_tool_rounds(task)
        # Segment path: assemble over the merged rounds → reconstruct.
        segs = assemble_segments(task, merged=merged)
        from_seg = reconstruct_tool_messages_from_segments(segs)
        from_rounds = _reconstruct_tool_call_messages(merged)
        assert from_seg == from_rounds
        assert from_seg is not None

    def test_continue_no_double_count_invariant_6(self):
        """Segment merge uses checkpoint+current ordering exactly ONCE — the
        merged list has 2 rounds, and the segment path yields exactly 2
        tool_use → 2 assistant(tool_calls) messages (not 3-4 from double-count)."""
        from lib.tasks_pkg.manager._persist import _merge_tool_rounds
        task = self._mk_continue_task()
        merged = _merge_tool_rounds(task)
        assert len(merged) == 2  # checkpoint(1) + current(1), no double-count
        segs = assemble_segments(task, merged=merged)
        msgs = reconstruct_tool_messages_from_segments(segs)
        asst_tc = [m for m in msgs if m.get('tool_calls')]
        assert len(asst_tc) == 2
        # Ordering preserved: checkpoint tool first, current tool second.
        assert asst_tc[0]['tool_calls'][0]['function']['name'] == 'web_search'
        assert asst_tc[1]['tool_calls'][0]['function']['name'] == 'fetch_url'

    def test_continue_thinking_signature_carried_invariant_4(self):
        """The checkpoint round's thinking+signature survives the segment
        rebuild (inv #4: Claude needs both to replay a signed thinking block)."""
        task = self._mk_continue_task()
        segs = assemble_segments(task)
        msgs = reconstruct_tool_messages_from_segments(segs)
        asst0 = [m for m in msgs if m.get('tool_calls')][0]
        assert asst0['reasoning_content'] == 'ckpt reason'
        assert asst0['thinking_signature'] == 'ckpt-sig'

    def test_resumed_attempt_counter_reset_keeps_both_batches_distinct(self):
        """A resumed executor's round zero is not the prior task's round zero."""
        from lib.tasks_pkg.manager._persist import _merge_tool_rounds

        task = {
            'id': 'task-new', '_attemptId': 'attempt-new',
            'content': 'done', 'thinking': '',
            '_checkpointToolRounds': [{
                'roundNum': 1, 'llmRound': 0,
                'attemptId': 'attempt-old', 'taskId': 'task-old',
                'toolCallId': 'call-old', 'toolName': 'search_tools',
                'toolArgs': '{}', 'toolContent': 'old result',
                'status': 'done', 'assistantContent': 'old narration',
            }],
            'toolRounds': [{
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'call-new', 'toolName': 'read_tool_artifact',
                'toolArgs': '{}', 'toolContent': 'new result',
                'status': 'done', 'assistantContent': 'new narration',
            }],
        }

        merged = _merge_tool_rounds(task)
        assert [(item['attemptId'], item['taskId']) for item in merged] == [
            ('attempt-old', 'task-old'), ('attempt-new', 'task-new'),
        ]
        segments = assemble_segments(task, merged=merged)
        narration = [
            item for item in segments
            if item['type'] == 'text' and not item.get('deliverable')
        ]
        assert [item['text'] for item in narration] == [
            'old narration', 'new narration',
        ]
        assert [item['attemptId'] for item in narration] == [
            'attempt-old', 'attempt-new',
        ]
        assert len({item['blockId'] for item in narration}) == 2
        tools = [item for item in segments if item['type'] == 'tool_use']
        assert [(item['attemptId'], item['taskId']) for item in tools] == [
            ('attempt-old', 'task-old'), ('attempt-new', 'task-new'),
        ]
        rebuilt = reconstruct_tool_messages_from_segments(segments)
        assert [[call['id'] for call in message['tool_calls']]
                for message in rebuilt or [] if message.get('tool_calls')] == [
            ['call-old'], ['call-new'],
        ]

    def test_legacy_noncontiguous_counter_reset_does_not_swallow_narration(self):
        """Pre-migration projections have no attemptId but remain lossless."""
        rounds = [
            {
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call-a',
                'toolName': 'one', 'status': 'done',
                'assistantContent': 'first round zero',
            },
            {
                'roundNum': 2, 'llmRound': 1, 'toolCallId': 'call-b',
                'toolName': 'two', 'status': 'done',
            },
            {
                'roundNum': 1, 'llmRound': 0, 'toolCallId': 'call-c',
                'toolName': 'three', 'status': 'done',
                'assistantContent': 'resumed round zero',
            },
        ]
        segments = assemble_segments(
            {'content': '', 'thinking': ''}, merged=rounds,
        )
        assert [
            item['text'] for item in segments
            if item['type'] == 'text' and not item.get('deliverable')
        ] == ['first round zero', 'resumed round zero']

    def test_NC_segment_drops_signature_fails_inv_4(self):
        """NEUTER inv #4: strip the signature off the checkpoint thinking
        segment → the rebuilt assistant loses the signed thinking block
        (Claude would reject) → the parity assertion against the signed
        toolRounds path FAILS. Proves the signature carry is load-bearing."""
        from lib.tasks_pkg.segments import SEG_THINKING
        from lib.tasks_pkg.manager._persist import _merge_tool_rounds
        task = self._mk_continue_task()
        merged = _merge_tool_rounds(task)
        segs = assemble_segments(task, merged=merged)
        # Poison: drop the signature off the (non-terminal) thinking segment.
        for s in segs:
            if s.get('type') == SEG_THINKING and not s.get('terminal'):
                s.pop('signature', None)
        from_seg = reconstruct_tool_messages_from_segments(segs)
        from_rounds = _reconstruct_tool_call_messages(merged)
        # Signed rounds path keeps the signature; neutered segment path drops
        # it → the reconstructions DIVERGE.
        assert from_seg != from_rounds
        asst0 = [m for m in from_seg if m.get('tool_calls')][0]
        assert 'thinking_signature' not in asst0


class TestToolHistoryFromSegmentsParity:
    """A Continue rebuild driven from persisted segments (tool_history_from_
    segments → inject_tool_history) is byte-identical to the frontend-supplied
    toolHistory path — proving segments CAN drive inject_tool_history."""

    def test_inject_tool_history_parity_claude(self):
        # Frontend-supplied toolHistory (the live Continue path today).
        cfg_fe = {'toolHistory': [
            _th_round('tc_1', 'fetch_url', '{"url":"https://x"}', 'page body',
                      assistant_content='Fetching…',
                      thinking='reasoned', thinking_signature='sig-123')]}
        msgs_fe = _base_messages()
        inject_tool_history(msgs_fe, cfg_fe, _make_task(), 'claude-opus-4-7')

        # Segment-derived toolHistory (the step-4 capability).
        rounds = [{
            'roundNum': 1, 'llmRound': 0, 'toolCallId': 'tc_1',
            'toolName': 'fetch_url', 'toolArgs': '{"url":"https://x"}',
            'toolContent': 'page body', 'status': 'done',
            'assistantContent': 'Fetching…', 'thinking': 'reasoned',
            'thinkingSignature': 'sig-123'}]
        task = _task_with_rounds(rounds, content='A.')
        segs = assemble_segments(task)
        cfg_seg = {'toolHistory': tool_history_from_segments(segs)}
        msgs_seg = _base_messages()
        inject_tool_history(msgs_seg, cfg_seg, _make_task(), 'claude-opus-4-7')

        assert msgs_seg == msgs_fe

    def test_invalid_segment_origin_caller_is_rejected_at_projection_gate(self):
        segments = [{
            'type': 'tool_use', 'blockId': 'tool:owned',
            'id': 'owned', 'name': 'read_files', 'input': '{}',
            'llmRound': 0,
            'result': {'content': 'must not become root', 'status': 'done'},
            '_round': {
                'roundNum': 1, 'llmRound': 0,
                'caller': {'type': 'multi_agent'},
            },
        }]

        history = tool_history_from_segments(segments)
        assert history == []

        messages = _base_messages()
        assert inject_tool_history(
            messages, {'toolHistory': history}, _make_task(), 'gpt-4o') == 0
        assert messages == _base_messages()

    def test_discarded_segment_is_transparent_to_later_adopted_call(self):
        segments = [
            {
                'type': 'tool_use', 'blockId': 'tool:discarded',
                'id': 'discarded', 'name': 'read_files', 'input': '{}',
                'llmRound': 0,
                'result': {'content': None, 'status': 'aborted'},
                '_providerAttemptDiscarded': True,
            },
            {
                'type': 'tool_use', 'blockId': 'tool:adopted',
                'id': 'adopted', 'name': 'read_files', 'input': '{}',
                'llmRound': 0,
                'result': {'content': 'real result', 'status': 'done'},
            },
        ]

        history = tool_history_from_segments(segments)

        assert [[call['id'] for call in batch['toolCalls']]
                for batch in history] == [['adopted']]

    def test_malformed_segment_entries_do_not_crash_or_hide_valid_sibling(self):
        segments = [
            None,
            {
                'type': 'text', 'deliverable': False, 'terminal': False,
                'text': {'not': 'text'}, 'llmRound': 0,
            },
            {
                'type': 'tool_use', 'blockId': 'tool:ok',
                'id': 'ok', 'name': 'read_files', 'input': '{}',
                'llmRound': 0,
                'result': {'content': 'ok', 'status': 'done'},
                '_round': {'roundNum': 1, 'llmRound': 0},
            },
        ]

        messages = reconstruct_tool_messages_from_segments(segments)
        assert messages is not None
        assert messages[0]['tool_calls'][0]['id'] == 'ok'
        assert messages[1]['content'] == 'ok'

    def test_attempt_local_rounds_remain_distinct_after_segment_projection(self):
        rounds = [
            {
                'attemptId': 'attempt-old', 'taskId': 'task-old',
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'old-r0', 'toolName': 'read_files',
                'toolArgs': '{}', 'toolContent': 'old result',
                'status': 'done', 'assistantContent': 'old narration',
            },
            {
                'attemptId': 'attempt-new', 'taskId': 'task-new',
                'roundNum': 1, 'llmRound': 0,
                'toolCallId': 'new-r0', 'toolName': 'read_files',
                'toolArgs': '{}', 'toolContent': 'new result',
                'status': 'done', 'assistantContent': 'new narration',
            },
        ]
        segments = assemble_segments(
            {'content': '', 'thinking': ''}, merged=rounds)
        history = tool_history_from_segments(segments)

        assert [[call['id'] for call in batch['toolCalls']]
                for batch in history] == [['old-r0'], ['new-r0']]
        assert [batch['assistantContent'] for batch in history] == [
            'old narration', 'new narration',
        ]


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
