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
import uuid

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

    @pytest.mark.parametrize(('model', 'incoming', 'expected'), [
        ('gpt-5.4', 'minimal', 'none'),
        ('gpt-5.4', 'max', 'xhigh'),
        ('gpt-5.6-sol', 'ultra', 'max'),
    ])
    def test_codex_reasoning_normalizes_to_subscription_registry(
            self, model, incoming, expected):
        out, _rev = openai_body_to_responses(
            {'model': model, 'messages': [],
             'reasoning_effort': incoming},
            profile='codex', stream=True)
        assert out['reasoning']['effort'] == expected

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

    def test_recycled_call_id_uses_occurrence_local_tool_name(self):
        from lib.llm.responses_outbound._to_responses import _messages_to_input

        messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'call_0', 'type': 'function',
                'function': {'name': 'run_command', 'arguments': '{}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'call_0', 'content': 'plain'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'call_0', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'call_0', 'content': 'file'},
        ]

        items = _messages_to_input(
            messages, {}, programmatic_tool_names={'read_files'})
        outputs = [item['output'] for item in items
                   if item.get('type') == 'function_call_output']
        assert outputs[0] == 'plain'
        assert json.loads(outputs[1]) == {
            'content': 'file', 'truncated': False,
        }

    def test_nonadjacent_recycled_result_does_not_borrow_tool_name(self):
        from lib.llm.responses_outbound._to_responses import _messages_to_input

        messages = [
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'call_0', 'type': 'function',
                'function': {'name': 'read_files', 'arguments': '{}'},
            }]},
            {'role': 'user', 'content': 'protocol interruption'},
            {'role': 'tool', 'tool_call_id': 'call_0', 'content': 'orphan'},
        ]

        items = _messages_to_input(
            messages, {}, programmatic_tool_names={'read_files'})
        output = next(item['output'] for item in items
                      if item.get('type') == 'function_call_output')
        assert output == 'orphan'

    def test_program_result_recovers_occurrence_caller_after_body_clean(self):
        from lib.llm import build_body

        caller = {'type': 'program', 'caller_id': 'program_1'}
        messages = [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'child_1', 'type': 'function', 'caller': caller,
                'function': {'name': 'read_files', 'arguments': '{}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'child_1', 'caller': caller,
             'content': 'file'},
        ]
        canonical = build_body('gpt-5.6-sol', messages, stream=True)
        # The generic OpenAI cleaner intentionally removes top-level private
        # fields; the adjacent assistant occurrence remains the authority.
        assert 'caller' not in canonical['messages'][2]

        wire, _reverse = openai_body_to_responses(
            canonical, profile='default')
        output = next(item for item in wire['input']
                      if item.get('type') == 'function_call_output')
        assert output['caller'] == caller

    def test_recycled_id_result_callers_are_paired_by_occurrence(self):
        from lib.llm.responses_outbound._to_responses import _messages_to_input

        messages = []
        for parent_id in ('program_1', 'program_2'):
            caller = {'type': 'program', 'caller_id': parent_id}
            messages.extend([
                {'role': 'assistant', 'content': '', 'tool_calls': [{
                    'id': 'call_0', 'type': 'function', 'caller': caller,
                    'function': {'name': 'read_files', 'arguments': '{}'},
                }]},
                {'role': 'tool', 'tool_call_id': 'call_0', 'content': parent_id},
            ])

        items = _messages_to_input(messages, {})
        outputs = [item for item in items
                   if item.get('type') == 'function_call_output']
        assert [item['caller']['caller_id'] for item in outputs] == [
            'program_1', 'program_2']

    def test_invalid_outbound_caller_is_not_promoted_to_root(self):
        from lib.llm.responses_outbound._to_responses import _messages_to_input

        items = _messages_to_input([
            {'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': 'call_1', 'type': 'function',
                'caller': 'not-an-object',
                'function': {'name': 'run_command', 'arguments': '{}'},
            }]},
            {'role': 'tool', 'tool_call_id': 'call_1',
             'caller': 'also-not-an-object', 'content': 'rejected'},
        ], {})
        function_call = next(item for item in items
                             if item.get('type') == 'function_call')
        output = next(item for item in items
                      if item.get('type') == 'function_call_output')
        assert function_call['caller'] == 'not-an-object'
        assert output['caller'] == 'also-not-an-object'

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
        body = {'model': 'm', 'messages': [], '_task_id': 't123',
                '_conv_id': 'c123', '_working_set_tokens': 128000,
                '_admitted_input_tokens': 12345,
                '_tool_search_mode': 'auto',
                '_frontend_selected_tool_names': ['x'],
                '_tool_namespace_by_name': {'x': 'custom'},
                '_responses_transport': 'websocket',
                '_reasoning_mode': 'pro', '_text_verbosity': 'high',
                '_image_detail': 'original',
                '_multi_agent_mode': 'read_only',
                '_safety_identifier': 'private',
                '_responses_feature_profile': 'compatible'}
        out, _rev = openai_body_to_responses(body, profile='default')
        assert '_task_id' not in out
        assert '_conv_id' not in out
        assert '_working_set_tokens' not in out
        assert not any(key.startswith('_') for key in out)

    def test_gpt56_explicit_cache_floor_and_stable_hashed_namespace(self):
        body = {'model': 'gpt-5.6-sol', '_task_id': 'task-secret',
                '_responses_feature_profile': 'openai',
                '_conv_id': 'conversation-secret',
                '_working_set_tokens': 128000,
                'messages': [
                    {'role': 'system', 'content': 'stable instruction'},
                    {'role': 'user', 'content': 'dynamic question'},
                ]}
        out, _rev = openai_body_to_responses(body, profile='default')
        assert str(uuid.UUID(out['prompt_cache_key'])) == out['prompt_cache_key']
        assert 'conversation-secret' not in out['prompt_cache_key']
        marker = out['input'][0]['content'][0]['prompt_cache_breakpoint']
        assert marker == {'mode': 'explicit'}
        assert out['context_management'] == [{
            'type': 'compaction', 'compact_threshold': 128000}]
        assert 'reasoning.encrypted_content' in out['include']
        assert out['reasoning']['context'] == 'all_turns'

        same, _ = openai_body_to_responses(dict(body), profile='default')
        assert same['prompt_cache_key'] == out['prompt_cache_key']

    def test_codex_gpt56_omits_public_only_cache_and_compaction_fields(self):
        body = {'model': 'gpt-5.6-luna', '_conv_id': 'conversation-secret',
                '_working_set_tokens': 128000,
                'messages': [
                    {'role': 'system', 'content': 'stable instruction'},
                    {'role': 'user', 'content': 'dynamic question'},
                ]}
        out, _rev = openai_body_to_responses(body, profile='codex')

        # Codex CLI's subscription protocol keeps the stable cache namespace
        # and reasoning replay, but chatgpt.com's Codex backend rejects the
        # public Responses API's explicit marker on Luna.
        assert str(uuid.UUID(out['prompt_cache_key'])) == out['prompt_cache_key']
        assert out['reasoning']['context'] == 'all_turns'
        assert 'reasoning.encrypted_content' in out['include']
        assert 'prompt_cache_breakpoint' not in out['input'][0]['content'][0]
        assert 'context_management' not in out

    def test_gpt56_fields_do_not_leak_to_generic_responses_provider(self):
        body = {'model': 'deepseek-v4-flash', '_conv_id': 'c',
                '_working_set_tokens': 128000,
                'messages': [{'role': 'system', 'content': 'instruction'}]}
        out, _rev = openai_body_to_responses(body, profile='default')
        assert 'prompt_cache_key' not in out
        assert 'context_management' not in out
        assert 'prompt_cache_breakpoint' not in out['input'][0]['content'][0]

    def test_response_format_maps_to_responses_text_format(self):
        out, _ = openai_body_to_responses({
            'model': 'gpt-5.6-sol',
            '_responses_feature_profile': 'openai',
            'messages': [{'role': 'user', 'content': 'return json'}],
            'response_format': {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'answer', 'strict': True,
                    'schema': {
                        'type': 'object',
                        'properties': {'ok': {'type': 'boolean'}},
                        'required': ['ok'], 'additionalProperties': False,
                    },
                },
            },
        })
        assert out['text']['format']['type'] == 'json_schema'
        assert out['text']['format']['name'] == 'answer'
        assert out['text']['format']['strict'] is True
        assert out['text']['format']['schema']['required'] == ['ok']
        assert 'response_format' not in out

    def test_gpt56_pro_verbosity_safety_and_original_image_detail(self):
        out, _ = openai_body_to_responses({
            'model': 'gpt-5.6-sol',
            '_responses_feature_profile': 'openai',
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': 'inspect'},
                {'type': 'image_url',
                 'image_url': {'url': 'data:image/png;base64,AA'}},
            ]}],
            '_reasoning_mode': 'pro', '_text_verbosity': 'low',
            '_image_detail': 'original',
            '_safety_identifier': 'tofu_0123456789abcdef',
        })
        assert out['reasoning']['mode'] == 'pro'
        assert out['text']['verbosity'] == 'low'
        assert out['safety_identifier'] == 'tofu_0123456789abcdef'
        image = out['input'][0]['content'][1]
        assert image['type'] == 'input_image'
        assert image['detail'] == 'original'

    def test_tool_search_defers_only_non_pinned_catalog(self):
        tools = [{
            'type': 'function',
            'function': {
                'name': f'tool_{index}', 'description': f'tool {index}',
                'parameters': {'type': 'object', 'properties': {}},
            },
        } for index in range(24)]
        out, _ = openai_body_to_responses({
            'model': 'gpt-5.6-sol',
            '_responses_feature_profile': 'openai',
            'messages': [{'role': 'user', 'content': 'work'}],
            'tools': tools, '_tool_search_mode': 'auto',
            '_frontend_selected_tool_names': ['tool_0', 'tool_1'],
            '_tool_namespace_by_name': {
                f'tool_{index}': ('project' if index < 12 else 'automation')
                for index in range(24)
            },
        })
        assert out['tools'][0] == {'type': 'tool_search'}
        direct = [tool for tool in out['tools']
                  if tool.get('type') == 'function']
        assert {tool['name'] for tool in direct} == {'tool_0', 'tool_1'}
        assert all('defer_loading' not in tool for tool in direct)
        namespaces = [tool for tool in out['tools']
                      if tool.get('type') == 'namespace']
        assert namespaces and all(len(ns['tools']) <= 10 for ns in namespaces)
        deferred = [tool for ns in namespaces for tool in ns['tools']]
        assert len(deferred) == 22
        assert all(tool['defer_loading'] is True for tool in deferred)
        assert {tool['name'] for tool in direct + deferred} == {
            f'tool_{index}' for index in range(24)}

    def test_tool_choice_is_hoisted_out_of_tool_search(self):
        tools = [{
            'type': 'function',
            'function': {'name': f'tool_{index}',
                         'parameters': {'type': 'object'}},
        } for index in range(20)]
        out, _ = openai_body_to_responses({
            'model': 'gpt-5.6-sol',
            '_responses_feature_profile': 'openai',
            'messages': [{'role': 'user', 'content': 'work'}],
            'tools': tools, '_tool_search_mode': 'auto',
            'tool_choice': {
                'type': 'function', 'function': {'name': 'tool_19'}},
        })
        direct_names = {tool.get('name') for tool in out['tools']
                        if tool.get('type') == 'function'}
        assert 'tool_19' in direct_names
        assert out['tool_choice'] == {
            'type': 'function', 'name': 'tool_19'}

    def test_multi_agent_beta_omits_explicit_compaction(self):
        out, _ = openai_body_to_responses({
            'model': 'gpt-5.6-sol', '_working_set_tokens': 128000,
            '_responses_feature_profile': 'openai',
            '_multi_agent_mode': 'read_only',
            '_multi_agent_max_concurrent_subagents': 4,
            'messages': [{'role': 'user', 'content': 'compare modules'}],
        })
        assert out['multi_agent'] == {
            'enabled': True, 'max_concurrent_subagents': 4}
        assert 'context_management' not in out
        wire = json.dumps(out['input'], ensure_ascii=False)
        assert 'Native subagents are read-only analysts' in wire


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
                        'input_tokens_details': {
                            'cached_tokens': 64, 'cache_write_tokens': 16},
                        'output_tokens_details': {'reasoning_tokens': 3}}),
        ])
        msg, finish, usage = acc.finalize()
        assert msg['content'] == '你好，世界'
        assert finish == 'stop'
        assert usage['prompt_tokens'] == 100
        assert usage['completion_tokens'] == 7
        assert usage['total_tokens'] == 107
        assert usage['prompt_tokens_details'] == {
            'cached_tokens': 64, 'cache_write_tokens': 16}
        assert usage['cache_write_tokens'] == 16
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

    def test_reasoning_summary_parts_get_paragraph_separators(self):
        """OpenAI summary parts are markdown headlines; without a separator
        at part boundaries adjacent '**…**' headlines fuse ('**A****B**')."""
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            {'type': 'response.output_item.added',
             'item': {'type': 'reasoning', 'id': 'rs_1'}},
            {'type': 'response.reasoning_summary_part.added'},
            _reason_summary('**Planning the endpoint**'),
            {'type': 'response.reasoning_summary_part.done'},
            {'type': 'response.reasoning_summary_part.added'},
            _reason_summary('**Designing the query**'),
            {'type': 'response.reasoning_summary_part.done'},
            _text('done'), _completed()])
        msg, _f, _u = acc.finalize()
        assert msg['reasoning_content'] == (
            '**Planning the endpoint**\n\n**Designing the query**')

    def test_reasoning_part_boundary_never_leading_separator(self):
        """The FIRST part boundary is a no-op — no leading '\n\n'."""
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            {'type': 'response.reasoning_summary_part.added'},
            _reason_summary('solo'),
            _text('done'), _completed()])
        msg, _f, _u = acc.finalize()
        assert msg['reasoning_content'] == 'solo'

    def test_second_reasoning_item_also_separated(self):
        """A response with two reasoning blocks gets the same boundary."""
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            {'type': 'response.output_item.added',
             'item': {'type': 'reasoning', 'id': 'rs_1'}},
            {'type': 'response.reasoning_summary_part.added'},
            _reason_summary('block one'),
            {'type': 'response.output_item.done',
             'item': {'type': 'reasoning', 'id': 'rs_1'}},
            {'type': 'response.output_item.added',
             'item': {'type': 'reasoning', 'id': 'rs_2'}},
            {'type': 'response.reasoning_summary_part.added'},
            _reason_summary('block two'),
            _text('done'), _completed()])
        msg, _f, _u = acc.finalize()
        assert msg['reasoning_content'] == 'block one\n\nblock two'

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

    def test_unknown_explicit_item_id_never_borrows_current_call(self):
        acc = _acc(ResponsesSSETranslator())
        with pytest.raises(RetryableAPIError, match='unknown.*item_id'):
            _feed(acc, [
                _fn_added('call_1', 'read_files', item_id='fc_1'),
                _fn_args('{"wrong":true}', item_id='fc_missing'),
            ])

    def test_output_index_routes_parallel_arguments_without_item_ids(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            {'type': 'response.output_item.added', 'output_index': 0,
             'item': {'type': 'function_call', 'call_id': 'call_1',
                      'name': 'read_files'}},
            {'type': 'response.output_item.added', 'output_index': 1,
             'item': {'type': 'function_call', 'call_id': 'call_2',
                      'name': 'read_files'}},
            {'type': 'response.function_call_arguments.delta',
             'output_index': 0, 'delta': '{"path":"a"}'},
            {'type': 'response.function_call_arguments.delta',
             'output_index': 1, 'delta': '{"path":"b"}'},
            _completed(),
        ])
        msg, _finish, _usage = acc.finalize()
        assert [call['function']['arguments']
                for call in msg['tool_calls']] == [
            '{"path":"a"}', '{"path":"b"}']

    def test_arguments_done_fills_only_missing_stream_suffix(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _fn_added('call_1', 'read_files', item_id='fc_1'),
            _fn_args('{"path":', item_id='fc_1'),
            {'type': 'response.function_call_arguments.done',
             'item_id': 'fc_1', 'arguments': '{"path":"a.py"}'},
            _completed(),
        ])
        msg, _finish, _usage = acc.finalize()
        assert msg['tool_calls'][0]['function']['arguments'] == (
            '{"path":"a.py"}')

    def test_arguments_done_disagreement_fails_closed(self):
        acc = _acc(ResponsesSSETranslator())
        with pytest.raises(RetryableAPIError, match='disagree'):
            _feed(acc, [
                _fn_added('call_1', 'read_files', item_id='fc_1'),
                _fn_args('{"path":"wrong"}', item_id='fc_1'),
                {'type': 'response.function_call_arguments.done',
                 'item_id': 'fc_1', 'arguments': '{"path":"right"}'},
            ])

    def test_terminal_output_reconciles_missing_argument_suffix(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _fn_added('call_1', 'read_files', item_id='fc_1'),
            _fn_args('{"path":', item_id='fc_1'),
            {'type': 'response.completed', 'response': {
                'status': 'completed',
                'output': [{
                    'type': 'function_call', 'id': 'fc_1',
                    'call_id': 'call_1', 'name': 'read_files',
                    'arguments': '{"path":"a.py"}',
                }],
                'usage': {},
            }},
        ])
        msg, _finish, _usage = acc.finalize()
        assert msg['tool_calls'][0]['function']['arguments'] == (
            '{"path":"a.py"}')

    def test_initial_function_arguments_are_not_discarded(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            {'type': 'response.output_item.added',
             'item': {'type': 'function_call', 'id': 'fc_1',
                      'call_id': 'call_1', 'name': 'read_files',
                      'arguments': '{"path":"a.py"}'}},
            _completed(),
        ])
        msg, _finish, _usage = acc.finalize()
        assert msg['tool_calls'][0]['function']['arguments'] == (
            '{"path":"a.py"}')

    def test_replayed_function_start_same_position_does_not_mint_call(self):
        acc = _acc(ResponsesSSETranslator())
        start = _fn_added('call_1', 'read_files', item_id='fc_1')
        _feed(acc, [start, start, _fn_args('{}', item_id='fc_1'), _completed()])
        msg, finish, _usage = acc.finalize()
        assert finish == 'tool_calls'
        assert [(call['id'], call['function']['name'])
                for call in msg['tool_calls']] == [('call_1', 'read_files')]

    def test_equal_calls_at_distinct_response_positions_remain_distinct(self):
        acc = _acc(ResponsesSSETranslator())
        _feed(acc, [
            _fn_added('call_1', 'read_files', item_id='fc_1'),
            _fn_added('call_2', 'read_files', item_id='fc_2'),
            _fn_args('{}', item_id='fc_1'),
            _fn_args('{}', item_id='fc_2'),
            _completed(),
        ])
        msg, _finish, _usage = acc.finalize()
        assert [call['id'] for call in msg['tool_calls']] == [
            'call_1', 'call_2']

    def test_recycled_item_id_at_distinct_output_positions_is_not_collapsed(self):
        acc = _acc(ResponsesSSETranslator())
        starts = [
            {
                'type': 'response.output_item.added', 'output_index': index,
                'item': {
                    'type': 'function_call', 'id': 'recycled-item',
                    'call_id': 'recycled-call', 'name': 'read_files',
                },
            }
            for index in (0, 1)
        ]
        _feed(acc, [
            *starts,
            {'type': 'response.function_call_arguments.delta',
             'item_id': 'recycled-item', 'output_index': 0,
             'delta': '{"path":"a"}'},
            {'type': 'response.function_call_arguments.delta',
             'item_id': 'recycled-item', 'output_index': 1,
             'delta': '{"path":"b"}'},
            {'type': 'response.completed', 'response': {
                'status': 'completed',
                'output': [
                    {**starts[0]['item'], 'arguments': '{"path":"a"}'},
                    {**starts[1]['item'], 'arguments': '{"path":"b"}'},
                ],
                'usage': {},
            }},
        ])

        msg, finish, _usage = acc.finalize()
        assert finish == 'tool_calls'
        assert [call['function']['arguments']
                for call in msg['tool_calls']] == [
            '{"path":"a"}', '{"path":"b"}',
        ]

    def test_ambiguous_recycled_item_id_cannot_route_without_position(self):
        acc = _acc(ResponsesSSETranslator())
        with pytest.raises(RetryableAPIError, match='ambiguous.*output_index'):
            _feed(acc, [
                {'type': 'response.output_item.added', 'output_index': 0,
                 'item': {'type': 'function_call', 'id': 'recycled',
                          'call_id': 'call', 'name': 'read_files'}},
                {'type': 'response.output_item.added', 'output_index': 1,
                 'item': {'type': 'function_call', 'id': 'recycled',
                          'call_id': 'call', 'name': 'read_files'}},
                {'type': 'response.function_call_arguments.delta',
                 'item_id': 'recycled', 'delta': '{}'},
            ])

    def test_reused_response_position_with_changed_identity_fails(self):
        acc = _acc(ResponsesSSETranslator())
        with pytest.raises(RetryableAPIError, match='response position'):
            _feed(acc, [
                _fn_added('call_1', 'read_files', item_id='fc_1'),
                _fn_added('call_2', 'run_command', item_id='fc_1'),
            ])

    def test_stream_preserves_invalid_caller_for_common_rejection(self):
        translator = ResponsesSSETranslator()
        chunks = translator.translate(json.dumps({
            'type': 'response.output_item.added',
            'output_index': 0,
            'item': {
                'type': 'function_call', 'id': 'fc_1',
                'call_id': 'call_1', 'name': 'run_command',
                'caller': 'not-an-object',
                'agent': {'agent_name': '/subagent'},
            },
        }))
        call = chunks[0]['choices'][0]['delta']['tool_calls'][0]
        assert call['caller'] == 'not-an-object'

    def test_null_caller_does_not_mask_valid_agent_attribution(self):
        chunks = ResponsesSSETranslator().translate(json.dumps({
            'type': 'response.output_item.added',
            'output_index': 0,
            'item': {
                'type': 'function_call', 'id': 'fc_1',
                'call_id': 'call_1', 'name': 'read_files',
                'caller': None,
                'agent': {'agent_name': '/subagent'},
            },
        }))
        call = chunks[0]['choices'][0]['delta']['tool_calls'][0]
        assert call['caller'] == {
            'type': 'multi_agent', 'agent_name': '/subagent'}

    @pytest.mark.parametrize('event', [
        [],
        {'type': []},
        {'type': 'response.output_text.delta', 'delta': {}},
        {'type': 'response.output_text.delta', 'delta': 'x',
         'output_index': {}},
        {'type': 'response.output_text.delta', 'delta': 'x',
         'agent': {'agent_name': []}},
        {'type': 'response.output_item.added', 'item': []},
        {'type': 'response.output_item.added', 'item': {
            'type': 'message', 'agent': {}}},
        {'type': 'response.output_item.added', 'item': {
            'type': 'function_call', 'id': [], 'call_id': 'c', 'name': 'f'}},
        {'type': 'response.output_item.added', 'item': {
            'type': 'function_call', 'id': 'x', 'call_id': 'c', 'name': []}},
        {'type': 'response.function_call_arguments.delta',
         'item_id': {}, 'delta': '{}'},
        {'type': 'response.completed', 'response': []},
        {'type': 'response.completed', 'response': {'output': {}}},
        {'type': 'response.incomplete', 'response': {
            'output': [], 'incomplete_details': []}},
    ])
    def test_malformed_stream_shapes_return_typed_protocol_error(self, event):
        chunks = ResponsesSSETranslator().translate(json.dumps(event))
        assert chunks[0]['error']['type'] == 'server_error'
        assert chunks[0]['error']['http_code'] == '500'

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

    def test_opaque_reasoning_and_compaction_items_survive_stream(self):
        reasoning = {'type': 'reasoning', 'id': 'rs_1',
                     'encrypted_content': 'opaque-reasoning'}
        compact = {'type': 'compaction', 'id': 'cmp_1',
                   'encrypted_content': 'opaque-compaction'}
        acc = _acc(ResponsesSSETranslator(model='gpt-5.6-sol'),
                   model='gpt-5.6-sol')
        _feed(acc, [
            {'type': 'response.output_item.done', 'item': reasoning},
            _text('done'),
            {'type': 'response.completed', 'response': {
                'status': 'completed', 'output': [reasoning, compact],
                'usage': {'input_tokens': 10, 'output_tokens': 2}}},
        ])
        msg, _finish, _usage = acc.finalize()
        assert msg['_responses_items'] == [reasoning, compact]

    def test_terminal_opaque_items_preserve_equal_occurrences_and_recycled_ids(self):
        idless = {'type': 'reasoning', 'encrypted_content': 'same'}
        recycled_a = {'type': 'compaction', 'id': 'recycled',
                      'encrypted_content': 'first'}
        recycled_b = {'type': 'compaction', 'id': 'recycled',
                      'encrypted_content': 'second'}
        expected = [idless, idless, recycled_a, recycled_b]
        acc = _acc(ResponsesSSETranslator(model='gpt-5.6-sol'),
                   model='gpt-5.6-sol')

        _feed(acc, [{
            'type': 'response.completed',
            'response': {
                'status': 'completed', 'output': expected,
                'usage': {'input_tokens': 2, 'output_tokens': 1},
            },
        }])

        msg, _finish, _usage = acc.finalize()
        assert msg['_responses_items'] == expected

    def test_provisional_retransmit_replaces_only_the_same_output_position(self):
        first = {'type': 'reasoning', 'id': 'same-id',
                 'encrypted_content': 'first'}
        replacement = {'type': 'reasoning', 'id': 'same-id',
                       'encrypted_content': 'replacement'}
        other_position = {'type': 'reasoning', 'id': 'same-id',
                          'encrypted_content': 'other position'}
        translator = ResponsesSSETranslator(model='gpt-5.6-sol')

        translator.translate(json.dumps({
            'type': 'response.output_item.done',
            'output_index': 3, 'item': first,
        }))
        translator.translate(json.dumps({
            'type': 'response.output_item.done',
            'output_index': 3, 'item': replacement,
        }))
        translator.translate(json.dumps({
            'type': 'response.output_item.done',
            'output_index': 4, 'item': other_position,
        }))

        assert translator.response_items == [replacement, other_position]

    def test_tool_search_and_multi_agent_items_survive_stream(self):
        searched = {'type': 'tool_search_call', 'id': 'ts_1',
                    'status': 'completed'}
        delegated = {'type': 'multi_agent_call', 'id': 'ma_1',
                     'status': 'completed'}
        acc = _acc(ResponsesSSETranslator(model='gpt-5.6-sol'),
                   model='gpt-5.6-sol')
        _feed(acc, [
            {'type': 'response.output_item.done', 'item': searched},
            {'type': 'response.output_item.done', 'item': delegated},
            _text('done'),
            {'type': 'response.completed', 'response': {
                'id': 'resp_1', 'status': 'completed',
                'output': [searched, delegated],
                'usage': {'input_tokens': 5, 'output_tokens': 1}}},
        ])
        msg, _finish, _usage = acc.finalize()
        assert msg['_responses_items'] == [searched, delegated]


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

    def test_multi_part_summary_joined_with_blank_line(self):
        out = responses_response_to_openai({
            'status': 'completed',
            'output': [{'type': 'reasoning', 'summary': [
                {'type': 'summary_text', 'text': '**Part one**'},
                {'type': 'summary_text', 'text': '**Part two**'}]}]})
        assert out['choices'][0]['message']['reasoning_content'] == (
            '**Part one**\n\n**Part two**')

    def test_failed_status_yields_error_envelope(self):
        out = responses_response_to_openai({
            'status': 'failed',
            'error': {'code': 'rate_limit_exceeded', 'message': 'slow down'}})
        assert 'error' in out
        assert 'slow down' in out['error']['message']

    @pytest.mark.parametrize('data', [
        {'status': []},
        {'status': 'queued'},
        {'status': 'completed', 'output': {}},
        {'status': 'completed', 'output': ['bad-item']},
        {'status': 'completed', 'output': [
            {'type': 'message', 'content': {}}]},
        {'status': 'completed', 'output': [
            {'type': 'message', 'content': [
                {'type': 'output_text', 'text': {}}]}]},
        {'status': 'completed', 'output': [
            {'type': 'reasoning', 'summary': {}}]},
        {'status': 'completed', 'output': [
            {'type': 'message', 'agent': {}, 'content': []}]},
        {'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': [],
             'name': 'read_files', 'arguments': '{}'}]},
        {'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': 'call',
             'name': [], 'arguments': '{}'}]},
        {'status': 'completed', 'output': [
            {'type': 'function_call', 'call_id': 'call',
             'name': 'read_files', 'arguments': {}}]},
        {'status': 'incomplete', 'output': [], 'incomplete_details': []},
        {'status': 'incomplete', 'output': [], 'incomplete_details': {}},
        {'status': 'incomplete', 'output': [],
         'incomplete_details': {'reason': 'content_filter'}},
    ])
    def test_malformed_or_nonterminal_response_fails_closed(self, data):
        out = responses_response_to_openai(data)
        assert out['error']['type'] == 'invalid_response'

    def test_malformed_failed_error_is_still_a_typed_failure(self):
        out = responses_response_to_openai({
            'status': 'failed', 'error': ['malformed'],
        })
        assert out['error'] == {
            'message': 'response_failed: response failed',
            'type': 'response_failed',
        }

    def test_invalid_caller_is_preserved_for_common_ingress_rejection(self):
        out = responses_response_to_openai({
            'status': 'completed',
            'output': [{
                'type': 'function_call',
                'call_id': 'call_1',
                'name': 'run_command',
                'arguments': '{}',
                'caller': 'not-an-object',
                'agent': {'agent_name': '/subagent'},
            }],
        })
        call = out['choices'][0]['message']['tool_calls'][0]
        assert call['caller'] == 'not-an-object'

    def test_malformed_usage_counts_are_bounded_to_numbers(self):
        out = responses_response_to_openai({
            'status': 'completed',
            'output': [],
            'usage': {
                'input_tokens': {'bad': 'count'},
                'output_tokens': '7',
                'input_tokens_details': {
                    'cached_tokens': [], 'cache_write_tokens': '3'},
                'output_tokens_details': {'reasoning_tokens': '2'},
            },
        })
        assert out['usage'] == {
            'prompt_tokens': 0,
            'completion_tokens': 7,
            'total_tokens': 7,
            'cache_write_tokens': 3,
            'prompt_tokens_details': {
                'cached_tokens': 0, 'cache_write_tokens': 3},
            'completion_tokens_details': {'reasoning_tokens': 2},
        }

    def test_incomplete_max_tokens_finish_length(self):
        out = responses_response_to_openai({
            'status': 'incomplete',
            'incomplete_details': {'reason': 'max_output_tokens'},
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': 'cut'}]}]})
        assert out['choices'][0]['finish_reason'] == 'length'

    def test_nonstream_opaque_items_are_attached_for_next_turn(self):
        reasoning = {'type': 'reasoning', 'id': 'rs_1',
                     'encrypted_content': 'abc'}
        compact = {'type': 'compaction', 'id': 'cmp_1',
                   'encrypted_content': 'xyz'}
        out = responses_response_to_openai({
            'status': 'completed', 'output': [reasoning, compact]})
        assert out['choices'][0]['message']['_responses_items'] == [
            reasoning, compact]

    def test_compaction_replay_prunes_old_dynamic_history(self):
        compact = {'type': 'compaction', 'id': 'cmp_1',
                   'encrypted_content': 'opaque'}
        body = {'model': 'gpt-5.6-sol', '_conv_id': 'c1',
                '_responses_feature_profile': 'openai',
                'messages': [
                    {'role': 'system', 'content': 'stable'},
                    {'role': 'user', 'content': 'old user turn'},
                    {'role': 'assistant', 'content': 'old answer'},
                    {'role': 'assistant', 'content': 'after compact',
                     '_responses_items': [compact]},
                    {'role': 'user', 'content': 'new question'},
                ]}
        converted, _ = openai_body_to_responses(body, profile='default')
        assert converted['input'][0]['role'] == 'developer'
        assert converted['input'][1] == compact
        wire_text = json.dumps(converted['input'], ensure_ascii=False)
        assert 'old user turn' not in wire_text
        assert 'old answer' not in wire_text
        assert 'after compact' in wire_text
        assert 'new question' in wire_text


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

    def test_websocket_and_multi_agent_transport_metadata(self):
        from lib.llm._sse_core import prepare_request
        plan = prepare_request({
            'model': 'gpt-5.6-sol', '_task_id': 'task-ws',
            '_responses_feature_profile': 'openai',
            '_responses_transport': 'websocket',
            '_multi_agent_mode': 'read_only',
            'messages': [{'role': 'user', 'content': 'compare'}],
        }, api_key='k', base_url='https://api.openai.com/v1',
           api_protocol='responses')
        assert plan.responses_transport == 'websocket'
        assert plan.responses_state_key == 'task-ws'
        assert plan.responses_profile == 'default'
        assert 'responses_multi_agent=v1' in plan.hdrs['OpenAI-Beta']

    def test_codex_oauth_slot_uses_codex_profile(self, monkeypatch):
        """oauth='codex' + protocol='responses' → codex profile (instructions,
        include) — token resolution mocked out."""
        monkeypatch.setattr(
            'lib.oauth.outbound.resolve_oauth_request',
            lambda oauth, body, extra_headers, **_kwargs: ('TOK', {}, body))
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
