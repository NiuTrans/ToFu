"""tests/test_branch_routes.py — branch CRUD route tests.

Boots a Quart app with a SQLite-backed conversation, exercises the
v1 branch endpoints end-to-end.
"""

from __future__ import annotations

import asyncio
import inspect
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
    if inspect.iscoroutinefunction(_QR.get_json):
        _orig = _QR.get_json

        def _sync(self, *a, **kw):
            return asyncio.run(_orig(self, *a, **kw))
        _QR.get_json = _sync


def _new_loop_run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@unittest.skipIf(
    os.environ.get('TOFU_SKIP_NETWORK_E2E') == '1',
    'TOFU_SKIP_NETWORK_E2E=1 set')
class BranchRouteTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _install_shim()
        cls._tmp = tempfile.TemporaryDirectory()
        cls._tmp_db = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        cls._tmp_db.close()

        # Env MUST be set BEFORE server.py / lib.database import — those
        # modules read paths and PG_* config once at bootstrap.
        os.environ['TOFU_DB_BACKEND'] = 'sqlite'
        os.environ['TOFU_DB_PATH'] = cls._tmp_db.name
        os.environ['TUNNEL_TOKEN'] = ''

        from lib import api_keys
        cls._orig_path = api_keys._STORE_PATH
        api_keys._STORE_PATH = os.path.join(cls._tmp.name, 'api_keys.json')
        api_keys._cache.clear()
        api_keys._cache_loaded = False

        # Boot the real server. Server.py runs DB schema bootstrap.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'server_branches', 'server.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        cls.app = mod.app

        # Force schema bootstrap (idempotent).
        try:
            from lib.database import init_db
            init_db()
        except Exception as e:
            print(f'[branch_routes_test] init_db: {e}')

        from lib.api_keys import create_key
        _row, cls.token = create_key(
            name='branch-test', scopes=['conversations'])
        # Counter for unique conv ids per test (avoids test-order coupling).
        cls._conv_counter = 0

    def setUp(self):
        # Fresh conv per test
        type(self)._conv_counter += 1
        self.conv_id = f'test-conv-{os.getpid()}-{type(self)._conv_counter}'

        async def _seed():
            from lib.database import DOMAIN_CHAT, get_db, json_dumps_pg
            from routes.common import DEFAULT_USER_ID
            import time
            async with self.app.app_context():
                db = get_db(DOMAIN_CHAT)
                now_ms = int(time.time() * 1000)
                messages = [
                    {'role': 'user', 'content': 'hello'},
                    {'role': 'assistant', 'content': 'world'},
                ]
                db.execute(
                    'INSERT INTO conversations (id, user_id, title, messages, '
                    ' settings, created_at, updated_at, search_text) '
                    'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    (self.conv_id, DEFAULT_USER_ID, 'Test',
                     json_dumps_pg(messages), '{}', now_ms, now_ms,
                     'hello world'),
                )
                db.commit()

        _new_loop_run(_seed())

    @classmethod
    def tearDownClass(cls):
        from lib import api_keys
        api_keys._STORE_PATH = cls._orig_path
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        cls._tmp.cleanup()
        try:
            os.unlink(cls._tmp_db.name)
        except OSError:
            pass

    def _hdr(self):
        return {'Authorization': f'Bearer {self.token}'}

    # ── List ──────────────────────────────────────────────────────

    def test_list_empty(self):
        async def go():
            return await self.app.test_client().get(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr())
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        self.assertEqual(body['count'], 0)

    def test_list_msg_out_of_range(self):
        async def go():
            return await self.app.test_client().get(
                f'/api/v1/conversations/{self.conv_id}/messages/99/branches',
                headers=self._hdr())
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 400)

    def test_list_unknown_conv(self):
        async def go():
            return await self.app.test_client().get(
                '/api/v1/conversations/no-such-conv/messages/0/branches',
                headers=self._hdr())
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 404)

    # ── Create ────────────────────────────────────────────────────

    def test_create_basic(self):
        async def go():
            return await self.app.test_client().post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(),
                json={'title': 'paper review'})
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200,
                          _new_loop_run(r.get_data(as_text=True)))
        body = _new_loop_run(r.get_json())
        self.assertTrue(body['ok'])
        b = body['branch']
        self.assertTrue(b['id'])
        self.assertEqual(b['title'], 'paper review')
        # Server-classified kind
        self.assertEqual(b['kind'], 'paper')
        self.assertEqual(b['messages'], [])
        self.assertGreaterEqual(body['branch_idx'], 0)
        self.assertGreaterEqual(body['total_branches'], 1)

    def test_create_with_anchor(self):
        async def go():
            return await self.app.test_client().post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(),
                json={'title': 'code branch',
                      'anchor_text': 'function foo() {}',
                      'parent_selection': 'function foo() { return 42; }'})
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        b = body['branch']
        self.assertEqual(b['anchorText'], 'function foo() {}')
        self.assertEqual(b['parentSelection'], 'function foo() { return 42; }')
        self.assertEqual(b['kind'], 'code')

    def test_create_empty_title_400(self):
        async def go():
            return await self.app.test_client().post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(), json={'title': '   '})
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 400)

    def test_create_msg_out_of_range(self):
        async def go():
            return await self.app.test_client().post(
                f'/api/v1/conversations/{self.conv_id}/messages/99/branches',
                headers=self._hdr(), json={'title': 'x'})
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 400)

    def test_create_then_list(self):
        async def go():
            c = self.app.test_client()
            await c.post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(), json={'title': 'first'})
            await c.post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(), json={'title': 'second'})
            return await c.get(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr())
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 200)
        body = _new_loop_run(r.get_json())
        self.assertEqual(body['count'], 2)
        titles = [b['title'] for b in body['branches']]
        self.assertEqual(titles, ['first', 'second'])

    # ── Delete ────────────────────────────────────────────────────

    def test_delete_branch(self):
        async def go():
            c = self.app.test_client()
            r = await c.post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(), json={'title': 'doomed'})
            body = await r.get_json()
            bidx = body['branch_idx']
            r2 = await c.delete(
                f'/api/v1/conversations/{self.conv_id}/messages/1/'
                f'branches/{bidx}',
                headers=self._hdr())
            r3 = await c.get(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr())
            return r2, r3
        r2, r3 = _new_loop_run(go())
        self.assertEqual(r2.status_code, 200)
        body3 = _new_loop_run(r3.get_json())
        self.assertEqual(body3['count'], 0)

    def test_delete_branch_idx_out_of_range(self):
        async def go():
            return await self.app.test_client().delete(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches/99',
                headers=self._hdr())
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 400)

    def test_create_unique_ids(self):
        """Two rapid creates must NOT collide on id."""
        async def go():
            c = self.app.test_client()
            r1 = await c.post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(), json={'title': 'a'})
            r2 = await c.post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                headers=self._hdr(), json={'title': 'b'})
            return r1, r2
        r1, r2 = _new_loop_run(go())
        b1 = _new_loop_run(r1.get_json())['branch']
        b2 = _new_loop_run(r2.get_json())['branch']
        self.assertNotEqual(b1['id'], b2['id'])

    # ── Auth ──────────────────────────────────────────────────────

    def test_unauth_rejected(self):
        async def go():
            return await self.app.test_client().post(
                f'/api/v1/conversations/{self.conv_id}/messages/1/branches',
                json={'title': 'x'})
        r = _new_loop_run(go())
        self.assertEqual(r.status_code, 401)


if __name__ == '__main__':
    unittest.main()
