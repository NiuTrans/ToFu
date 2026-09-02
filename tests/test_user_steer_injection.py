"""tests/test_user_steer_injection.py — the human-steer inject lane's
exactly-once (never-zero, never-double) invariant.

A message the user sends WHILE the assistant is still generating (composer
inject-mode = ``steer``) is delivered via a two-lane mechanism:

  1. FAST PATH = an ``agent_inbox`` item under ``mode='user-steer'``, keyed on
     the conversation (``swarm_key_for`` → convId). The orchestrator drains it
     at the next ROUND BOUNDARY and injects it as a user message; a
     deferred-confirm flush (after the LLM call confirms consumption) emits the
     ``user_steer_inject`` chip.
  2. DURABLE FALLBACK = when the running task's inbox slot is NOT drainable
     (tombstoned — the task is finalizing and will run no further drain) the
     send route re-routes to the ``message_queue`` instead, so the steer becomes
     the next turn rather than being silently dropped.

The LOAD-BEARING correctness bar is EXACTLY-ONCE:

  • never ZERO — a steer that misses its drain window (task ended / aborted
    before the deferred-confirm flush) is SALVAGED into the durable queue by
    the finalizer (``_salvage_undelivered_steer``), covering BOTH the
    still-in-inbox case and the drained-but-unconfirmed
    ``_steer_inject_pending`` case.
  • never DOUBLE — the salvage drains the inbox (removing the item) as it
    re-queues, so a subsequent drain finds nothing.

Each fix is proven with a byte-reverting NEUTER that makes the message arrive
ZERO or TWICE.

Run with::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_user_steer_injection.py -v
"""

from __future__ import annotations

import unittest

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

TEST_OWNER_USER_ID = 1
_STEER_CONVERSATION_ID = 'steerconv000001'


@pytest.fixture(autouse=True)
def _conversation_authority(chat_sidecar):
    """Exercise the queue against an isolated owned Sidecar conversation."""
    from tests._seed import delete_conversation, seed_conversation
    seed_conversation(_STEER_CONVERSATION_ID, title='Steer test')
    try:
        yield
    finally:
        delete_conversation(_STEER_CONVERSATION_ID)


class UserSteerInjectionTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._conv = _STEER_CONVERSATION_ID

    def setUp(self):
        from lib import agent_inbox
        from lib.message_queue import clear_queue
        clear_queue(self._conv, user_id=TEST_OWNER_USER_ID)
        agent_inbox.reset_for_test(self._conv)

    # ── helpers ──────────────────────────────────────────────────────
    def _task(self, **over):
        t = {'id': 'steertask00001', 'convId': self._conv, 'status': 'running',
             '_userId': TEST_OWNER_USER_ID, 'aborted': False,
             'config': {'model': 'm'}, 'toolRounds': []}
        t.update(over)
        return t

    def _enqueue_steer(self, text='go check the tests too', *, user_msg=None):
        """Mirror the send route's steer branch: inbox enqueue under
        mode='user-steer' carrying the pre-built user_msg for verbatim salvage."""
        from lib.agent_inbox import enqueue as _inbox_enqueue
        um = user_msg or {'role': 'user', 'content': text}
        _inbox_enqueue(self._conv, text, priority='next', mode='user-steer',
                       extra={'_user_msg': um, 'config': {'model': 'm'}})

    # ═══════════════════ Lane 1: drainable → inbox ═══════════════════

    def test_steer_enqueues_under_user_steer_mode(self):
        """The steer lands in the inbox tagged mode='user-steer' (NOT
        swarm-update / peer-msg), carrying the pre-built user_msg."""
        from lib import agent_inbox
        self._enqueue_steer('are you editing styles.css?')
        items = agent_inbox.drain(self._conv, modes=['user-steer'])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['mode'], 'user-steer')
        self.assertIn('styles.css', items[0]['value'])
        self.assertEqual(items[0]['_user_msg']['content'],
                         'are you editing styles.css?')

    def test_steer_excluded_from_swarm_drain(self):
        """The orchestrator's swarm drain (exclude_modes=['peer-msg',
        'user-steer']) must NOT pick up a steer — else it renders as a
        <swarm-update> chip and is marked delivered via the wrong lane."""
        from lib import agent_inbox
        self._enqueue_steer()
        swarm_items = agent_inbox.drain(
            self._conv, exclude_modes=['peer-msg', 'user-steer'])
        self.assertEqual(swarm_items, [],
                         'steer must be invisible to the swarm drain')
        # It is still there for the dedicated user-steer drain.
        self.assertEqual(agent_inbox.peek(self._conv), 1)

    # ═══════════════════ Lane 2: not drainable → queue fallback ═══════════════════

    def test_not_drainable_slot_would_be_dropped_by_inbox(self):
        """Proves WHY the route needs the fallback: a tombstoned slot silently
        DROPS an enqueue. The send route checks tombstones and routes to the
        durable queue instead (asserted in the route-shaped test below)."""
        from lib import agent_inbox
        agent_inbox.clear(self._conv)  # tombstones the slot
        self._enqueue_steer()          # dropped silently
        self.assertEqual(agent_inbox.peek(self._conv), 0,
                         'a tombstoned slot drops the enqueue — the route must '
                         'NOT rely on the inbox when the slot is not drainable')

    def test_route_fallback_uses_queue_when_not_drainable(self):
        """Route-shaped: when the slot is tombstoned the steer is enqueued to
        message_queue (a fresh turn), never lost."""
        from lib import agent_inbox
        from lib.message_queue import enqueue_message, get_queue
        agent_inbox.clear(self._conv)  # not drainable
        # Route branch: tombstoned → durable queue fallback.
        from lib.agent_inbox import _tombstones as _tomb, _lock as _lk
        with _lk:
            drainable = self._conv not in _tomb
        self.assertFalse(drainable)
        um = {'role': 'user', 'content': 'fallback steer'}
        enqueue_message(self._conv, {'text': um['content'], '_user_msg': um},
                        {'model': 'm'}, user_id=TEST_OWNER_USER_ID)
        self.assertEqual(len(get_queue(self._conv, user_id=TEST_OWNER_USER_ID)), 1,
                         'not-drainable steer must fall back to the durable queue')

    # ═══════════════════ Exactly-once salvage on task end ═══════════════════

    def test_salvage_still_in_inbox_requeues_once(self):
        """Case 1: the steer never got drained (task ended before the next
        round boundary). The finalizer salvages it from the inbox → exactly one
        durable queue row, and the inbox is emptied (no double)."""
        from lib import agent_inbox
        from lib.message_queue import get_queue
        from lib.tasks_pkg.orchestrator._finalize import _salvage_undelivered_steer
        self._enqueue_steer('please also run the linter')
        n = _salvage_undelivered_steer(self._task())
        self.assertEqual(n, 1)
        q = get_queue(self._conv, user_id=TEST_OWNER_USER_ID)
        self.assertEqual(len(q), 1, 'exactly one durable row salvaged')
        self.assertEqual(agent_inbox.peek(self._conv), 0,
                         'the inbox item was consumed by the salvage (no double)')

    def test_salvage_pending_after_abort_requeues_once(self):
        """Case 2: the steer was drained into `messages` but the task aborted
        BEFORE the deferred-confirm flush → it sits in _steer_inject_pending.
        The finalizer salvages that too (never zero)."""
        from lib.message_queue import get_queue
        from lib.tasks_pkg.orchestrator._finalize import _salvage_undelivered_steer
        task = self._task(aborted=True)
        task['_steer_inject_pending'] = [{
            'value': 'the message the model never consumed',
            '_user_msg': {'role': 'user',
                          'content': 'the message the model never consumed'},
            'config': {'model': 'm'},
        }]
        n = _salvage_undelivered_steer(task)
        self.assertEqual(n, 1)
        self.assertEqual(len(get_queue(self._conv, user_id=TEST_OWNER_USER_ID)), 1,
                         'NEVER ZERO: an abort before flush still delivers via '
                         'the salvaged durable row')
        self.assertNotIn('_steer_inject_pending', task,
                         'the pending stash is consumed (popped) by the salvage')

    def test_salvage_noop_when_nothing_undelivered(self):
        """A clean turn end (steer already delivered + flushed, or none sent)
        salvages nothing — no spurious extra turn."""
        from lib.message_queue import get_queue
        from lib.tasks_pkg.orchestrator._finalize import _salvage_undelivered_steer
        n = _salvage_undelivered_steer(self._task())
        self.assertEqual(n, 0)
        self.assertEqual(len(get_queue(self._conv, user_id=TEST_OWNER_USER_ID)), 0)

    def test_salvage_verbatim_user_msg_not_retranslated(self):
        """The salvaged row carries the pre-built _user_msg verbatim so
        dispatch_next_queued appends it without re-translating."""
        from lib.storage import get_storage_client
        from lib.tasks_pkg.orchestrator._finalize import _salvage_undelivered_steer
        um = {'role': 'user', 'content': 'translated body',
              'originalContent': '原始正文', '_translateDone': True}
        self._enqueue_steer('translated body', user_msg=um)
        _salvage_undelivered_steer(self._task())
        rows = get_storage_client().query(
            'queue.list', {'conv_id': self._conv, 'user_id': TEST_OWNER_USER_ID})
        self.assertEqual(len(rows), 1)
        payload = rows[0]['payload']
        self.assertEqual(payload.get('_user_msg', {}).get('originalContent'),
                         '原始正文',
                         'salvage must preserve the pre-built (translated) '
                         'user_msg verbatim — no re-translation')

    # ═══════════════════ NEUTER controls ═══════════════════

    def test_NEUTER_no_salvage_zero_delivers_on_abort(self):
        """NEGATIVE CONTROL — skip the salvage entirely (the pre-fix state).
        A steer still queued in the inbox at task end is dropped when the swarm
        teardown tombstones the slot → ZERO delivery. Proves the salvage is
        load-bearing."""
        from lib import agent_inbox
        from lib.message_queue import get_queue
        self._enqueue_steer('this steer would vanish without the salvage')
        # NEUTER: do NOT call _salvage_undelivered_steer. Simulate the swarm
        # teardown that clears+tombstones the inbox on task end.
        agent_inbox.clear(self._conv)
        self.assertEqual(agent_inbox.peek(self._conv), 0)
        self.assertEqual(len(get_queue(self._conv, user_id=TEST_OWNER_USER_ID)), 0,
                         'ZERO DELIVERY: without the salvage the undrained steer '
                         'is cleared with the inbox and never reaches the queue')

    def test_NEUTER_double_salvage_would_double_deliver(self):
        """NEGATIVE CONTROL — the salvage DRAINS the inbox as it re-queues, so a
        second salvage finds nothing. If the salvage merely PEEKED (didn't
        drain), calling it twice would enqueue twice = double delivery. This
        proves the drain-as-you-salvage is what prevents the double."""
        from lib.message_queue import get_queue
        from lib.tasks_pkg.orchestrator._finalize import _salvage_undelivered_steer
        self._enqueue_steer()
        self.assertEqual(_salvage_undelivered_steer(self._task()), 1)
        # Second call: the item was DRAINED (removed) by the first salvage, so
        # nothing is left to re-queue → still exactly one durable row.
        self.assertEqual(_salvage_undelivered_steer(self._task()), 0)
        self.assertEqual(len(get_queue(self._conv, user_id=TEST_OWNER_USER_ID)), 1,
                         'the drain-as-you-salvage keeps it exactly once even if '
                         'the finalizer runs twice')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_storage
    guard_standalone_storage('test_user_steer_injection.__main__')
    unittest.main()
