"""tests/test_peer_message_target_resolution.py — peer-message DELIVERY fix.

The Project Brain peer tools surface conversation ids in an 8-char display form
(``project_peer_status`` prints ``[{convId[:8]}]``). An agent copies that short
id verbatim into ``project_message`` / ``project_intervene``. Before the fix
``send_peer_message`` → ``enqueue_message`` used it as the queue KEY, but
``dequeue_next`` / the redispatch sweep / the task registry key on the FULL
14-char id — so a message enqueued under ``mr7hh5n6`` was invisible to
conversation ``mr7hh5n6llzwnm`` and NEVER delivered (and the short id registered
as an orphaned-dispatchable conv mapping to nothing).

The fix resolves the target id to its canonical FULL id (exact, else unique
prefix) BEFORE the self-check / enqueue / feed emit, refusing on ambiguity /
no-match. This suite proves — against a REAL seeded DB — that:

  • a message addressed by the 8-char id lands in the queue under the FULL id;
  • an exact full id still works;
  • an ambiguous prefix is REFUSED (not mis-delivered to a random row);
  • an unknown id is REFUSED;
  • self-send is caught on the RESOLVED id (short-id addressing self).
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

import pytest

pytestmark = pytest.mark.unit


class PeerMessageTargetResolutionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))

        from lib.database import get_thread_db, DOMAIN_CHAT
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)

        def _mk(cid):
            db.execute(
                'INSERT INTO conversations '
                '(id, user_id, title, messages, created_at, updated_at, '
                ' settings, msg_count, search_text) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (cid, 1, 'T', '[]', now, now, '{}', 0, ''))

        _mk('mr7hh5n6llzwnm')          # the real target
        _mk('mr7hn42qp518zu')          # the sender
        _mk('dupdupab1111aa')          # ambiguous pair …
        _mk('dupdupab2222bb')          # … shares 8-char prefix 'dupdupab'
        db.commit()
        cls._sender = 'mr7hn42qp518zu'
        cls._target_full = 'mr7hh5n6llzwnm'
        cls._target_short = 'mr7hh5n6'

    @classmethod
    def tearDownClass(cls):
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        cls._tmp.cleanup()

    def setUp(self):
        # Clear the per-(sender,target) rate window + any queued rows between
        # tests so each starts clean.
        import lib.conversations.project_peer as pp
        with pp._rate_lock:
            pp._peer_msg_history.clear()
        from lib.message_queue import clear_queue
        for cid in ('mr7hh5n6llzwnm', 'mr7hn42qp518zu',
                    'dupdupab1111aa', 'dupdupab2222bb'):
            clear_queue(cid)

    # ── the resolver itself ──────────────────────────────────────────
    def test_resolve_exact(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        self.assertEqual(_resolve_target_conv_id(self._target_full),
                         (self._target_full, ''))

    def test_resolve_short_prefix(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        self.assertEqual(_resolve_target_conv_id(self._target_short),
                         (self._target_full, ''))

    def test_resolve_ambiguous(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        full, err = _resolve_target_conv_id('dupdupab')
        self.assertEqual(err, 'ambiguous_target')
        self.assertEqual(full, '')

    def test_resolve_unknown(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        full, err = _resolve_target_conv_id('nosuchconv99')
        self.assertEqual(err, 'unknown_target')
        self.assertEqual(full, '')

    # ── end-to-end: the message lands under the FULL id ───────────────
    def test_short_id_message_enqueues_under_full_id(self):
        from lib.conversations.project_peer import send_peer_message
        from lib.message_queue import get_queue

        res = send_peer_message('/proj', self._sender, self._target_short,
                                'watch the parser epic')
        self.assertTrue(res.get('ok'), f'send failed: {res}')

        # The queue must be keyed on the FULL id (what dequeue_next reads) …
        full_q = get_queue(self._target_full)
        self.assertEqual(len(full_q), 1,
                         'peer message must be enqueued under the FULL conv_id')
        self.assertIn('watch the parser epic', full_q[0]['text'])
        # … and NOT under the truncated phantom id.
        short_q = get_queue(self._target_short)
        self.assertEqual(len(short_q), 0,
                         'nothing may be enqueued under the truncated phantom id')

    def test_full_id_message_still_works(self):
        from lib.conversations.project_peer import send_peer_message
        from lib.message_queue import get_queue
        res = send_peer_message('/proj', self._sender, self._target_full, 'hi')
        self.assertTrue(res.get('ok'))
        self.assertEqual(len(get_queue(self._target_full)), 1)

    def test_ambiguous_target_refused_not_delivered(self):
        from lib.conversations.project_peer import send_peer_message
        from lib.message_queue import get_queue
        res = send_peer_message('/proj', self._sender, 'dupdupab', 'x')
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('error'), 'ambiguous_target')
        # Neither ambiguous row may receive the message.
        self.assertEqual(len(get_queue('dupdupab1111aa')), 0)
        self.assertEqual(len(get_queue('dupdupab2222bb')), 0)

    def test_unknown_target_refused(self):
        from lib.conversations.project_peer import send_peer_message
        res = send_peer_message('/proj', self._sender, 'nosuchconv99', 'x')
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('error'), 'unknown_target')

    def test_self_send_via_short_id_caught_on_resolved_id(self):
        # Sender addresses ITS OWN conversation by an 8-char prefix → the
        # self-check must fire on the RESOLVED full id.
        from lib.conversations.project_peer import send_peer_message
        res = send_peer_message('/proj', self._target_full, 'mr7hh5n6', 'x')
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('error'), 'cannot_message_self')


if __name__ == '__main__':
    unittest.main()
