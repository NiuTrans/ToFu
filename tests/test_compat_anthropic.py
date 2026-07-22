"""tests/test_compat_anthropic.py — Anthropic compat translator unit tests."""

import threading
import unittest


class AnthropicTranslateTest(unittest.TestCase):

    def test_system_string_becomes_system_message(self):
        from lib.compat.anthropic import translate_anthropic_request
        msgs, _cfg, _opts = translate_anthropic_request({
            'model': 'claude',
            'system': 'You are helpful.',
            'max_tokens': 1024,
            'messages': [{'role': 'user', 'content': 'Hi'}],
        })
        self.assertEqual(msgs[0]['role'], 'system')
        self.assertEqual(msgs[0]['content'], 'You are helpful.')
        self.assertEqual(msgs[1]['role'], 'user')

    def test_system_array_blocks_concatenated(self):
        from lib.compat.anthropic import translate_anthropic_request
        msgs, _cfg, _opts = translate_anthropic_request({
            'model': 'claude',
            'system': [
                {'type': 'text', 'text': 'Part 1.'},
                {'type': 'text', 'text': 'Part 2.'},
            ],
            'max_tokens': 100,
            'messages': [{'role': 'user', 'content': 'Hi'}],
        })
        self.assertIn('Part 1.', msgs[0]['content'])
        self.assertIn('Part 2.', msgs[0]['content'])

    def test_thinking_budget_maps_to_depth(self):
        from lib.compat.anthropic import translate_anthropic_request
        for budget, expected in [(4096, 'medium'),
                                  (12000, 'high'),
                                  (24000, 'xhigh'),
                                  (50000, 'max')]:
            _m, cfg, _o = translate_anthropic_request({
                'model': 'claude', 'max_tokens': 100,
                'messages': [{'role': 'user', 'content': 'hi'}],
                'thinking': {'type': 'enabled', 'budget_tokens': budget},
            })
            self.assertEqual(cfg['thinkingDepth'], expected,
                             f'budget={budget}')
            self.assertTrue(cfg['thinkingEnabled'])

    def test_tools_disable_auto_tools(self):
        from lib.compat.anthropic import translate_anthropic_request
        _m, cfg, _o = translate_anthropic_request({
            'model': 'claude', 'max_tokens': 100,
            'messages': [{'role': 'user', 'content': 'hi'}],
            'tools': [{'name': 'foo', 'description': 'd',
                       'input_schema': {'type': 'object'}}],
        })
        self.assertEqual(cfg['searchMode'], 'off')
        self.assertFalse(cfg['fetchEnabled'])

    def test_response_shape(self):
        from lib.compat.anthropic import build_anthropic_response
        task = {
            'id': 'abc', 'status': 'done', 'content': 'Hello',
            'thinking': 'pondering', 'finishReason': 'stop',
            'usage': {'input_tokens': 12, 'output_tokens': 7},
            'toolRounds': [], 'aborted': False,
        }
        resp = build_anthropic_response(task, model='claude')
        self.assertEqual(resp['type'], 'message')
        self.assertEqual(resp['role'], 'assistant')
        self.assertEqual(resp['model'], 'claude')
        self.assertEqual(resp['stop_reason'], 'end_turn')
        # First block is thinking, second is text.
        self.assertEqual(resp['content'][0]['type'], 'thinking')
        self.assertEqual(resp['content'][1]['type'], 'text')
        self.assertEqual(resp['content'][1]['text'], 'Hello')
        self.assertEqual(resp['usage']['input_tokens'], 12)
        self.assertEqual(resp['usage']['output_tokens'], 7)

    def test_streaming_emits_named_events(self):
        import asyncio
        from lib.compat.anthropic import stream_anthropic_chunks
        # NEW contract (epic pt_cb8f98b0cb9b47fb, step 3): raw content deltas
        # are NOT streamed; the narration-free deliverable is emitted as one
        # text block at `done` from the segment model / content fallback.
        task = {
            'id': 'abc',
            'content': 'Hi',
            'events': [
                {'type': 'delta', 'content': 'Hi', 'seq': 0},
                {'type': 'done', 'finishReason': 'stop',
                 'usage': {'input_tokens': 1, 'output_tokens': 1}, 'seq': 1},
            ],
            'events_lock': threading.Lock(),
            'status': 'done',
        }

        async def _drain():
            return [frame async for frame in
                    stream_anthropic_chunks(task, model='claude')]

        out = ''.join(asyncio.new_event_loop().run_until_complete(_drain()))
        self.assertIn('event: message_start', out)
        self.assertIn('event: content_block_start', out)
        self.assertIn('event: content_block_delta', out)
        self.assertIn('"text": "Hi"', out)  # deliverable emitted at done
        self.assertIn('event: message_stop', out)


    def test_streaming_emits_tool_use_block(self):
        """A task that finished on a tool call must emit a tool_use content
        block in the stream (parity with the sync _content_blocks_from_task).
        Before the fix the stream `done` branch emitted no tool_use block → a
        streaming caller got stop_reason=tool_use with no tool_use payload."""
        import asyncio
        from lib.compat.anthropic import stream_anthropic_chunks
        tc = {'id': 'toolu_x', 'type': 'function',
              'function': {'name': 'get_weather', 'arguments': '{"city":"SF"}'}}
        task = {
            'id': 'abc', 'content': '',
            'events': [{'type': 'done', 'finishReason': 'tool_use', 'seq': 0}],
            'events_lock': threading.Lock(),
            'status': 'done', 'finishReason': 'tool_use',
            'toolRounds': [{'tool_calls': [tc]}],
        }

        async def _drain():
            return [f async for f in stream_anthropic_chunks(task, model='claude')]
        out = ''.join(asyncio.new_event_loop().run_until_complete(_drain()))
        self.assertIn('"type": "tool_use"', out,
                      'stream must emit a tool_use content block when the task '
                      'finished on a tool call')
        self.assertIn('get_weather', out)
        self.assertIn('input_json_delta', out)
        self.assertIn('"stop_reason": "tool_use"', out)

    def test_malformed_body_raises_valueerror_for_count_tokens_guard(self):
        # count_tokens now wraps translate_anthropic_request in try/except
        # ValueError → 400 (mirroring /v1/messages). This asserts the raise
        # the guard relies on: a non-array `messages` is a ValueError, not an
        # uncaught 500. (NEUTER context: without the route guard this same
        # exception escapes count_tokens as a 500.)
        from lib.compat.anthropic import translate_anthropic_request
        with self.assertRaises(ValueError):
            translate_anthropic_request({
                'model': 'claude', 'max_tokens': 10, 'messages': 'not-a-list',
            })


if __name__ == '__main__':
    unittest.main()
