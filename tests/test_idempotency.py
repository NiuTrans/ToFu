"""tests/test_idempotency.py — Idempotency-Key cache behaviour.

Uses a minimal Quart app to exercise the decorator under a real request
context. Mocking `flask.request` in isolation is awkward because Quart
defers the proxy resolution.
"""

import asyncio
import time
import unittest


class IdempotencyTest(unittest.TestCase):

    def setUp(self):
        from lib.idempotency import _cache
        _cache.clear()
        # Build a minimal Quart app once.
        from quart import Quart
        from lib.idempotency import idempotent_post
        app = Quart(__name__)
        app.config['TESTING'] = True

        self.calls = 0

        @app.route('/x', methods=['POST'])
        @idempotent_post()
        async def _x():
            self.calls += 1
            return {'ok': True, 'value': self.calls}, 200

        @app.route('/err', methods=['POST'])
        @idempotent_post()
        async def _err():
            self.calls += 1
            return {'ok': False, 'error': 'bad'}, 400

        @app.route('/slow-async', methods=['POST'])
        @idempotent_post()
        async def _slow_async():
            self.calls += 1
            await asyncio.sleep(0.05)
            return {'ok': True, 'value': self.calls}, 200

        @app.route('/slow-sync', methods=['POST'])
        @idempotent_post()
        def _slow_sync():
            self.calls += 1
            time.sleep(0.05)
            return {'ok': True, 'value': self.calls}, 200

        self.app = app

    def _post(self, path, headers=None):
        async def _run():
            client = self.app.test_client()
            return await client.post(path, headers=headers or {},
                                       json={})
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_no_header_passes_through(self):
        self._post('/x')
        self._post('/x')
        self.assertEqual(self.calls, 2)

    def test_replay_same_key_returns_cached(self):
        self._post('/x', {'Idempotency-Key': 'abc'})
        self._post('/x', {'Idempotency-Key': 'abc'})
        self.assertEqual(self.calls, 1)

    def test_different_keys_rerun(self):
        self._post('/x', {'Idempotency-Key': 'k1'})
        self._post('/x', {'Idempotency-Key': 'k2'})
        self.assertEqual(self.calls, 2)

    def test_error_status_not_cached(self):
        self._post('/err', {'Idempotency-Key': 'errkey'})
        self._post('/err', {'Idempotency-Key': 'errkey'})
        self.assertEqual(self.calls, 2)

    def _post_concurrently(self, path, key):
        async def _run():
            clients = [self.app.test_client(), self.app.test_client()]
            responses = await asyncio.gather(*[
                client.post(path, headers={'Idempotency-Key': key}, json={})
                for client in clients
            ])
            return [await response.get_json() for response in responses]

        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()

    def test_concurrent_async_duplicates_are_singleflight(self):
        bodies = self._post_concurrently('/slow-async', 'same-async')
        self.assertEqual(self.calls, 1)
        self.assertEqual([body['value'] for body in bodies], [1, 1])

    def test_concurrent_sync_duplicates_are_singleflight(self):
        bodies = self._post_concurrently('/slow-sync', 'same-sync')
        self.assertEqual(self.calls, 1)
        self.assertEqual([body['value'] for body in bodies], [1, 1])


if __name__ == '__main__':
    unittest.main()
