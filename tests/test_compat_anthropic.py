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


if __name__ == '__main__':
    unittest.main()
