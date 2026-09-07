"""tests/test_api_v1_chat_route.py — Exercise /api/v1/chat/completions.

The real orchestrator calls live LLMs. We stub ``spawn_task`` to write
a synthetic completion onto the task dict before returning, then verify
the route assembles a correct response.
"""

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest

import pytest

from tests.support.model_routing import (
    allow_native_test_endpoint,
    clear_test_model_routing,
    native_test_model,
    reset_native_test_model_route,
)


pytestmark = pytest.mark.unit


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class ChatRouteTest(unittest.TestCase):

    ROUTING_OWNER_ID = 1

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib import api_keys

        from quart import Quart
        cls.app = Quart(__name__, static_folder=None)
        cls.app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
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
        _row, cls.token = create_key(
            owner_user_id=cls.ROUTING_OWNER_ID,
            name='chat-bot', scopes=['chat'])
        cls._endpoint_allowance = allow_native_test_endpoint()
        cls._endpoint_allowance.__enter__()

    @classmethod
    def tearDownClass(cls):
        clear_test_model_routing(owner_user_id=cls.ROUTING_OWNER_ID)
        cls._endpoint_allowance.__exit__(None, None, None)
        cls._tmp.cleanup()

    def setUp(self):
        reset_native_test_model_route(
            owner_user_id=self.ROUTING_OWNER_ID)
        # Clear idempotency cache so test order doesn't matter.
        from lib.idempotency import _cache
        _cache.clear()
        # Stub spawn_task so it immediately marks the task done with
        # synthetic content / usage.
        import lib.tasks_pkg.spawn as pkg

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
        import lib.tasks_pkg.spawn as pkg
        pkg.spawn_task = self._orig_spawn

    def test_sync_completion(self):
        async def go():
            r = await self.app.test_client().post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}'},
                json={
                    'model': native_test_model(),
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
            self.assertEqual(body['model'], 'stub-model')
            self.assertEqual(body['choices'][0]['message']['content'],
                             'Hello from stub')
            self.assertEqual(body['choices'][0]['finish_reason'], 'stop')
            self.assertEqual(body['usage']['total_tokens'], 15)
            self.assertIn('task_id', body)
        _new_loop_run(go())

    def test_sync_stream_failure_is_http_error_not_fake_completion(self):
        import lib.tasks_pkg.spawn as pkg

        current_spawn = pkg.spawn_task

        def _failed_spawn(task):
            task['content'] = 'safe prefix'
            task['status'] = 'done'
            task['finishReason'] = 'premature_close'
            task['streamState'] = 'malformed_stream'
            from lib.tasks_pkg.manager import append_event
            append_event(task, {
                'type': 'done',
                'finishReason': 'premature_close',
                'streamState': 'malformed_stream',
            })

        pkg.spawn_task = _failed_spawn
        try:
            async def go():
                response = await self.app.test_client().post(
                    '/api/v1/chat/completions',
                    headers={'Authorization': f'Bearer {self.token}'},
                    json={
                        'model': native_test_model(),
                        'messages': [{'role': 'user', 'content': 'Hi'}],
                        'timeout_s': 5,
                    },
                )
                self.assertEqual(response.status_code, 500)
                body = await response.get_json()
                self.assertFalse(body['ok'])
                self.assertNotIn('choices', body)

            _new_loop_run(go())
        finally:
            pkg.spawn_task = current_spawn

    def test_native_stream_failure_uses_error_event(self):
        from routes.api_v1.chat import _stream_generator

        task = {
            'id': 'native-cut',
            'status': 'done',
            'content': 'safe prefix',
            'finishReason': 'premature_close',
            'streamState': 'malformed_stream',
            'events': [{
                'type': 'done',
                'finishReason': 'premature_close',
                'streamState': 'malformed_stream',
                'seq': 0,
            }],
            'events_lock': threading.Lock(),
        }

        async def go():
            return [frame async for frame in _stream_generator(
                task, 'test-model', 'chatcmpl-cut')]

        frames = _new_loop_run(go())
        payloads = [
            json.loads(frame[len('data: '):].strip())
            for frame in frames
            if frame.startswith('data: ') and '[DONE]' not in frame
        ]
        self.assertEqual(payloads[0]['error']['code'],
                         'provider_stream_error')
        self.assertFalse(any('choices' in payload for payload in payloads))

    def test_rejected_without_auth(self):
        # Credential gate only fires in private/multi-user mode; open mode
        # (default) lets unauth requests through with a synthetic principal.
        from lib.auth_mode import reset_for_tests
        prev = os.environ.get('TOFU_AUTH_MODE')
        os.environ['TOFU_AUTH_MODE'] = 'private'
        reset_for_tests()
        try:
            async def go():
                r = await self.app.test_client().post(
                    '/api/v1/chat/completions',
                    json={'messages': [{'role': 'user', 'content': 'Hi'}]})
                self.assertEqual(r.status_code, 401)
            _new_loop_run(go())
        finally:
            if prev is None:
                os.environ.pop('TOFU_AUTH_MODE', None)
            else:
                os.environ['TOFU_AUTH_MODE'] = prev
            reset_for_tests()

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
                json={'model': native_test_model(),
                      'messages': [{'role': 'user', 'content': 'Hi'}],
                      'timeout_s': 5})
            self.assertEqual(r1.status_code, 200)
            d1 = await r1.get_json()
            tid1 = d1['task_id']
            # Second call with same key → should replay the same task_id
            r2 = await cli.post(
                '/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {self.token}',
                         'Idempotency-Key': 'k-replay-1'},
                json={'model': native_test_model(),
                      'messages': [{'role': 'user', 'content': 'Hi'}],
                      'timeout_s': 5})
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(r2.headers.get('Idempotency-Replay'), 'true')
            d2 = await r2.get_json()
            self.assertEqual(d2['task_id'], tid1)
        _new_loop_run(go())


if __name__ == '__main__':
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_api_v1_chat_route.py')
    unittest.main()
