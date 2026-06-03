"""tests/test_anthropic_outbound.py — outbound Anthropic protocol adapter.

Covers ``lib.llm.anthropic_outbound``: OpenAI→Anthropic body translation,
non-streaming response parsing, and the SSE translator that emits OpenAI
chunks for ``SSEAccumulator``.
"""

import json
import unittest

from lib.llm.anthropic_outbound import (
    AnthropicSSETranslator,
    anthropic_headers,
    anthropic_messages_url,
    anthropic_response_to_openai,
    openai_body_to_anthropic,
)


class UrlAndHeadersTest(unittest.TestCase):
    def test_url_appends_messages(self):
        self.assertEqual(
            anthropic_messages_url('https://api.openai.com/v1/anthropic'),
            'https://api.openai.com/v1/anthropic/v1/messages')

    def test_url_idempotent_when_already_messages(self):
        self.assertEqual(anthropic_messages_url('https://x/v1/messages'),
                         'https://x/v1/messages')

    def test_url_direct_anthropic_v1_base(self):
        # Direct api.anthropic.com base already ends at the /v1 version
        # segment — must NOT become /v1/v1/messages.
        self.assertEqual(anthropic_messages_url('https://api.anthropic.com/v1'),
                         'https://api.anthropic.com/v1/messages')

    def test_headers_carry_both_auth_styles(self):
        h = anthropic_headers('app-id', {'M-X': '1'})
        self.assertEqual(h['x-api-key'], 'app-id')
        self.assertEqual(h['Authorization'], 'Bearer app-id')
        self.assertEqual(h['anthropic-version'], '2023-06-01')
        self.assertEqual(h['M-X'], '1')


class BodyTranslationTest(unittest.TestCase):
    def test_system_hoisted_to_top_level(self):
        body = {'model': 'm', 'max_tokens': 8,
                'messages': [{'role': 'system', 'content': 'sys'},
                             {'role': 'user', 'content': 'hi'}]}
        a = openai_body_to_anthropic(body)
        self.assertEqual(a['system'], 'sys')
        self.assertEqual(len(a['messages']), 1)
        self.assertEqual(a['messages'][0]['role'], 'user')

    def test_tool_calls_become_tool_use_and_tool_results_merge(self):
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 't1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{"a":1}'}}]},
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'r1'},
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'r2'},
        ]}
        a = openai_body_to_anthropic(body)
        # assistant tool_use
        self.assertEqual(a['messages'][1]['content'][0]['type'], 'tool_use')
        self.assertEqual(a['messages'][1]['content'][0]['input'], {'a': 1})
        # two tool results merged into ONE user turn
        tr_turn = a['messages'][2]
        self.assertEqual(tr_turn['role'], 'user')
        self.assertEqual(len(tr_turn['content']), 2)
        self.assertTrue(all(b['type'] == 'tool_result' for b in tr_turn['content']))

    def test_tools_schema_converted(self):
        body = {'model': 'm', 'max_tokens': 8,
                'messages': [{'role': 'user', 'content': 'hi'}],
                'tools': [{'type': 'function', 'function': {
                    'name': 'f', 'description': 'd',
                    'parameters': {'type': 'object', 'properties': {}}}}]}
        a = openai_body_to_anthropic(body)
        self.assertEqual(a['tools'][0]['name'], 'f')
        self.assertIn('input_schema', a['tools'][0])

    def test_cache_control_preserved_on_system_blocks(self):
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'system', 'content': [
                {'type': 'text', 'text': 'sys',
                 'cache_control': {'type': 'ephemeral'}}]},
            {'role': 'user', 'content': 'hi'}]}
        a = openai_body_to_anthropic(body)
        self.assertIsInstance(a['system'], list)
        self.assertEqual(a['system'][0]['cache_control'], {'type': 'ephemeral'})


class ResponseTranslationTest(unittest.TestCase):
    def test_text_and_tool_use_and_usage(self):
        resp = {'id': 'msg_1', 'model': 'claude-opus-4-7', 'stop_reason': 'tool_use',
                'content': [{'type': 'text', 'text': 'hello'},
                            {'type': 'tool_use', 'id': 'tu1', 'name': 'f',
                             'input': {'a': 1}}],
                'usage': {'input_tokens': 10, 'output_tokens': 5,
                          'cache_read_input_tokens': 2}}
        o = anthropic_response_to_openai(resp)
        msg = o['choices'][0]['message']
        self.assertEqual(o['choices'][0]['finish_reason'], 'tool_calls')
        self.assertEqual(msg['content'], 'hello')
        self.assertEqual(msg['tool_calls'][0]['function']['name'], 'f')
        self.assertEqual(json.loads(msg['tool_calls'][0]['function']['arguments']),
                         {'a': 1})
        self.assertEqual(o['usage']['completion_tokens'], 5)
        self.assertEqual(o['usage']['prompt_tokens'], 12)  # input + cache_read

    def test_end_turn_maps_to_stop(self):
        o = anthropic_response_to_openai(
            {'stop_reason': 'end_turn', 'content': [{'type': 'text', 'text': 'x'}]})
        self.assertEqual(o['choices'][0]['finish_reason'], 'stop')


class SSETranslationTest(unittest.TestCase):
    def _run(self, events):
        t = AnthropicSSETranslator(model='x')
        content, args, fr, done = '', '', None, False
        for ev in events:
            for chunk in t.translate(json.dumps(ev)):
                if chunk == '[DONE]':
                    done = True
                    continue
                ch = chunk['choices'][0]
                d = ch.get('delta', {})
                content += d.get('content', '')
                for tc in d.get('tool_calls', []):
                    args += tc.get('function', {}).get('arguments', '')
                if ch.get('finish_reason'):
                    fr = ch['finish_reason']
        return content, args, fr, done

    def test_text_tool_and_stop(self):
        events = [
            {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'text_delta', 'text': 'Hel'}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'text_delta', 'text': 'lo'}},
            {'type': 'content_block_start', 'index': 1,
             'content_block': {'type': 'tool_use', 'id': 'tu1', 'name': 'f'}},
            {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': '{"a":'}},
            {'type': 'content_block_delta', 'index': 1,
             'delta': {'type': 'input_json_delta', 'partial_json': '1}'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}},
            {'type': 'message_stop'},
        ]
        content, args, fr, done = self._run(events)
        self.assertEqual(content, 'Hello')
        self.assertEqual(args, '{"a":1}')
        self.assertEqual(fr, 'tool_calls')
        self.assertTrue(done)

    def test_error_event_surfaces(self):
        t = AnthropicSSETranslator()
        out = t.translate(json.dumps({'type': 'error',
                                      'error': {'message': 'boom'}}))
        self.assertEqual(out[0]['error']['message'], 'boom')


if __name__ == '__main__':
    unittest.main()
