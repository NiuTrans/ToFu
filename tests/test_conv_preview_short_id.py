"""tests/test_conv_preview_short_id.py — conv_preview short-id resolution.

Regression for the Project Brain hover-preview bug: the panel (and the
peer-message feed payload it renders from) sometimes carries a TRUNCATED
8-char conversation id (the ``[:8]`` display form) rather than the full
14-char id — a peer note's ``toConv`` in particular is stored short. The
``GET /api/v1/conversations/<id>/preview`` endpoint did an exact ``WHERE id=?``
lookup, missed the row, and the hover fell back to "Untitled / no messages".

The fix resolves a short id by UNIQUE prefix (``WHERE id LIKE ?``), accepting
the match only when it is unambiguous. This suite drives the endpoint's DB
logic directly against a fresh SQLite DB:

  • exact id           → resolves (unchanged behaviour)
  • truncated 8-char   → resolves to the full row + its first user message
  • ambiguous prefix   → NOT resolved (two rows share the prefix → 404)
  • unknown id         → 404
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install the Flask→Quart shim before importing routes (mirrors server.py).
import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit


def _make_app_ctx():
    """Minimal Quart app for a request context (mirrors test_api_response)."""
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


class ConvPreviewShortIdTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))

        # Seed a handful of conversations with realistic long ids that share
        # an 8-char prefix in the ambiguous case.
        from lib.database import get_thread_db, DOMAIN_CHAT
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)

        def _mk(cid, title, first_q):
            msgs = [
                {'role': 'user', 'content': first_q},
                {'role': 'assistant', 'content': 'ok'},
            ]
            db.execute(
                'INSERT INTO conversations '
                '(id, user_id, title, messages, created_at, updated_at, '
                ' settings, msg_count, search_text) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (cid, 1, title, json.dumps(msgs), now, now, '{}', len(msgs),
                 first_q))

        _mk('mr7hh5n6llzwnm', 'Finish tag guard', '不要展示不完整的 finish tag')
        _mk('mr80dhn1coz7f5', 'Tab CSS fix', 'tab 点击没有切换')
        # Ambiguous pair: two rows share the 8-char prefix 'dupdupab'.
        _mk('dupdupab1111aa', 'Dup one', 'first dup question')
        _mk('dupdupab2222bb', 'Dup two', 'second dup question')
        db.commit()

    @classmethod
    def tearDownClass(cls):
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        cls._tmp.cleanup()

    def _preview(self, conv_id):
        """Invoke the async endpoint and return the parsed JSON body + status.

        The async DB facade (``async_fetchone`` / ``async_fetchall``) runs on a
        separate executor pool whose connections don't follow the
        ``reset_sqlite_for_tests`` repoint in this process, so we route those
        two calls through the seeded thread-local DB. This still exercises the
        REAL endpoint branch logic (exact-miss → prefix-resolve → ambiguity
        guard) — only the transport is swapped.
        """
        import asyncio
        import unittest.mock as _mock
        from lib.database import get_thread_db, DOMAIN_CHAT
        import routes.conversations as rc

        async def _fake_fetchone(sql, params=None, *, domain=DOMAIN_CHAT):
            return get_thread_db(DOMAIN_CHAT).execute(sql, params).fetchone()

        async def _fake_fetchall(sql, params=None, *, domain=DOMAIN_CHAT):
            return get_thread_db(DOMAIN_CHAT).execute(sql, params).fetchall()

        app = _make_app_ctx()

        async def _run():
            # api_ok / api_not_found call jsonify → need a request context.
            async with app.test_request_context('/preview', method='GET'):
                resp = await rc.conv_preview(conv_id)
                response, status = (resp if isinstance(resp, tuple)
                                    else (resp, getattr(resp, 'status_code', 200)))
                body = await response.get_data(as_text=True)
                return json.loads(body), status

        with _mock.patch.object(rc, 'async_fetchone', _fake_fetchone), \
                _mock.patch.object(rc, 'async_fetchall', _fake_fetchall):
            return asyncio.run(_run())

    def test_exact_id_resolves(self):
        data, status = self._preview('mr7hh5n6llzwnm')
        self.assertEqual(data.get('id'), 'mr7hh5n6llzwnm')
        self.assertEqual(data.get('title'), 'Finish tag guard')
        self.assertIn('finish tag', data.get('firstUserMessage', ''))

    def test_truncated_short_id_resolves_by_prefix(self):
        # The exact bug: the panel passes the 8-char display id.
        data, status = self._preview('mr7hh5n6')
        self.assertEqual(data.get('id'), 'mr7hh5n6llzwnm',
                         'short id must resolve to the full conversation row')
        self.assertEqual(data.get('title'), 'Finish tag guard')
        self.assertTrue(data.get('firstUserMessage'),
                        'first user message must be populated, not empty')

        data2, _ = self._preview('mr80dhn1')
        self.assertEqual(data2.get('id'), 'mr80dhn1coz7f5')
        self.assertEqual(data2.get('title'), 'Tab CSS fix')

    def test_ambiguous_prefix_not_resolved(self):
        # Two rows share the prefix → must NOT guess; 404 is correct.
        data, status = self._preview('dupdupab')
        self.assertEqual(status, 404,
                         'an ambiguous prefix must not resolve to a random row')

    def test_unknown_id_is_404(self):
        _data, status = self._preview('doesnotexistxx')
        self.assertEqual(status, 404)


if __name__ == '__main__':
    unittest.main()
