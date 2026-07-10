"""tests/test_peer_status_title_backfill.py — peer roster shows human titles.

The Project Brain Team panel (and the agent-facing ``project_peer_status``)
rendered a bare ``conv <id>`` for every sibling, because presence ``announce``
is usually called with an empty ``convTitle`` → the presence snapshot's
``title`` is blank → the roster falls back to the opaque id.

``build_peer_status`` now backfills the REAL stored conversation title from the
DB for any peer whose presence title is empty, and falls back to a short
opening-question snippet when a conversation was never titled. This suite proves
— against a REAL seeded DB — that:

  • a peer with a stored title gets that title (never ``conv <id>``);
  • a never-titled conversation gets an opening-question snippet;
  • a presence-supplied title is NOT clobbered by the backfill;
  • an unknown conv resolves to no title (frontend/id fallback still applies).

Double-neuter: no-op the backfill loop in ``build_peer_status`` → the stored
title + snippet assertions FAIL (the view keeps its blank title).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest

import pytest

pytestmark = pytest.mark.unit


class PeerStatusTitleBackfillTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))

        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)

        def _mk(cid, title, messages):
            db.execute(
                'INSERT INTO conversations '
                '(id, user_id, title, messages, created_at, updated_at, '
                ' settings, msg_count, search_text) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (cid, 1, title, json.dumps(messages), now, now, '{}',
                 len(messages), ''))

        # A titled conversation.
        _mk('convtitled0001', 'Parser refactor work', [])
        # A never-titled conversation (title 'Untitled') with an opening turn.
        _mk('convuntitled02', 'Untitled',
            [{'role': 'user', 'content': 'Fix the SSE reconnect race in chat'}])
        db.commit()

    @classmethod
    def tearDownClass(cls):
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        cls._tmp.cleanup()

    def _stub_snapshot(self, peers):
        """Patch presence.snapshot to return `peers` for any project path."""
        import lib.presence.registry as reg
        self._orig_snapshot = reg.snapshot
        reg.snapshot = lambda _path: {'peers': peers}
        self.addCleanup(lambda: setattr(reg, 'snapshot', self._orig_snapshot))

    def test_titles_by_conv_resolves_stored_title(self):
        from lib.conversations.project_peer import _titles_by_conv
        out = _titles_by_conv(['convtitled0001'])
        self.assertEqual(out.get('convtitled0001'), 'Parser refactor work')

    def test_titles_by_conv_falls_back_to_snippet(self):
        from lib.conversations.project_peer import _titles_by_conv
        out = _titles_by_conv(['convuntitled02'])
        self.assertIn('SSE reconnect race', out.get('convuntitled02', ''))

    def test_titles_by_conv_unknown_absent(self):
        from lib.conversations.project_peer import _titles_by_conv
        out = _titles_by_conv(['nosuchconv0001'])
        self.assertNotIn('nosuchconv0001', out)

    def test_build_peer_status_backfills_titles(self):
        # Presence supplies these peers with BLANK titles (the real-world case).
        self._stub_snapshot([
            {'convId': 'convtitled0001', 'agentId': '', 'title': '',
             'statusLabel': 'generating'},
            {'convId': 'convuntitled02', 'agentId': '', 'title': '',
             'statusLabel': 'working'},
        ])
        from lib.conversations.project_peer import build_peer_status
        status = build_peer_status('/proj', 'someOtherConv')
        by_id = {p['convId']: p for p in status['peers']}
        self.assertEqual(by_id['convtitled0001']['title'], 'Parser refactor work')
        self.assertIn('SSE reconnect race', by_id['convuntitled02']['title'])

    def test_build_peer_status_keeps_presence_title(self):
        # A presence-supplied title must NOT be overwritten by the backfill.
        self._stub_snapshot([
            {'convId': 'convtitled0001', 'agentId': '',
             'title': 'Live presence title', 'statusLabel': 'generating'},
        ])
        from lib.conversations.project_peer import build_peer_status
        status = build_peer_status('/proj', 'someOtherConv')
        by_id = {p['convId']: p for p in status['peers']}
        self.assertEqual(by_id['convtitled0001']['title'], 'Live presence title')


if __name__ == '__main__':
    unittest.main()
