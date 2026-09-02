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

import contextlib
import unittest
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

TEST_OWNER_USER_ID = 1

_CONVERSATION_IDS = (
    'mr7hh5n6llzwnm',
    'mr7hn42qp518zu',
    'dupdupab1111aa',
    'dupdupab2222bb',
)


@pytest.fixture(autouse=True)
def _conversation_authority(chat_sidecar):
    """Give each resolution test an isolated owned conversation catalog."""
    from tests._seed import delete_conversation, seed_conversation
    for conversation_id in _CONVERSATION_IDS:
        seed_conversation(conversation_id, title='Peer resolution test')
    try:
        yield
    finally:
        for conversation_id in _CONVERSATION_IDS:
            delete_conversation(conversation_id)


@contextlib.contextmanager
def _fake_live_task(conv_id, *, task_id='livetask00001x'):
    """Register a fake LIVE task for the duration of the block. With a live
    (drain-eligible) target, send_peer_message keeps the durable row QUEUED
    (the fast-path twin + completion hook own delivery) — the shape these
    resolution tests assert. Without it the event-channel send-time idle
    drain would deliver immediately and consume the row."""
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    t = {'id': task_id, 'convId': conv_id, 'status': 'running',
         '_userId': TEST_OWNER_USER_ID, 'aborted': False,
         'config': {'model': 'm'}, 'toolRounds': []}
    with tasks_lock:
        tasks[task_id] = t
    try:
        yield t
    finally:
        with tasks_lock:
            tasks.pop(task_id, None)


class PeerMessageTargetResolutionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._sender = 'mr7hn42qp518zu'
        cls._target_full = 'mr7hh5n6llzwnm'
        cls._target_short = 'mr7hh5n6'

    def setUp(self):
        # Clear the per-(sender,target) rate window + any queued rows between
        # tests so each starts clean.
        import lib.conversations.project_peer as pp
        with pp._rate_lock:
            pp._peer_msg_history.clear()
        from lib.message_queue import clear_queue
        for cid in ('mr7hh5n6llzwnm', 'mr7hn42qp518zu',
                    'dupdupab1111aa', 'dupdupab2222bb'):
            clear_queue(cid, user_id=TEST_OWNER_USER_ID)
        # The busy-target path enqueues fast-path inbox twins — reset them too.
        from lib import agent_inbox
        for cid in ('mr7hh5n6llzwnm', 'mr7hn42qp518zu'):
            agent_inbox.reset_for_test(cid)

    # ── the resolver itself ──────────────────────────────────────────
    def test_resolve_exact(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        self.assertEqual(_resolve_target_conv_id(self._target_full, user_id=TEST_OWNER_USER_ID),
                         (self._target_full, ''))

    def test_resolve_short_prefix(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        self.assertEqual(_resolve_target_conv_id(self._target_short, user_id=TEST_OWNER_USER_ID),
                         (self._target_full, ''))

    def test_resolve_ambiguous(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        full, err = _resolve_target_conv_id('dupdupab', user_id=TEST_OWNER_USER_ID)
        self.assertEqual(err, 'ambiguous_target')
        self.assertEqual(full, '')

    def test_resolve_unknown(self):
        from lib.conversations.project_peer import _resolve_target_conv_id
        full, err = _resolve_target_conv_id('nosuchconv99', user_id=TEST_OWNER_USER_ID)
        self.assertEqual(err, 'unknown_target')
        self.assertEqual(full, '')

    # ── DB-error fail-CLOSED for a truncated id (no phantom-queue loss) ──
    def test_resolve_db_error_truncated_id_fails_closed(self):
        """On a DB fault a TRUNCATED (prefix) id must NOT be returned verbatim —
        enqueuing it would land in a phantom queue no conversation drains
        (silent loss). It must fail closed with 'resolve_failed'."""
        import lib.conversations.project_peer as pp

        with patch(
            'lib.conversations.repository.get_conversation',
            side_effect=RuntimeError('storage unavailable'),
        ):
            full, err = pp._resolve_target_conv_id(self._target_short, user_id=TEST_OWNER_USER_ID)  # 8-char
        self.assertEqual(full, '')
        self.assertEqual(err, 'resolve_failed')

    def test_resolve_db_error_full_id_passes_through(self):
        """A FULL-length id is already canonical — a transient DB blip must not
        drop a valid send. It passes through unchanged (no error)."""
        import lib.conversations.project_peer as pp

        with patch(
            'lib.conversations.repository.get_conversation',
            side_effect=RuntimeError('storage unavailable'),
        ):
            full, err = pp._resolve_target_conv_id(self._target_full, user_id=TEST_OWNER_USER_ID)  # 14-char
        self.assertEqual(full, self._target_full)
        self.assertEqual(err, '')

    # ── end-to-end: the message lands under the FULL id ───────────────
    def test_short_id_message_enqueues_under_full_id(self):
        from lib.conversations.project_peer import send_peer_message
        from lib.message_queue import get_queue

        # Busy target: the event-channel send-time drain defers (the twin +
        # completion hook own delivery), so the durable row stays QUEUED —
        # the resolution-keyed shape this test asserts.
        with _fake_live_task(self._target_full):
            res = send_peer_message('/proj', self._sender, self._target_short,
                                    'watch the parser epic', user_id=TEST_OWNER_USER_ID)
        self.assertTrue(res.get('ok'), f'send failed: {res}')

        # The queue must be keyed on the FULL id (what dequeue_next reads) …
        full_q = get_queue(self._target_full, user_id=TEST_OWNER_USER_ID)
        self.assertEqual(len(full_q), 1,
                         'peer message must be enqueued under the FULL conv_id')
        self.assertIn('watch the parser epic', full_q[0]['text'])
        # … and NOT under the truncated phantom id.
        short_q = get_queue(self._target_short, user_id=TEST_OWNER_USER_ID)
        self.assertEqual(len(short_q), 0,
                         'nothing may be enqueued under the truncated phantom id')

    def test_full_id_message_still_works(self):
        from lib.conversations.project_peer import send_peer_message
        from lib.message_queue import get_queue
        with _fake_live_task(self._target_full):
            res = send_peer_message('/proj', self._sender, self._target_full, 'hi', user_id=TEST_OWNER_USER_ID)
        self.assertTrue(res.get('ok'))
        self.assertEqual(len(get_queue(self._target_full, user_id=TEST_OWNER_USER_ID)), 1)

    def test_ambiguous_target_refused_not_delivered(self):
        from lib.conversations.project_peer import send_peer_message
        from lib.message_queue import get_queue
        res = send_peer_message('/proj', self._sender, 'dupdupab', 'x', user_id=TEST_OWNER_USER_ID)
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('error'), 'ambiguous_target')
        # Neither ambiguous row may receive the message.
        self.assertEqual(len(get_queue('dupdupab1111aa', user_id=TEST_OWNER_USER_ID)), 0)
        self.assertEqual(len(get_queue('dupdupab2222bb', user_id=TEST_OWNER_USER_ID)), 0)

    def test_unknown_target_refused(self):
        from lib.conversations.project_peer import send_peer_message
        res = send_peer_message('/proj', self._sender, 'nosuchconv99', 'x', user_id=TEST_OWNER_USER_ID)
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('error'), 'unknown_target')

    def test_self_send_via_short_id_caught_on_resolved_id(self):
        # Sender addresses ITS OWN conversation by an 8-char prefix → the
        # self-check must fire on the RESOLVED full id.
        from lib.conversations.project_peer import send_peer_message
        res = send_peer_message('/proj', self._target_full, 'mr7hh5n6', 'x', user_id=TEST_OWNER_USER_ID)
        self.assertFalse(res.get('ok'))
        self.assertEqual(res.get('error'), 'cannot_message_self')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_peer_message_target_resolution.__main__')
    unittest.main()
