"""tests/test_compat_openai.py — OpenAI compat translator unit tests.

Covers the pure-function translator only (no HTTP); the routes are
exercised separately via integration tests.
"""

import threading
import time
import unittest


class OpenAITranslateTest(unittest.TestCase):

    def test_simple_request(self):
        from lib.compat.openai import translate_openai_request
        msgs, cfg, opts = translate_openai_request({
            'model': 'claude-opus-4-7',
            'messages': [{'role': 'user', 'content': 'Hi'}],
        })
        self.assertEqual(len(msgs), 1)
        self.assertEqual(cfg['model'], 'claude-opus-4-7')
        self.assertEqual(cfg['preset'], 'claude-opus-4-7')
        self.assertFalse(opts['stream'])

    def test_temperature_max_tokens(self):
        from lib.compat.openai import translate_openai_request
        _msgs, cfg, _opts = translate_openai_request({
            'model': 'gpt-x', 'messages': [],
            'temperature': 0.7, 'max_tokens': 1024,
            'top_p': 0.9, 'seed': 42,
        })
        self.assertEqual(cfg['temperature'], 0.7)
        self.assertEqual(cfg['maxTokens'], 1024)
        self.assertEqual(cfg['topP'], 0.9)
        self.assertEqual(cfg['seed'], 42)

    def test_tools_disable_auto_tools(self):
        from lib.compat.openai import translate_openai_request
        _msgs, cfg, _opts = translate_openai_request({
            'model': 'gpt-x', 'messages': [],
            'tools': [{'type': 'function', 'function': {'name': 'foo'}}],
        })
        self.assertIn('tools', cfg)
        # Tofu auto tools should be off when caller supplied explicit tools.
        self.assertEqual(cfg['searchMode'], 'off')
        self.assertFalse(cfg['fetchEnabled'])
        self.assertFalse(cfg['memoryEnabled'])
        self.assertFalse(cfg['mcpEnabled'])

    def test_response_format_maps_to_cfg(self):
        from lib.compat.openai import translate_openai_request
        rf = {'type': 'json_object'}
        _msgs, cfg, _opts = translate_openai_request({
            'model': 'gpt-x', 'messages': [],
            'response_format': rf,
        })
        self.assertEqual(cfg['responseFormat'], rf)

    def test_response_format_absent_when_not_requested(self):
        from lib.compat.openai import translate_openai_request
        _msgs, cfg, _opts = translate_openai_request({
            'model': 'gpt-x', 'messages': [],
        })
        self.assertNotIn('responseFormat', cfg)

    def test_reasoning_effort_maps_to_thinking_depth(self):
        from lib.compat.openai import translate_openai_request
        _m, cfg, _o = translate_openai_request({
            'model': 'o1', 'messages': [],
            'reasoning_effort': 'high',
        })
        self.assertEqual(cfg['thinkingDepth'], 'max')
        self.assertTrue(cfg['thinkingEnabled'])

    def test_stream_flag(self):
        from lib.compat.openai import translate_openai_request
        _m, _cfg, opts = translate_openai_request({
            'model': 'x', 'messages': [], 'stream': True,
        })
        self.assertTrue(opts['stream'])

    def test_invalid_messages_raises(self):
        from lib.compat.openai import translate_openai_request
        with self.assertRaises(ValueError):
            translate_openai_request({'messages': 'not a list'})

    def test_build_response_shape(self):
        from lib.compat.openai import build_openai_response
        task = {
            'id': 'abc', 'status': 'done', 'content': 'Hello',
            'thinking': '', 'finishReason': 'stop',
            'usage': {'prompt_tokens': 10, 'completion_tokens': 5},
            'toolRounds': [], 'aborted': False,
        }
        resp = build_openai_response(task, model='m', requested_id='id1')
        self.assertEqual(resp['id'], 'id1')
        self.assertEqual(resp['object'], 'chat.completion')
        self.assertEqual(resp['model'], 'm')
        self.assertEqual(resp['choices'][0]['message']['content'], 'Hello')
        self.assertEqual(resp['choices'][0]['finish_reason'], 'stop')
        self.assertEqual(resp['usage']['prompt_tokens'], 10)
        self.assertEqual(resp['usage']['completion_tokens'], 5)

    def test_build_response_aborted_maps_to_length(self):
        from lib.compat.openai import build_openai_response
        task = {'id': 'a', 'status': 'aborted', 'content': '',
                'finishReason': 'length', 'aborted': True}
        resp = build_openai_response(task, model='m')
        self.assertEqual(resp['choices'][0]['finish_reason'], 'length')

    def test_streaming_yields_done(self):
        from lib.compat.openai import stream_openai_chunks
        # NEW contract (epic pt_cb8f98b0cb9b47fb, step 3): raw content deltas
        # are NOT forwarded (unclassifiable mid-stream); the narration-free
        # deliverable is emitted at `done` from the segment model / content.
        task = {
            'id': 'abc',
            'content': 'Hello',
            'events': [
                {'type': 'delta', 'content': 'Hel', 'seq': 0},
                {'type': 'delta', 'content': 'lo', 'seq': 1},
                {'type': 'done', 'finishReason': 'stop',
                 'usage': {'prompt_tokens': 1, 'completion_tokens': 1}, 'seq': 2},
            ],
            'events_lock': threading.Lock(),
            'status': 'done', 'finishReason': 'stop',
        }
        import asyncio

        async def _drain():
            return [frame async for frame in
                    stream_openai_chunks(task, model='m')]

        out = asyncio.new_event_loop().run_until_complete(_drain())
        text = ''.join(out)
        # The deliverable is emitted (as one clean chunk at done), plus [DONE].
        self.assertIn('Hello', text)
        self.assertIn('[DONE]', text)


if __name__ == '__main__':
    unittest.main()
