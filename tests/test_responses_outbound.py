"""Tests for lib/llm/responses_outbound — the third wire protocol.

The Responses-API boundary layer extracted from lib/oauth/codex.py (S1 of
epic pt_b7a29ea7): ONE converter pair + ONE SSE translator shared by the
Codex-OAuth path (profile='codex') and generic Responses providers like
DeepSeek-V4-Flash (profile='default').

Golden-sample coverage (no network, no Flask, no DB):
  * request conversion  — messages→input items, tools, profiles, images
  * SSE translation     — text / reasoning_text / single tool / PARALLEL
                          tools (item_id routing) / failed / incomplete
  * non-stream back-conversion
  * wiring              — prepare_request single gate, codex dispatcher
                          coercion, chat() non-stream path
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.llm._sse_core import SSEAccumulator
from lib.llm.diagnostics import RawSSEDumper
from lib.llm.responses_outbound import (
    ResponsesSSETranslator,
    openai_body_to_responses,
    responses_response_to_openai,
    responses_url,
)
from lib.llm_errors import RateLimitError, RetryableAPIError

pytestmark = pytest.mark.unit


# ──────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────

def _acc(translator, model='deepseek-v4-flash', **kw):
    body = {'model': model, 'messages': []}
    return SSEAccumulator(
        body, 'trace', RawSSEDumper(model, 'trace', body),
        translator, time.time(), **kw)


def _feed(acc, events):
    for ev in events:
        if acc.feed_line('data: ' + json.dumps(ev)):
            break


def _text(t):
    return {'type': 'response.output_text.delta', 'delta': t}


def _reason_text(t):   # DeepSeek flavor (no summary channel)
    return {'type': 'response.reasoning_text.delta', 'delta': t}


def _reason_summary(t):  # OpenAI flavor
    return {'type': 'response.reasoning_summary_text.delta', 'delta': t}


def _fn_added(call_id, name, item_id=''):
    item = {'type': 'function_call', 'call_id': call_id, 'name': name}
    if item_id:
        item['id'] = item_id
    return {'type': 'response.output_item.added', 'item': item}


def _fn_args(delta, item_id=''):
    ev = {'type': 'response.function_call_arguments.delta', 'delta': delta}
    if item_id:
        ev['item_id'] = item_id
    return ev


def _completed(usage=None):
    return {'type': 'response.completed',
            'response': {'status': 'completed', 'output': [],
                         'usage': usage or {'input_tokens': 10,
                                            'output_tokens': 5}}}


# ──────────────────────────────────────────────────────────────
#  Request conversion
# ──────────────────────────────────────────────────────────────

class TestRequestConversion:
    def test_default_profile_keeps_sampling_params(self):
        body = {'model': 'deepseek-v4-flash',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'temperature': 0.7, 'top_p': 0.9, 'max_tokens': 512}
        out, _rev = openai_body_to_responses(body, profile='default', stream=True)
        assert out['temperature'] == 0.7
        assert out['top_p'] == 0.9
        assert out['max_output_tokens'] == 512
        assert 'max_tokens' not in out
        assert 'instructions' not in out          # default omits the field
        assert out['store'] is False              # stateless, always
        assert out['stream'] is True
        assert 'include' not in out               # no encrypted reasoning ask

    def test_codex_profile_drops_params_and_sets_codex_fields(self):
        body = {'model': 'gpt-5.2-codex',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'temperature': 0.7, 'top_p': 0.9, 'max_tokens': 512,
                'reasoning_effort': 'high'}
        out, _rev = openai_body_to_responses(body, profile='codex', stream=True)
        assert 'temperature' not in out
        assert 'top_p' not in out
        assert 'max_tokens' not in out and 'max_output_tokens' not in out
        assert out['instructions'] == ''
        assert out['store'] is False
        assert out['include'] == ['reasoning.encrypted_content']
        assert out['reasoning'] == {'effort': 'high', 'summary': 'auto'}
        assert out['parallel_tool_calls'] is True

    def test_default_reasoning_effort_without_summary(self):
        body = {'model': 'deepseek-v4-flash', 'messages': [],
                'reasoning_effort': 'low'}
        out, _rev = openai_body_to_responses(body, profile='default')
        assert out['reasoning'] == {'effort': 'low'}   # DeepSeek: no summary

    def test_codex_reasoning_defaults_medium(self):
        out, _rev = openai_body_to_responses(
            {'model': 'gpt-5.2-codex', 'messages': []}, profile='codex')
        assert out['reasoning'] == {'effort': 'medium', 'summary': 'auto'}

    def test_assistant_content_and_tool_calls_both_emitted(self):
        """An assistant turn with text AND tool_calls must produce the
        message item AND the function_call items — dropping either half
        breaks multi-turn replay."""
        body = {'model': 'm', 'messages': [
            {'role': 'assistant', 'content': 'Let me check.',
             'tool_calls': [{'id': 'call_1', 'type': 'function',
                             'function': {'name': 'read_files',
                                          'arguments': '{"path":"a.py"}'}}]}]}
        out, _rev = openai_body_to_responses(body, profile='default')
        inp = out['input']
        assert inp[0] == {'type': 'message', 'role': 'assistant',
                          'content': [{'type': 'output_text',
                                       'text': 'Let me check.'}]}
        assert inp[1] == {'type': 'function_call', 'call_id': 'call_1',
                          'name': 'read_files',
                          'arguments': '{"path":"a.py"}'}

    def test_messages_to_input_items(self):
        body = {'model': 'm', 'messages': [
            {'role': 'system', 'content': 'be terse'},
            {'role': 'user', 'content': 'hello'},
            {'role': 'assistant', 'content': 'hi there'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'function': {'name': 'read_files',
                             'arguments': '{"path":"a.py"}'}}]},
            {'role': 'tool', 'tool_call_id': 'call_1', 'content': 'FILE'},
            {'role': 'user', 'content': 'and?'},
        ]}
        out, _rev = openai_body_to_responses(body, profile='default')
        inp = out['input']
        assert inp[0] == {'type': 'message', 'role': 'developer',
                          'content': [{'type': 'input_text', 'text': 'be terse'}]}
        assert inp[1]['role'] == 'user'
        assert inp[2] == {'type': 'message', 'role': 'assistant',
                          'content': [{'type': 'output_text', 'text': 'hi there'}]}
        # bare tool_calls assistant → top-level function_call item
        assert inp[3] == {'type': 'function_call', 'call_id': 'call_1',
                          'name': 'read_files',
                          'arguments': '{"path":"a.py"}'}
        # tool result keyed by call_id, not by position
        assert inp[4] == {'type': 'function_call_output',
                          'call_id': 'call_1', 'output': 'FILE'}
        assert inp[5]['role'] == 'user'

    def test_tools_flatten_and_tool_choice(self):
        body = {'model': 'm', 'messages': [],
                'tools': [{'type': 'function',
                           'function': {'name': 'grep_search',
                                        'description': 'search',
                                        'parameters': {'type': 'object'}}}],
                'tool_choice': {'type': 'function',
                                'function': {'name': 'grep_search'}}}
        out, _rev = openai_body_to_responses(body, profile='default')
        assert out['tools'] == [{'type': 'function', 'name': 'grep_search',
                                 'description': 'search',
                                 'parameters': {'type': 'object'}}]
        assert out['tool_choice'] == {'type': 'function', 'name': 'grep_search'}

    def test_image_block_to_input_image(self):
        body = {'model': 'm', 'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'what is this?'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AA'}}]}]}
        out, _rev = openai_body_to_responses(body, profile='default')
        parts = out['input'][0]['content']
        assert parts[0] == {'type': 'input_text', 'text': 'what is this?'}
        assert parts[1] == {'type': 'input_image',
                            'image_url': 'data:image/png;base64,AA'}

    def test_internal_keys_never_leak(self):
        body = {'model': 'm', 'messages': [], '_task_id': 't123'}
        out, _rev = openai_body_to_responses(body, profile='default')
        assert '_task_id' not in out


# ──────────────────────────────────────────────────────────────
#  SSE translation (through the shared accumulator)
# ──────────────────────────────────────────────────────────────

class TestSSETranslation:
    def test_text_stream_and_usage_with_cached_tokens(self):
        acc = _acc(ResponsesSSETranslator(model='deepseek-v4-flash'))
        _feed(acc, [
            {'type': 'response.created', 'response': {'id': 'resp_1'}},
            _text('你好'), _text('，世界'),
            _completed({'input_tokens': 100, 'output_tokens': 7,
                        'total_tokens': 107,
                        'input_tokens_details': {'cached_tokens': 64},
                        'output_tokens_details': {'reasoning_tokens': 3}}),
        ])
        msg, finish, usage = acc.finalize()
        assert msg['content'] == '你好，世界'
        assert finish == 'stop'
        assert usage['prompt_tokens'] == 100
        assert usage['completion_tokens'] == 7
        assert usage['total_tokens'] == 107
        assert usage['prompt_tokens_details'] == {'cached_tokens': 64}
        assert usage['completion_tokens_details'] == {'reasoning_tokens': 3}

    def test_reasoning_text_delta_maps_to_reasoning_content(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [_reason_text('先想'), _reason_text('再想'),
                    _text('答案'), _completed()])
        msg, _f, _u = acc.finalize()
        assert msg['reasoning_content'] == '先想再想'
        assert msg['content'] == '答案'

    def test_reasoning_summary_delta_still_supported(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [_reason_summary('thinking…'), _text('done'), _completed()])
        msg, _f, _u = acc.finalize()
        assert msg['reasoning_content'] == 'thinking…'

    def test_single_tool_call(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _fn_added('call_9', 'grep_search', item_id='fc_1'),
            _fn_args('{"pat', item_id='fc_1'),
            _fn_args('tern":"x"}', item_id='fc_1'),
            _completed(),
        ])
        msg, finish, _u = acc.finalize()
        assert finish == 'tool_calls'
        tc = msg['tool_calls'][0]
        assert tc['id'] == 'call_9'
        assert tc['function']['name'] == 'grep_search'
        assert tc['function']['arguments'] == '{"pattern":"x"}'

    def test_parallel_tool_calls_route_arguments_by_item_id(self):
        """THE item_id exam: interleaved argument deltas of two parallel
        calls must land in their own calls — routing by 'current index'
        (the old codex behaviour) concatenates them into one."""
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _fn_added('call_a', 'read_files', item_id='fc_1'),
            _fn_added('call_b', 'grep_search', item_id='fc_2'),
            _fn_args('{"path":"a.', item_id='fc_1'),
            _fn_args('{"pattern":"x', item_id='fc_2'),
            _fn_args('py"}', item_id='fc_1'),
            _fn_args('"}', item_id='fc_2'),
            _completed(),
        ])
        msg, finish, _u = acc.finalize()
        assert finish == 'tool_calls'
        tcs = sorted(msg['tool_calls'], key=lambda t: t['id'])
        assert tcs[0]['function']['arguments'] == '{"path":"a.py"}'
        assert tcs[0]['function']['name'] == 'read_files'
        assert tcs[1]['function']['arguments'] == '{"pattern":"x"}'
        assert tcs[1]['function']['name'] == 'grep_search'

    def test_arguments_delta_without_item_id_falls_back_to_current(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _fn_added('call_1', 'read_files'),          # no item id
            _fn_args('{"path":"a.py"}'),                # no item id
            _completed(),
        ])
        msg, finish, _u = acc.finalize()
        assert msg['tool_calls'][0]['function']['arguments'] == '{"path":"a.py"}'

    def test_failed_rate_limit_raises_typed_error(self):
        acc = _acc(ResponsesSSETranslator())
        with pytest.raises(RateLimitError):
            _feed(acc, [
                _text('partial'),
                {'type': 'response.failed',
                 'response': {'status': 'failed',
                              'error': {'code': 'rate_limit_exceeded',
                                        'message': 'Too many requests'}}},
            ])

    def test_failed_generic_raises_retryable_or_error(self):
        acc = _acc(ResponsesSSETranslator())
        with pytest.raises((RetryableAPIError, Exception)):
            _feed(acc, [
                {'type': 'response.failed',
                 'response': {'status': 'failed',
                              'error': {'code': 'server_error',
                                        'message': 'upstream melted'}}}])

    def test_incomplete_max_output_tokens_finish_length(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _text('truncated…'),
            {'type': 'response.incomplete',
             'response': {'status': 'incomplete',
                          'incomplete_details': {'reason': 'max_output_tokens'},
                          'usage': {'input_tokens': 5, 'output_tokens': 16}}},
        ])
        msg, finish, usage = acc.finalize()
        assert finish == 'length'
        assert msg['content'] == 'truncated…'
        assert usage['completion_tokens'] == 16

    def test_unknown_events_tolerated(self):
        """web_search_call.* / reasoning items / content_part lifecycle —
        none may crash the translator."""
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            {'type': 'response.in_progress', 'response': {}},
            {'type': 'response.output_item.added',
             'item': {'type': 'reasoning', 'id': 'rs_1'}},
            {'type': 'response.web_search_call.in_progress',
             'item_id': 'ws_1'},
            {'type': 'response.web_search_call.searching',
             'item_id': 'ws_1'},
            {'type': 'response.content_part.added',
             'item_id': 'msg_1', 'part': {'type': 'output_text', 'text': ''}},
            _text('ok'),
            {'type': 'response.content_part.done', 'item_id': 'msg_1',
             'part': {'type': 'output_text', 'text': 'ok'}},
            {'type': 'response.output_item.done',
             'item': {'type': 'message', 'id': 'msg_1'}},
            _completed(),
        ])
        msg, finish, _u = acc.finalize()
        assert msg['content'] == 'ok'
        assert finish == 'stop'


# ──────────────────────────────────────────────────────────────
#  Non-stream back-conversion
# ──────────────────────────────────────────────────────────────

class TestFromResponses:
    def test_message_tool_calls_reasoning_usage(self):
        data = {
            'status': 'completed',
            'output': [
                {'type': 'reasoning',
                 'summary': [{'type': 'summary_text', 'text': '想了下'}]},
                {'type': 'message', 'content': [
                    {'type': 'output_text', 'text': '前半'},
                    {'type': 'output_text', 'text': '后半'}]},
                {'type': 'function_call', 'call_id': 'call_1',
                 'name': 'read_files', 'arguments': '{"path":"a.py"}'},
            ],
            'usage': {'input_tokens': 20, 'output_tokens': 9,
                      'total_tokens': 29,
                      'input_tokens_details': {'cached_tokens': 11}},
        }
        out = responses_response_to_openai(data)
        ch = out['choices'][0]
        assert ch['message']['content'] == '前半\n后半'
        assert ch['message']['reasoning_content'] == '想了下'
        assert ch['finish_reason'] == 'tool_calls'
        tc = ch['message']['tool_calls'][0]
        assert tc['id'] == 'call_1'
        assert tc['function']['name'] == 'read_files'
        assert out['usage']['prompt_tokens_details'] == {'cached_tokens': 11}

    def test_failed_status_yields_error_envelope(self):
        out = responses_response_to_openai({
            'status': 'failed',
            'error': {'code': 'rate_limit_exceeded', 'message': 'slow down'}})
        assert 'error' in out
        assert 'slow down' in out['error']['message']

    def test_incomplete_max_tokens_finish_length(self):
        out = responses_response_to_openai({
            'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens'},
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'cut'}]}]})
        assert out['choices'][0]['finish_reason'] == 'length'


# ──────────────────────────────────────────────────────────────
#  Tool-name truncation reverse map (pt_1e1b2d3215e14c54)
#
#  64 chars is the OpenAI function-name limit — EVERY Responses
#  upstream enforces it, so long MCP tool names are truncated on the
#  way out. Without a per-request reverse map the model echoes the
#  TRUNCATED name and the executor's tool lookup misses — the exact
#  shape the anthropic cloak path already solves with
#  ``tool_name_reverse``. Mirrors that pattern: the converter records
#  {truncated: original}, the map rides the translator, names are
#  restored on the response side (stream AND non-stream).
# ──────────────────────────────────────────────────────────────

_LONG_TOOL = 'mcp__some_mcp_server__' + 'x' * 60   # 78 chars > 64


class TestToolNameReverseMap:
    def test_converter_records_truncation_in_reverse_map(self):
        body = {'model': 'm', 'messages': [],
                'tools': [{'type': 'function', 'function': {
                    'name': _LONG_TOOL, 'description': 'd',
                    'parameters': {'type': 'object'}}}],
                'tool_choice': {'type': 'function',
                                'function': {'name': _LONG_TOOL}}}
        out, rev = openai_body_to_responses(body, profile='default')
        truncated = out['tools'][0]['name']
        assert len(truncated) == 64
        assert out['tool_choice']['name'] == truncated
        assert rev == {truncated: _LONG_TOOL}

    def test_short_names_yield_empty_map(self):
        _out, rev = openai_body_to_responses(
            {'model': 'm', 'messages': [],
             'tools': [{'type': 'function',
                        'function': {'name': 'read_files'}}]},
            profile='default')
        assert rev == {}

    def test_assistant_tool_call_names_recorded(self):
        body = {'model': 'm', 'messages': [
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'c1', 'type': 'function',
                'function': {'name': _LONG_TOOL, 'arguments': '{}'}}]}]}
        out, rev = openai_body_to_responses(body, profile='default')
        fc = out['input'][0]
        assert len(fc['name']) == 64
        assert rev[fc['name']] == _LONG_TOOL

    def test_stream_translator_restores_truncated_name(self):
        tr = ResponsesSSETranslator(model='m')
        truncated = _LONG_TOOL[:64]
        tr.tool_name_reverse = {truncated: _LONG_TOOL}
        acc = _acc(tr)
        _feed(acc, [
            _fn_added('call_1', truncated, item_id='fc_1'),
            _fn_args('{}', item_id='fc_1'),
            _completed(),
        ])
        msg, finish, _u = acc.finalize()
        assert finish == 'tool_calls'
        assert msg['tool_calls'][0]['function']['name'] == _LONG_TOOL

    def test_nonstream_restores_truncated_name(self):
        truncated = _LONG_TOOL[:64]
        data = {'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': 'c1',
             'name': truncated, 'arguments': '{}'}]}
        out = responses_response_to_openai(
            data, tool_name_reverse={truncated: _LONG_TOOL})
        tc = out['choices'][0]['message']['tool_calls'][0]
        assert tc['function']['name'] == _LONG_TOOL

    def test_codex_facade_failed_event_maps_to_typed_error(self):
        """pt_6d749150 close-out proof: the CODEX FACADE path (not just the
        new class) maps response.failed to the typed error ladder."""
        from lib.oauth.codex import CodexSSETranslator as FacadeTranslator
        acc = _acc(FacadeTranslator(model='gpt-5.2-codex'))
        with pytest.raises(RateLimitError):
            _feed(acc, [
                {'type': 'response.failed',
                 'response': {'status': 'failed',
                              'error': {'code': 'rate_limit_exceeded',
                                        'message': 'Too many requests'}}},
            ])


# ──────────────────────────────────────────────────────────────
#  URL + codex facade
# ──────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────
#  URL + codex facade
# ──────────────────────────────────────────────────────────────

class TestURLAndFacade:
    def test_responses_url(self):
        assert responses_url('https://api.deepseek.com/v1') == \
            'https://api.deepseek.com/v1/responses'
        assert responses_url('https://api.deepseek.com/v1/') == \
            'https://api.deepseek.com/v1/responses'

    def test_codex_facade_parity(self):
        """lib.oauth.codex re-exports the SAME translator/converter — the
        legacy test suite drives them through this facade."""
        from lib.oauth.codex import (
            CodexSSETranslator, codex_translate_request)
        body = {'model': 'gpt-5.2-codex',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'temperature': 0.5, 'max_tokens': 128}
        out = codex_translate_request(body)
        assert out['instructions'] == ''
        assert out['store'] is False
        assert out['stream'] is True
        assert 'temperature' not in out
        assert out['include'] == ['reasoning.encrypted_content']
        # translator class is the unified one
        assert CodexSSETranslator is ResponsesSSETranslator


# ──────────────────────────────────────────────────────────────
#  Wiring: single gate in prepare_request
# ──────────────────────────────────────────────────────────────

class TestPrepareRequestGate:
    def test_responses_protocol_translates_and_reurls(self):
        from lib.llm._sse_core import prepare_request
        body = {'model': 'deepseek-v4-flash',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'temperature': 0.3, 'max_tokens': 64}
        plan = prepare_request(
            body, api_key='k', base_url='https://api.deepseek.com/v1',
            api_protocol='responses')
        assert plan.url == 'https://api.deepseek.com/v1/responses'
        assert plan.wire_translator is not None
        assert 'input' in plan.body and 'messages' not in plan.body
        assert plan.body['temperature'] == 0.3
        assert plan.body['max_output_tokens'] == 64
        assert plan.body['store'] is False

    def test_codex_oauth_slot_uses_codex_profile(self, monkeypatch):
        """oauth='codex' + protocol='responses' → codex profile (instructions,
        include) — token resolution mocked out."""
        monkeypatch.setattr(
            'lib.oauth.outbound.resolve_oauth_request',
            lambda oauth, body, extra_headers: ('TOK', {}, body))
        from lib.llm._sse_core import prepare_request
        body = {'model': 'gpt-5.2-codex',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'temperature': 0.3}
        plan = prepare_request(
            body, api_key='k',
            base_url='https://chatgpt.com/backend-api/codex',
            api_protocol='responses', oauth='codex')
        assert plan.url == 'https://chatgpt.com/backend-api/codex/responses'
        assert plan.body['instructions'] == ''
        assert plan.body['include'] == ['reasoning.encrypted_content']
        assert 'temperature' not in plan.body

    def test_no_url_sniffing_without_protocol(self):
        """A codex-shaped base_url WITHOUT protocol='responses' must NOT be
        translated — the old URL sniff is gone (single gate)."""
        from lib.llm._sse_core import prepare_request
        body = {'model': 'm', 'messages': [{'role': 'user', 'content': 'hi'}]}
        plan = prepare_request(
            body, api_key='k',
            base_url='https://chatgpt.com/backend-api/codex',
            api_protocol='openai')
        assert plan.url.endswith('/chat/completions')
        assert 'messages' in plan.body
        assert plan.wire_translator is None

    def test_anthropic_branch_untouched(self):
        from lib.llm._sse_core import prepare_request
        body = {'model': 'claude-opus-4-5-20251101',
                'messages': [{'role': 'user', 'content': 'hi'}],
                'max_tokens': 64}
        plan = prepare_request(
            body, api_key='k', base_url='https://api.anthropic.com/v1',
            api_protocol='anthropic')
        assert plan.url == 'https://api.anthropic.com/v1/messages'
        assert plan.wire_translator is not None
        assert 'messages' in plan.body   # anthropic keeps messages key

    def test_dispatcher_coerces_codex_oauth_to_responses(self):
        from lib.llm_dispatch.dispatcher import _oauth_wire_protocol
        assert _oauth_wire_protocol({'oauth': 'codex'}) == 'responses'
        assert _oauth_wire_protocol({'oauth': 'claude'}) == ''
        assert _oauth_wire_protocol({}) == ''
        assert _oauth_wire_protocol({'oauth': 'codex',
                                     'protocol': 'openai'}) == 'responses'


# ──────────────────────────────────────────────────────────────
#  chat() non-stream wiring
# ──────────────────────────────────────────────────────────────

class TestChatNonStream:
    def test_chat_responses_round_trip(self, monkeypatch):
        import importlib
        # lib.llm.chat the MODULE — the package facade re-exports the chat
        # FUNCTION under the same name, so a plain import binds the function.
        chat_mod = importlib.import_module('lib.llm.chat')
        captured = {}

        class _Resp:
            status_code = 200
            headers = {}
            text = '{}'

            def json(self):
                return {'status': 'completed',
                        'output': [{'type': 'message', 'content': [
                            {'type': 'output_text', 'text': 'pong'}]}],
                        'usage': {'input_tokens': 3, 'output_tokens': 2,
                                  'total_tokens': 5}}

        def _fake_post(url, **kw):
            captured['url'] = url
            captured['json'] = kw.get('json')
            return _Resp()

        monkeypatch.setattr(chat_mod, 'http_post', _fake_post)
        content, usage = chat_mod.chat(
            [{'role': 'user', 'content': 'ping'}], 'deepseek-v4-flash',
            max_tokens=64, api_key='k',
            base_url='https://api.deepseek.com/v1', api_protocol='responses')
        assert captured['url'] == 'https://api.deepseek.com/v1/responses'
        assert 'input' in captured['json']
        assert 'messages' not in captured['json']
        assert captured['json']['stream'] is False
        assert captured['json']['max_output_tokens'] == 64
        assert content == 'pong'
        assert usage['total_tokens'] == 5

    def test_chat_responses_failed_raises(self, monkeypatch):
        import importlib
        chat_mod = importlib.import_module('lib.llm.chat')

        class _Resp:
            status_code = 200
            headers = {}
            text = '{}'

            def json(self):
                return {'status': 'failed',
                        'error': {'code': 'server_error',
                                  'message': 'melted upstream'}}

        monkeypatch.setattr(chat_mod, 'http_post', lambda url, **kw: _Resp())
        with pytest.raises(Exception) as ei:
            chat_mod.chat([{'role': 'user', 'content': 'ping'}],
                          'deepseek-v4-flash', api_key='k',
                          base_url='https://api.deepseek.com/v1',
                          api_protocol='responses')
        assert 'melted upstream' in str(ei.value)


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v']))
