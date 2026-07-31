"""tests/test_peer_message_round_boundary.py — Pillar #6 peer-message
round-boundary FAST PATH + the zero-double-delivery invariant.

A peer message (``project_message`` / ``project_intervene``) is delivered via a
HYBRID two-lane mechanism, both upstream-gated by the SAME storm-guard +
short-id resolution in ``send_peer_message``:

  1. DURABLE source of truth = ``message_queue`` ``KIND_PEER_MSG`` row. Survives
     a crash; wakes an IDLE target via the brain idle-drain (a fresh turn).
  2. FAST-PATH accelerator = when the target has a drain-eligible LIVE task (an
     ordinary orchestrator turn — NOT endpoint / VU), ALSO enqueue an
     ``agent_inbox`` item, tagged with the durable row's ``queueId``. The
     orchestrator drains it at the next ROUND BOUNDARY (never mid-stream).

The LOAD-BEARING correctness bar is ZERO DOUBLE DELIVERY. The message must be
delivered exactly once even though it lives in two lanes. Two races, both
closed and both proven here with a byte-reverting NEUTER that makes the message
arrive TWICE:

  • FORWARD (inbox drains first): the orchestrator drain hook deletes the
    durable ``message_queue`` row by ``queueId`` (``remove_from_queue``) the
    moment it injects the inbox item — so ``dispatch_next_queued`` can never pop
    it later as a redundant fresh turn.
  • REVERSE (task ends first): ``dispatch_next_queued`` pops the durable row as
    a fresh turn BEFORE the next drain, and calls ``agent_inbox.consume_peer``
    to drop the now-redundant inbox twin so it isn't re-injected next round.

Run with::

    python -m pytest tests/test_peer_message_round_boundary.py -v
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import time
import unittest

import pytest

pytestmark = pytest.mark.unit


@contextlib.contextmanager
def _fake_live_task(conv_id, *, task_id='livetask00001x', endpoint=False,
                    vu=False, aborted=False, status='running'):
    """Register a fake task in the live registry for the duration of the block.

    Mirrors the shape ``_live_drain_eligible_task`` inspects: convId, status,
    aborted, config.endpointMode, _endpoint_managed, _vu_subtask.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock
    t = {
        'id': task_id, 'convId': conv_id, 'status': status,
        'aborted': aborted, 'config': {'model': 'm'}, 'toolRounds': [],
    }
    if endpoint:
        t['_endpoint_managed'] = True
    if vu:
        t['_vu_subtask'] = True
    with tasks_lock:
        tasks[task_id] = t
    try:
        yield t
    finally:
        with tasks_lock:
            tasks.pop(task_id, None)


@contextlib.contextmanager
def _stub_spawn_chain():
    """Stub the task-creation chain so a REAL dispatch_next_queued runs
    (dequeue + message append + peer de-dup) WITHOUT registering a live task
    (a registry zombie would make LATER tests' targets look busy) or starting
    an LLM thread. Mirrors the patch set the race tests use."""
    import unittest.mock as mock
    import lib.conversations as convs
    import lib.tasks_pkg as tp
    import lib.tasks_pkg.conv_message_builder as cmb
    patches = [
        mock.patch.object(cmb, 'build_api_messages_from_db',
                          lambda cid, cfg: [{'role': 'user', 'content': 'x'}]),
        mock.patch.object(tp, 'create_task',
                          lambda cid, msgs, cfg: {'id': 'faketask00001'}),
        mock.patch.object(tp, 'spawn_task', lambda task: None),
        mock.patch.object(convs, 'set_conversation_settings', lambda *a, **k: None),
        mock.patch.object(convs, 'notify_conv_changed', lambda *a, **k: None),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in reversed(patches):
            p.stop()


class PeerRoundBoundaryTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        from lib.database import reset_sqlite_for_tests
        cls._db_snapshot = reset_sqlite_for_tests(
            os.path.join(cls._tmp.name, 'tofu.db'))

        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        now = int(time.time() * 1000)

        def _mk(cid):
            db.execute(
                'INSERT INTO conversations '
                '(id, user_id, title, messages, created_at, updated_at, '
                ' settings, msg_count, search_text) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (cid, 1, 'T', '[]', now, now, '{}', 0, ''))

        _mk('targetconv0001')   # the recipient
        _mk('senderconv0001')   # the sender
        db.commit()
        cls._target = 'targetconv0001'
        cls._sender = 'senderconv0001'

    @classmethod
    def tearDownClass(cls):
        from lib.database import restore_db_state
        restore_db_state(getattr(cls, '_db_snapshot', None))
        cls._tmp.cleanup()

    def setUp(self):
        # Fresh rate window + empty queue + empty inbox for every test.
        import lib.conversations.project_peer as pp
        with pp._rate_lock:
            pp._peer_msg_history.clear()
        from lib.message_queue import clear_queue
        clear_queue(self._target)
        clear_queue(self._sender)
        from lib import agent_inbox
        agent_inbox.reset_for_test(self._target)
        agent_inbox.reset_for_test(self._sender)
        # Reset the target's persisted messages so the fresh-turn append is
        # detectable per test.
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db.execute('UPDATE conversations SET messages=?, msg_count=0 '
                   'WHERE id=?', ('[]', self._target))
        db.commit()

    # ── helpers ──────────────────────────────────────────────────────
    def _send(self, text='watch the parser epic', human=False):
        from lib.conversations.project_peer import send_peer_message
        return send_peer_message('/proj', self._sender, self._target, text,
                                 human=human)

    def _target_messages(self):
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute('SELECT messages FROM conversations WHERE id=?',
                         (self._target,)).fetchone()
        return json.loads(row['messages'] or '[]')

    # ═══════════════════ Fast-path GATING ═══════════════════

    def test_live_target_gets_both_durable_row_and_inbox_twin(self):
        from lib import agent_inbox
        from lib.message_queue import get_queue
        with _fake_live_task(self._target):
            res = self._send()
        self.assertTrue(res.get('ok'), res)
        qid = res.get('queueId')
        # Durable row present (source of truth) …
        q = get_queue(self._target)
        self.assertEqual(len(q), 1)
        self.assertEqual(q[0]['queueId'], qid)
        # … AND a fast-path inbox twin, tagged with the durable queueId.
        items = agent_inbox.drain(self._target)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['mode'], 'peer-msg')
        self.assertEqual(items[0]['queueId'], qid)
        self.assertEqual(items[0]['fromConv'], self._sender)
        self.assertIn('watch the parser epic', items[0]['value'])

    def test_idle_target_gets_no_twin_and_delivers_at_send_time(self):
        """EVENT-CHANNEL contract (2026-07-27): an idle target gets NO inbox
        twin (that is the live-target fast path) — the durable row is drained
        AT SEND TIME: appended as a fresh _peerMessage turn, queue consumed,
        no 30 s heartbeat wait."""
        from lib import agent_inbox
        from lib.message_queue import get_queue
        with _stub_spawn_chain():
            res = self._send()
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(agent_inbox.peek(self._target), 0,
                         'an idle target gets NO inbox twin')
        self.assertEqual(len(get_queue(self._target)), 0,
                         'the durable row is consumed at send time')
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 1,
                         'the peer note renders as a fresh turn at send time')

    def test_endpoint_target_now_gets_fast_path_twin(self):
        """Endpoint mode gained an iteration-boundary drain hook
        (drain_peer_messages_into), so it is now fast-path eligible — the twin
        IS enqueued (previously the endpoint loop had no round boundary so the
        message stranded in the queue until the whole task ended)."""
        from lib import agent_inbox
        with _fake_live_task(self._target, endpoint=True):
            res = self._send()
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(agent_inbox.peek(self._target), 1,
                         'endpoint task now drains peer at its iteration '
                         'boundary → the fast-path twin must be offered')

    def test_vu_subtask_target_now_gets_fast_path_twin(self):
        """A live VU sub-task carries the parent conv in _peer_drain_key, so the
        target conv is fast-path eligible and gets the twin."""
        from lib import agent_inbox
        from lib.tasks_pkg.manager import tasks, tasks_lock
        # A VU sub-task runs with convId='' and _peer_drain_key=<parent conv>.
        with _fake_live_task(self._target, vu=True) as t:
            with tasks_lock:
                t['convId'] = ''
                t['_peer_drain_key'] = self._target
            res = self._send()
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(agent_inbox.peek(self._target), 1,
                         'a live VU sub-task (matched via _peer_drain_key) makes '
                         'the parent conv fast-path eligible')

    def test_aborted_task_is_not_drain_eligible(self):
        """An aborted (winding-down) task gets NO twin (it runs no more round
        boundaries). The send-time idle drain treats the conv as IDLE —
        mirroring drain_idle_peer_messages' strand-closing fall-through — and
        delivers immediately."""
        from lib import agent_inbox
        from lib.message_queue import get_queue
        with _fake_live_task(self._target, aborted=True):
            with _stub_spawn_chain():
                res = self._send()
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(agent_inbox.peek(self._target), 0,
                         'no twin for an aborted task')
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 1,
                         'the send-time drain closes the aborted-task strand')
        self.assertEqual(len(get_queue(self._target)), 0)

    # ═══════════════════ FORWARD race (inbox drains first) ═══════════════════

    def _dispatch_no_spawn(self, monkey):
        """Run the REAL dispatch_next_queued with task creation/spawn stubbed
        so we exercise the real dequeue + de-dup + message-append WITHOUT
        starting a background thread. Returns the task_id (or None).
        """
        import lib.conversations as convs
        import lib.tasks_pkg as tp
        import lib.tasks_pkg.conv_message_builder as cmb
        from lib.message_queue import dispatch_next_queued
        monkey(cmb, 'build_api_messages_from_db',
               lambda cid, cfg: [{'role': 'user', 'content': 'x'}])
        monkey(tp, 'create_task', lambda cid, msgs, cfg: {'id': 'faketask00001'})
        monkey(tp, 'spawn_task', lambda task: None)
        monkey(convs, 'set_conversation_settings',
               lambda *a, **k: None)
        monkey(convs, 'notify_conv_changed', lambda *a, **k: None)
        return dispatch_next_queued(self._target)

    def test_forward_race_dedup_gives_single_delivery(self):
        """Inbox drains first: the drain hook calls the REAL
        dedup_peer_durable_rows helper to delete the durable row by queueId, so
        a later dispatch finds nothing to pop = ONE delivery."""
        with _fake_live_task(self._target):
            res = self._send()
        qid = res['queueId']

        # The orchestrator round-boundary drain hook: drain the inbox (delivery
        # #1 — injected into the live turn) then run the REAL forward de-dup
        # helper the hook uses (dedup_peer_durable_rows).
        from lib import agent_inbox
        from lib.message_queue import dedup_peer_durable_rows, get_queue
        drained = agent_inbox.drain(self._target)
        self.assertEqual(len(drained), 1)
        # Delete the durable rows the drained peer items point at — exactly what
        # the orchestrator hook does with the drained items' queueIds.
        dedup_peer_durable_rows(
            self._target, [it.get('queueId') for it in drained])

        # Durable row is gone → a task-end dispatch pops nothing → no 2nd turn.
        self.assertEqual(len(get_queue(self._target)), 0)
        tid = self._dispatch_no_spawn_ctx()
        self.assertIsNone(tid, 'no durable row left → nothing to dispatch')
        msgs = self._target_messages()
        framed = [m for m in msgs if m.get('_peerMessage')]
        self.assertEqual(len(framed), 0,
                         'forward de-dup removed the durable row, so no fresh '
                         'turn is appended — the single delivery was the inbox '
                         'injection')

    def test_NEUTER_forward_race_without_dedup_delivers_twice(self):
        """NEGATIVE CONTROL — neuter the REAL dedup_peer_durable_rows helper to a
        no-op. After the inbox injects the message (delivery #1) the durable row
        is still popped by dispatch_next_queued as a FRESH TURN (delivery #2) =
        double delivery. Proves the forward de-dup is load-bearing, not a
        tautology."""
        with _fake_live_task(self._target):
            res = self._send()

        from lib import agent_inbox
        from lib.message_queue import get_queue
        drained = agent_inbox.drain(self._target)         # delivery #1
        self.assertEqual(len(drained), 1)

        # NEUTER the real helper → the durable row is NOT deleted.
        import lib.message_queue as mq
        orig = mq.dedup_peer_durable_rows
        mq.dedup_peer_durable_rows = lambda *a, **k: 0
        try:
            mq.dedup_peer_durable_rows(
                self._target, [it.get('queueId') for it in drained])
            self.assertEqual(len(get_queue(self._target)), 1)
            # Task ends → dispatch pops the orphaned durable row → 2nd delivery.
            tid = self._dispatch_no_spawn_ctx()
        finally:
            mq.dedup_peer_durable_rows = orig
        self.assertTrue(tid, 'the orphaned durable row dispatched a 2nd time')
        msgs = self._target_messages()
        framed = [m for m in msgs if m.get('_peerMessage')]
        # The inbox already delivered the message in-turn (drain above); the
        # durable row now delivers it AGAIN as a fresh persisted turn. This 2nd
        # copy is the double delivery the forward de-dup prevents (positive test
        # asserts 0 here). Its PRESENCE is what the neuter demonstrates.
        self.assertEqual(len(framed), 1,
                         'DOUBLE DELIVERY: without the forward de-dup the durable '
                         'row re-delivers a message the inbox already injected')

    # ═══════════════════ REVERSE race (task ends first) ═══════════════════

    def test_reverse_race_dedup_drops_inbox_twin(self):
        """Task ends before the next drain: dispatch_next_queued pops the
        durable row (delivery #1 as a fresh turn) and calls consume_peer to
        drop the inbox twin so it can't inject a 2nd time."""
        from lib import agent_inbox
        with _fake_live_task(self._target):
            self._send()
        self.assertEqual(agent_inbox.peek(self._target), 1)  # twin present

        tid = self._dispatch_no_spawn_ctx()                  # delivery #1
        self.assertTrue(tid)
        # The twin was consumed by queueId → a later drain injects nothing.
        self.assertEqual(agent_inbox.peek(self._target), 0,
                         'reverse de-dup must drop the inbox twin when the '
                         'durable row is dispatched as a fresh turn')

    def test_NEUTER_reverse_race_without_consume_peer_leaves_twin(self):
        """NEGATIVE CONTROL — revert dispatch_next_queued's consume_peer call to
        a no-op. The durable row still dispatches a fresh turn (delivery #1) but
        the inbox twin SURVIVES → the next round-boundary drain injects it AGAIN
        (delivery #2). Proves the reverse de-dup is load-bearing."""
        from lib import agent_inbox
        with _fake_live_task(self._target):
            self._send()
        self.assertEqual(agent_inbox.peek(self._target), 1)

        import lib.agent_inbox as ai
        orig = ai.consume_peer
        ai.consume_peer = lambda *a, **k: 0   # NEUTER
        try:
            tid = self._dispatch_no_spawn_ctx()             # delivery #1
        finally:
            ai.consume_peer = orig
        self.assertTrue(tid)
        # Twin still pending → it WOULD inject a 2nd time next round.
        self.assertEqual(agent_inbox.peek(self._target), 1,
                         'without consume_peer the inbox twin survives = double '
                         'delivery on the next drain')

    # ═══════════════════ Symptom-A: idle peer-drain ═══════════════════
    # A KIND_PEER_MSG row that lands in an IDLE, non-board conversation is
    # drained by NOTHING in steady state (the workflow idle-drain filters on
    # KIND_WORKFLOW), so it sits in the queue widget forever — shown but never
    # rendered as a turn. drain_idle_peer_messages() (run on the brain 30s
    # heartbeat) closes that gap by draining one such row per idle conv via the
    # SAME dispatch_next_queued seam → a fresh .peer-msg-banner turn.

    def _drain_idle_peer_ctx(self):
        """Run the REAL drain_idle_peer_messages under the same task-spawn stubs
        the race tests use (it calls the real dispatch_next_queued, which we do
        not want to actually spawn an LLM thread)."""
        import unittest.mock as mock
        import lib.conversations as convs
        import lib.tasks_pkg as tp
        import lib.tasks_pkg.conv_message_builder as cmb
        from lib.message_queue import drain_idle_peer_messages
        patches = [
            mock.patch.object(cmb, 'build_api_messages_from_db',
                              lambda cid, cfg: [{'role': 'user', 'content': 'x'}]),
            mock.patch.object(tp, 'create_task',
                              lambda cid, msgs, cfg: {'id': 'faketask00001'}),
            mock.patch.object(tp, 'spawn_task', lambda task: None),
            mock.patch.object(convs, 'set_conversation_settings', lambda *a, **k: None),
            mock.patch.object(convs, 'notify_conv_changed', lambda *a, **k: None),
        ]
        for p in patches:
            p.start()
        try:
            return drain_idle_peer_messages()
        finally:
            for p in reversed(patches):
                p.stop()

    def test_idle_peer_row_is_drained_as_fresh_turn(self):
        """EVENT-CHANNEL contract: a peer message to an IDLE conv is drained
        AT SEND TIME — appended as a fresh _peerMessage turn, durable row
        consumed. The 30 s drain_idle_peer_messages pass then finds NOTHING
        to do (idempotent — never a double delivery)."""
        from lib.message_queue import get_queue
        with _stub_spawn_chain():
            res = self._send('confirm you are not touching the parser module')
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(len(get_queue(self._target)), 0,
                         'consumed at send time (no 30 s wait)')
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 1,
                         'the pending peer message renders as a fresh turn')
        self.assertEqual(framed[0].get('_fromConv'), self._sender)
        # The 30 s pass is now exactly a safety net with nothing left to catch.
        spawned = self._drain_idle_peer_ctx()
        self.assertEqual(spawned, [],
                         'the heartbeat pass has nothing left to drain (no double)')
        framed2 = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed2), 1, 'still exactly ONE delivery')

    def test_idle_drain_skips_live_task_conv(self):
        """A conv with a LIVE task is NOT force-drained (its fast-path inbox
        twin + completion hook own delivery there — draining would
        double-dispatch). The durable row stays for the completion hook."""
        from lib.message_queue import get_queue
        with _fake_live_task(self._target):
            res = self._send()
            self.assertTrue(res.get('ok'), res)
            spawned = self._drain_idle_peer_ctx()
        self.assertNotIn('faketask00001', spawned)
        self.assertEqual(len(get_queue(self._target)), 1,
                         'a live-task conv keeps its durable row for the '
                         'completion hook — idle-drain must not touch it')
        # And no fresh turn was force-appended.
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 0)

    def test_NEUTER_send_time_drain_disabled_heartbeat_catches(self):
        """NEGATIVE CONTROL, repointed to the event-channel seam: with the
        SEND-TIME drain disabled, a peer note into an idle conv stays a
        stranded queue row (the Symptom-A shape returns) — and the REAL 30 s
        drain_idle_peer_messages pass then catches it (the net still works)."""
        from lib.message_queue import get_queue
        import lib.message_queue as mq
        real_dispatch = mq.dispatch_next_queued
        mq.dispatch_next_queued = lambda *a, **k: None   # NEUTER the seam
        try:
            res = self._send()
        finally:
            mq.dispatch_next_queued = real_dispatch
        self.assertTrue(res.get('ok'), res)
        self.assertEqual(len(get_queue(self._target)), 1,
                         'with the send-time drain disabled the row is stranded')
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 0, 'nothing renders at send time')
        # The 30 s safety net still catches what the disabled seam missed.
        spawned = self._drain_idle_peer_ctx()
        self.assertTrue(spawned,
                        'the heartbeat pass delivers what the seam missed')
        framed2 = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed2), 1, 'delivered exactly once via the net')

    # ═══════════ Symptom-B: deferred delivery / never-zero ═══════════
    # The forward de-dup MUST NOT delete the durable row at inject time (when
    # the message is only in the in-memory `messages` list, not yet consumed by
    # the model). If the task aborts between inject and the LLM call, the inbox
    # twin is already drained and the in-memory message dies — deleting the
    # durable row then = ZERO delivery. The fix DEFERS the delete (and the
    # arrival chip) until AFTER the LLM call confirms consumption, so an abort
    # leaves the durable row intact for a later fresh-turn redelivery.

    def test_abort_before_flush_preserves_durable_row_never_zero(self):
        """NEW deferred ordering: at inject we drain the inbox (in-memory
        delivery) but DO NOT delete the durable row. Simulate a task abort
        before the post-LLM flush — the in-memory message is lost, but the
        durable row SURVIVES and a later dispatch redelivers it as a fresh turn.
        Net: the message is delivered EXACTLY ONCE, never zero."""
        from lib import agent_inbox
        from lib.message_queue import get_queue
        with _fake_live_task(self._target):
            res = self._send('are you editing styles.css? I am about to rewrite it')
        qid = res['queueId']

        # Drain hook (inject): the inbox item is drained into the in-memory
        # message list (delivery attempt #1) — but under the DEFERRED ordering
        # the durable row is NOT deleted here.
        drained = agent_inbox.drain(self._target)
        self.assertEqual(len(drained), 1)
        # ★ deferred: durable row still present after inject (the never-zero key)
        self.assertEqual(len(get_queue(self._target)), 1,
                         'durable row must survive inject (deferred de-dup) so an '
                         'abort before flush cannot zero-deliver')

        # Task ABORTS before the post-LLM flush → the in-memory message dies.
        # The durable row is now the ONLY surviving copy. A later dispatch
        # (idle-drain / completion hook) pops it → a fresh persisted turn.
        tid = self._dispatch_no_spawn_ctx()
        self.assertTrue(tid, 'the surviving durable row must redeliver')
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 1,
                         'NEVER ZERO: an abort before flush still delivers the '
                         'message exactly once via the surviving durable row')

    def test_flush_after_consume_dedups_durable_row_single_delivery(self):
        """After the LLM confirms consumption, the deferred flush deletes the
        durable row so a later dispatch finds nothing = single delivery (no
        double)."""
        from lib import agent_inbox
        from lib.message_queue import dedup_peer_durable_rows, get_queue
        with _fake_live_task(self._target):
            res = self._send()
        qid = res['queueId']

        drained = agent_inbox.drain(self._target)          # delivery (in-memory)
        self.assertEqual(len(drained), 1)
        # LLM call succeeded → the DEFERRED flush now deletes the durable row
        # (this is what the orchestrator does post-consume).
        dedup_peer_durable_rows(self._target,
                                [it.get('queueId') for it in drained])
        self.assertEqual(len(get_queue(self._target)), 0)
        tid = self._dispatch_no_spawn_ctx()
        self.assertIsNone(tid, 'flushed durable row → nothing to redispatch')
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 0,
                         'single delivery: the in-memory inject was the delivery; '
                         'the flushed durable row adds no second turn')

    def test_NEUTER_inject_time_delete_zero_delivers_on_abort(self):
        """NEGATIVE CONTROL — the OLD delete-then-hope ordering: delete the
        durable row AT INJECT time (before consumption). Then an abort before
        the LLM call loses the in-memory message too → the message renders
        NOWHERE = ZERO delivery (the reported Symptom B). Proves the deferral is
        load-bearing."""
        from lib import agent_inbox
        from lib.message_queue import dedup_peer_durable_rows, get_queue
        with _fake_live_task(self._target):
            res = self._send()

        drained = agent_inbox.drain(self._target)          # in-memory only
        self.assertEqual(len(drained), 1)
        # OLD BEHAVIOUR (neuter): delete the durable row immediately at inject.
        dedup_peer_durable_rows(self._target,
                                [it.get('queueId') for it in drained])
        self.assertEqual(len(get_queue(self._target)), 0)

        # Task ABORTS before consuming the in-memory message → it is lost. With
        # the durable row already deleted, nothing remains to redeliver.
        tid = self._dispatch_no_spawn_ctx()
        self.assertIsNone(tid)
        framed = [m for m in self._target_messages() if m.get('_peerMessage')]
        self.assertEqual(len(framed), 0,
                         'ZERO DELIVERY: inject-time delete + abort loses the '
                         'message entirely — the bug the deferral fixes')

    def test_source_defers_dedup_and_chip_past_llm_call(self):
        """Source-ordering guard (byte-revert neuter): the orchestrator must
        STASH the peer items at inject and flush the de-dup only AFTER the LLM
        call confirms consumption. A revert to inline inject-time
        dedup_peer_durable_rows would place a de-dup CALL before the LLM unpack
        — this test fails on that."""
        import re

        # The orchestrator was split into a package (2026-06): the run-loop code
        # (peer-inject stash + deferred de-dup + the LLM unpack boundary) lives
        # in the ``_run`` submodule, NOT the package facade ``__init__.py``.
        # Reading ``orchestrator.__file__`` (the __init__) found none of the
        # tokens → this guard silently read the wrong file after the split.
        # Read the submodule that actually defines run_task's round loop.
        import lib.tasks_pkg.orchestrator._run as orch_run
        src = open(orch_run.__file__).read()
        # The deferral seam exists (exact token, not a substring of a rename).
        self.assertTrue(
            re.search(r"_peer_inject_pending\b", src),
            'peer items must be stashed under _peer_inject_pending for the '
            'deferred flush')

        # The LLM-result unpack is the "delivery confirmed" boundary.
        llm_pos = src.index("assistant_msg = llm_result[")
        # EVERY dedup_peer_durable_rows CALL (an occurrence followed by "(",
        # excluding the `import` line) must sit AFTER that boundary. A revert to
        # inject-time delete would put a call before it → caught here.
        call_positions = [m.start() for m in
                          re.finditer(r'dedup_peer_durable_rows\s*\(', src)]
        self.assertTrue(call_positions, 'the de-dup call must exist')
        self.assertTrue(
            all(pos > llm_pos for pos in call_positions),
            'durable-row de-dup must run ONLY AFTER the LLM call confirms the '
            'model consumed the message (no inject-time delete)')

    # ── shared dispatch-driver used by the race tests (context-managed
    #    monkeypatching without pytest fixtures, since this is unittest) ──
    def _dispatch_no_spawn_ctx(self):
        import unittest.mock as mock
        patches = []

        def _monkey(mod, name, val):
            p = mock.patch.object(mod, name, val)
            p.start()
            patches.append(p)
        try:
            return self._dispatch_no_spawn(_monkey)
        finally:
            for p in reversed(patches):
                p.stop()


if __name__ == '__main__':
    unittest.main()
