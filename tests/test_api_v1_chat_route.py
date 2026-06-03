"""tests/test_api_v1_chat_route.py — Exercise /api/v1/chat/completions.

The real orchestrator calls live LLMs. We stub ``spawn_task`` to write
a synthetic completion onto the task dict before returning, then verify
the route assembles a correct response.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest


def _install_shim():
    import quart
    sys.modules['flask'] = quart
    for attr in ('json', 'globals', 'helpers', 'wrappers', 'ctx'):
        qs = f'quart.{attr}'
        if qs in sys.modules:
            sys.modules[f'flask.{attr}'] = sys.modules[qs]
    from quart.wrappers import Request as _QR
    # If server.py's _install_flask_shim ran first, get_json is already
    # the sync-safe wrapper. Detect by inspecting the function — a
    # coroutine function is the unpatched original.
    import inspect
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync_get_json(self, *a, **kw):
            import asyncio as _a
            coro = _orig(self, *a, **kw)
            return _a.run(coro)
        _QR.get_json = _sync_get_json


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ChatRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys
        cls._orig = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        os.environ['TUNNEL_TOKEN'] = 'test-tunnel-no-real'

        from quart import Quart
        cls.app = Quart(__name__)
        cls.app.config['TESTING'] = True
        from routes.api_v1.auth import (
            attach_rate_headers, bearer_auth_before_request,
        )
        cls.app.before_request(bearer_auth_before_request)
        cls.app.after_request(attach_rate_headers)
        from routes.api_v1.chat import api_v1_chat_bp
        cls.app.register_blueprint(api_v1_chat_bp)

        # Mint a chat-scoped key.
        from lib.api_keys import create_key
        _row, cls.token = create_key(name='chat-bot', scopes=['chat'])

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()

    def setUp(self):
        # Clear idempotency cache so test order doesn't matter.
        from lib.idempotency import _cache
        _cache.clear()
        # Stub spawn_task so it immediately marks the task done with
        # synthetic content / usage.
        import lib.tasks_pkg as pkg

        def _fake_spawn(task):
            task['content'] = 'Hello from stub'
            task['thinking'] = ''
            task['status'] = 'done'
            task['finishReason'] = 'stop'
            task['usage'] = {'input_tokens': 10, 'output_tokens': 5,
                             'total_tokens': 15}
            # Append the matching events for SSE consumers.
            from lib.tasks_pkg.manager import append_event
            append_event(task, {'type': 'delta', 'content': 'Hello from stub'})
            append_event(task, {'type': 'done', 'finishReason': 'stop',
                                 'usage': task['usage']})

        self._orig_spawn = pkg.spawn_task
        pkg.spawn_task = _fake_spawn

    def tearDown(self):
        import lib.tasks_pkg as pkg
        pkg.spawn_task = self._orig_spawn

    def test_sync_completion(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': 'test-model',
                    'messages': [{'role': 'user', 'content': 'Hi'}],
                    'timeout_s': 5,
                })
            self.assertEqual(r.status_code, 200,
                              await r.get_data(as_text=True))
            body = await r.get_json()
            self.assertTrue(body['ok'])
            # api_ok(dict) merges fields into the top-level — body IS the
            # OpenAI-shaped completion (with an extra `ok:true` key).
            self.assertEqual(body['object'], 'chat.completion')
            self.assertEqual(body['model'], 'test-model')
            self.assertEqual(body['choices'][0]['message']['content'],
                             'Hello from stub')
            self.assertEqual(body['choices'][0]['finish_reason'], 'stop')
            self.assertEqual(body['usage']['total_tokens'], 15)
            self.assertIn('task_id', body)
        _new_loop_run(go())

    def test_rejected_without_auth(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/chat/completions',
                json={'messages': [{'role': 'user', 'content': 'Hi'}]})
            self.assertEqual(r.status_code, 401)
        _new_loop_run(go())

    def test_empty_messages_rejected(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'messages': []})
            self.assertEqual(r.status_code, 400)
        _new_loop_run(go())

    def test_invalid_role_rejected(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={'messages': [{'role': 'wrong', 'content': 'x'}]})
            self.assertEqual(r.status_code, 400)
        _new_loop_run(go())

    def test_idempotency_key_replays(self):
        async def go():
            cli = self.app.test_client()
            # First call
            r1 = await cli.post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}',
                         'Idempotency-Key': 'k-replay-1'},
                json={'messages': [{'role': 'user', 'content': 'Hi'}],
                      'timeout_s': 5})
            self.assertEqual(r1.status_code, 200)
            d1 = await r1.get_json()
            tid1 = d1['task_id']
            # Second call with same key → should replay the same task_id
            r2 = await cli.post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}',
                         'Idempotency-Key': 'k-replay-1'},
                json={'messages': [{'role': 'user', 'content': 'Hi'}],
                      'timeout_s': 5})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.headers.get('Idempotency-Replay'), 'true')
            d2 = await r2.get_json()
            self.assertEqual(d2['task_id'], tid1)
        _new_loop_run(go())


if __name__ == '__main__':
    unittest.main()
