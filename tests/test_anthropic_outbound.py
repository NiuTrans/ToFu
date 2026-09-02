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


class PhantomEmptyBlockTest(unittest.TestCase):
    """The Anthropic boundary must NEVER emit an empty/whitespace text block.

    Strict Anthropic-shape validators hard-400 the whole request on one
    (Moonshot: "text content is empty" — the 2026-07-31 kimi-k3 incident
    class on the OpenAI wire; same rejection shape on the Anthropic side).
    Covers the two ``not blocks`` fallbacks (ghost assistant / ghost user)
    and the emission guard in ``_convert_content_blocks`` — the boundary
    stays self-consistent even when a caller bypasses build_body's healers.
    """

    def test_ghost_assistant_gets_placeholder_not_empty_text(self):
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'user', 'content': 'q'},
            # ghost: no signed thinking, empty content, no tool_calls
            {'role': 'assistant', 'content': ''},
            {'role': 'user', 'content': 'next'},
        ]}
        a = openai_body_to_anthropic(body)
        asst = a['messages'][1]
        self.assertEqual(asst['role'], 'assistant')
        self.assertTrue(asst['content'])
        for b in asst['content']:
            if b['type'] == 'text':
                self.assertTrue(b['text'].strip(),
                                f'phantom empty text block: {b}')

    def test_ghost_user_gets_placeholder_not_empty_text(self):
        body = {'model': 'm', 'max_tokens': 8,
                'messages': [{'role': 'user', 'content': ''}]}
        a = openai_body_to_anthropic(body)
        u = a['messages'][0]
        self.assertTrue(u['content'])
        self.assertTrue(all(b['text'].strip() for b in u['content']
                            if b['type'] == 'text'))

    def test_empty_text_blocks_in_list_skipped_at_emission(self):
        """THE VU shape: a wrap seam produced [{text:''}, {text:'reminder'}]
        upstream of a caller that bypasses build_body — the boundary itself
        must not re-emit the phantom."""
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'user', 'content': [
                {'type': 'text', 'text': ''},
                {'type': 'text', 'text': '<system-reminder>\nx\n</system-reminder>'}]}]}
        a = openai_body_to_anthropic(body)
        texts = [b['text'] for b in a['messages'][0]['content']
                 if b['type'] == 'text']
        self.assertEqual(texts, ['<system-reminder>\nx\n</system-reminder>'])

    def test_whitespace_only_blocks_and_strings_skipped(self):
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'system', 'content': [
                {'type': 'text', 'text': 'sys'},
                {'type': 'text', 'text': '   '}]},
            {'role': 'user', 'content': [
                {'type': 'text', 'text': '\n '},
                {'type': 'text', 'text': 'hi'}]}]}
        a = openai_body_to_anthropic(body)
        self.assertEqual(a['system'], 'sys')
        self.assertEqual([b['text'] for b in a['messages'][0]['content']],
                         ['hi'])

    def test_assistant_tool_calls_only_no_phantom_text(self):
        """content='' + tool_calls → only tool_use blocks (existing contract)."""
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 't1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 't1', 'content': 'r'}]}
        a = openai_body_to_anthropic(body)
        self.assertEqual([b['type'] for b in a['messages'][1]['content']],
                         ['tool_use'])

    def test_tool_phantom_text_block_skipped_but_result_kept(self):
        """A tool result carrying [{text:''}, image] keeps the image, drops
        the phantom — multimodal tool results are the live producer shape."""
        body = {'model': 'm', 'max_tokens': 8, 'messages': [
            {'role': 'user', 'content': 'go'},
            {'role': 'assistant', 'content': '',
             'tool_calls': [{'id': 't1', 'type': 'function',
                             'function': {'name': 'f', 'arguments': '{}'}}]},
            {'role': 'tool', 'tool_call_id': 't1', 'content': [
                {'type': 'text', 'text': ''},
                {'type': 'image_url',
                 'image_url': {'url': 'https://x/y.png'}}]}]}
        a = openai_body_to_anthropic(body)
        tr = a['messages'][2]['content'][0]
        self.assertEqual(tr['type'], 'tool_result')
        kinds = [b['type'] for b in tr['content']]
        self.assertNotIn('text', kinds)
        self.assertIn('image', kinds)


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

    def test_missing_or_unknown_stop_reason_is_not_laundered_to_stop(self):
        missing = anthropic_response_to_openai({
            'content': [{'type': 'text', 'text': 'partial'}],
        })
        unknown = anthropic_response_to_openai({
            'stop_reason': 'future_limit',
            'content': [{'type': 'text', 'text': 'partial'}],
        })

        self.assertIsNone(missing['choices'][0]['finish_reason'])
        self.assertEqual(unknown['choices'][0]['finish_reason'], 'future_limit')

    def test_hosted_tool_blocks_are_kept_for_protocol_replay(self):
        blocks = [
            {'type': 'server_tool_use', 'id': 'srv_1',
             'name': 'tool_search_tool_bm25',
             'input': {'query': 'document editor'}},
            {'type': 'tool_search_tool_result', 'tool_use_id': 'srv_1',
             'content': [{'type': 'tool_reference',
                          'tool_name': 'update_doc'}]},
            {'type': 'tool_use', 'id': 'tu_1', 'name': 'update_doc',
             'input': {'id': '42'}},
        ]
        o = anthropic_response_to_openai({
            'stop_reason': 'tool_use', 'content': blocks})
        msg = o['choices'][0]['message']
        self.assertEqual(msg['_anthropic_content_blocks'], blocks)
        self.assertEqual(msg['tool_calls'][0]['function']['name'], 'update_doc')

        wire = openai_body_to_anthropic({
            'model': 'claude-sonnet-4-5', 'max_tokens': 64,
            'messages': [
                {'role': 'user', 'content': 'edit it'},
                msg,
                {'role': 'tool', 'tool_call_id': 'tu_1', 'content': 'ok'},
            ],
        })
        self.assertEqual(wire['messages'][1]['content'], blocks)


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

    def test_unknown_stop_reason_remains_unknown(self):
        _content, _args, finish, _done = self._run([{
            'type': 'message_delta',
            'delta': {'stop_reason': 'future_limit'},
        }])

        self.assertEqual(finish, 'future_limit')

    def test_server_tool_use_is_not_dispatched_and_full_blocks_are_replayable(self):
        t = AnthropicSSETranslator(model='claude-sonnet-4-5')
        events = [
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'server_tool_use', 'id': 'srv_1',
                               'name': 'tool_search_tool_bm25', 'input': {}}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'input_json_delta',
                       'partial_json': '{"query":"xuecheng"}'}},
            {'type': 'content_block_start', 'index': 1,
             'content_block': {
                 'type': 'tool_search_tool_result', 'tool_use_id': 'srv_1',
                 'content': [{'type': 'tool_reference',
                              'tool_name': 'mcp__xuecheng__update_doc'}]}},
            {'type': 'content_block_start', 'index': 2,
             'content_block': {'type': 'tool_use', 'id': 'tu_1',
                               'name': 'mcp__xuecheng__update_doc', 'input': {}}},
            {'type': 'content_block_delta', 'index': 2,
             'delta': {'type': 'input_json_delta',
                       'partial_json': '{"doc_id":"42"}'}},
        ]
        projected = []
        for event in events:
            projected.extend(t.translate(json.dumps(event)))

        # Only the actual client tool_use is projected into the application
        # executor; the hosted BM25 server_tool_use stays protocol-private.
        projected_calls = [tc for chunk in projected
                           for choice in chunk.get('choices', [])
                           for tc in choice.get('delta', {}).get('tool_calls', [])]
        self.assertEqual(len(projected_calls), 2)  # start + args of index 2
        self.assertTrue(all(tc.get('index') == 2 for tc in projected_calls))
        self.assertEqual(t.anthropic_content_blocks, [
            {'type': 'server_tool_use', 'id': 'srv_1',
             'name': 'tool_search_tool_bm25',
             'input': {'query': 'xuecheng'}},
            {'type': 'tool_search_tool_result', 'tool_use_id': 'srv_1',
             'content': [{'type': 'tool_reference',
                          'tool_name': 'mcp__xuecheng__update_doc'}]},
            {'type': 'tool_use', 'id': 'tu_1',
             'name': 'mcp__xuecheng__update_doc',
             'input': {'doc_id': '42'}},
        ])

    def _last_usage(self, events):
        """Return the final usage dict an accumulator would settle on
        (last chunk to carry a `usage` key wins — mirrors SSEAccumulator)."""
        t = AnthropicSSETranslator(model='x')
        usage = None
        for ev in events:
            for chunk in t.translate(json.dumps(ev)):
                if chunk == '[DONE]':
                    continue
                if chunk.get('usage'):
                    usage = chunk['usage']
        return usage

    def test_cache_read_survives_message_delta(self):
        # ★ Regression: Anthropic reports cache tokens on message_start and
        #   only output_tokens on message_delta. The downstream accumulator
        #   overwrites usage per chunk, so a message_delta usage carrying only
        #   output must NOT clobber the cache_read captured at message_start.
        events = [
            {'type': 'message_start', 'message': {'usage': {
                'input_tokens': 120,
                'cache_creation_input_tokens': 8000,
                'cache_read_input_tokens': 40000,
                'output_tokens': 1}}},
            {'type': 'content_block_start', 'index': 0,
             'content_block': {'type': 'text', 'text': ''}},
            {'type': 'content_block_delta', 'index': 0,
             'delta': {'type': 'text_delta', 'text': 'hi'}},
            {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'},
             'usage': {'output_tokens': 250}},
            {'type': 'message_stop'},
        ]
        u = self._last_usage(events)
        self.assertIsNotNone(u)
        self.assertEqual(u['cache_read_input_tokens'], 40000)
        self.assertEqual(u['cache_creation_input_tokens'], 8000)
        self.assertEqual(u['input_tokens'], 120)
        # output_tokens takes the delta's cumulative value (250, not 1)
        self.assertEqual(u['output_tokens'], 250)
        # prompt_tokens = input + cache_read + cache_write (OpenAI-shape total)
        self.assertEqual(u['prompt_tokens'], 120 + 40000 + 8000)


if __name__ == '__main__':
    unittest.main()
