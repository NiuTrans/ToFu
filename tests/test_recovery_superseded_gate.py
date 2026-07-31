#!/usr/bin/env python3
"""tests/test_recovery_superseded_gate.py — startup recovery must NOT resurrect
a SUPERSEDED turn, and the shell it appends must carry durable identity.

FORENSICS (2026-07-27, conv ms2gipv5a7gvbc, PostgreSQL — production)
--------------------------------------------------------------------
The conversation rendered FOUR consecutive identical "Agent · 因重启中断"
bubbles at the tail. The DB held two persisted copies (#16 full, #17 lite
twin) and the frontend fanned them out to four rendered cards. Root chain:

  1. Autopilot spawned a premature VU twin (task 06b29421, created 08:07:43)
     while its parent task was still running. The parent completed
     (status='done', 08:08:51) and its reply landed as msg#1 — the twin's
     own turn was SUPERSEDED. The twin then hung and stayed status='running'
     for 3.5 hours (a zombie).
  2. At the 11:42 restart, ``recover_stale_tasks_on_startup`` picked the
     zombie as the conv's merge candidate — the "most recovered text"
     heuristic prefers its 4.6k-char checkpoint over the fresher frontier
     task — and APPENDED it as a brand-new tail bubble (tail was a VU user
     message). A turn answered 3.5h earlier was resurrected.
  3. The appended shell carried NO ``_msgId`` and NO ``_taskId``. The
     frontend mints a fresh client id on every independent fetch of an
     id-less message (``core.js _ensureMsgId``), so the windowed LITE slice
     and the full/debug fetch each produced a distinct identity for the same
     bubble; two fetches × two identities rendered four cards, and a PUT
     persisted two of them (#17 even kept its transient ``_trimmed``
     markers — the heavy-field refill could not match the minted id).

GATES (this epic):
  G3 ``_task_superseded_by_newer_reply`` — a stale task whose conv already
     has a NEWER completed reply (status='done', completed after the stale
     task started) is marked interrupted but NEVER merged: its turn is
     already answered. The merge candidate is the NEWEST non-superseded
     stale task (the crash frontier), not the meatiest checkpoint.
  G4 tail-owner guard — a tail assistant bubble explicitly owned by a
     DIFFERENT task never receives this task's checkpoint (the G1 stitch
     with the roles reversed).
  ID the appended shell is stamped ``_msgId`` (uuid4) + ``_taskId`` at birth,
     so the frontend never re-mints and a second recovery sweep finds the
     home via G1 (merge-in-place idempotence).

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/recov_superseded_gate.db \
        python3 tests/test_recovery_superseded_gate.py
or via pytest (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1).
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/recov_superseded_gate.db')

import pytest

pytestmark = pytest.mark.unit


# ── Seed helpers (same shape as tests/test_recovery_merge_guards.py) ─────

def _rounds_live(n=2):
    return [
        {'roundNum': i + 1, 'llmRound': i, 'toolName': 'read_files',
         'toolCallId': f'tc_{i}', 'status': 'done', 'toolContent': f'content {i}',
         'toolArgs': '{"path": "foo.py"}', 'query': 'foo.py',
         'results': [{'toolName': 'read_files', 'badge': 'read',
                      'title': 'foo.py', 'snippet': f'content {i}'}]}
        for i in range(n)
    ]


def _thin_segments_for(rounds, content, thinking):
    from lib.tasks_pkg.segments import assemble_segments, segments_to_json
    task = {'content': content, 'thinking': thinking, 'toolRounds': rounds}
    return segments_to_json(assemble_segments(task))


def _seed_conv(conv_id, messages, *, active_task_id=None):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    settings = {}
    if active_task_id is not None:
        settings['activeTaskId'] = active_task_id
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'recov-superseded',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'settings': json_dumps_pg(settings),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'settings', 'created_at', 'updated_at'], retry=True)
    db.commit()


def _seed_task(task_id, conv_id, *, content, thinking, rounds=None, status='running',
               created_at=None, completed_at=None):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import TASK_RESULTS, upsert
    rounds = _rounds_live(2) if rounds is None else rounds
    thin = _thin_segments_for(rounds, content, thinking)
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': task_id, 'conv_id': conv_id,
        'content': content, 'thinking': thinking, 'error': None,
        'status': status,
        'tool_rounds': None,
        'segments': json_dumps_pg(thin),
        'metadata': json_dumps_pg({'model': 'test-model'}),
        'created_at': created_at if created_at is not None else now_ms,
        'completed_at': completed_at if completed_at is not None else now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                    'status', 'tool_rounds', 'segments', 'metadata',
                    'created_at', 'completed_at'], retry=True)
    db.commit()


def _read_conv(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT settings, messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row:
        return None, None
    settings = json.loads(row[0] or '{}') if isinstance(row[0], str) else (row[0] or {})
    messages = json.loads(row[1] or '[]') if isinstance(row[1], str) else (row[1] or [])
    return settings, messages


def _task_status(task_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT status FROM task_results WHERE task_id=?', (task_id,)).fetchone()
    return row[0] if row else None


def _mark_running(task_id):
    """Self-heal against a neighbouring sweep (xdist / shared sqlite file):
    re-flip OUR zombie to 'running' immediately before OUR sweep runs."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    db.execute("UPDATE task_results SET status='running' WHERE task_id=?", (task_id,))
    db.commit()


def _cleanup(conv_id, *task_ids):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        for tid in task_ids:
            db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (tid,))
        db.commit()
    except Exception:
        pass


class TestSupersededGate(unittest.TestCase):
    """G3 — a stale task whose turn a NEWER completed reply already answered
    is marked interrupted but NEVER merged (the ms2gipv5 resurrection)."""

    def setUp(self):
        from lib.database import init_db
        init_db()
        uid = str(id(self))
        self.conv_id = 'cv-sup-' + uid
        self.zombie = 'tk-zombie-' + uid
        self.done = 'tk-done-' + uid
        _cleanup(self.conv_id, self.zombie, self.done)
        self.t0 = int(time.time() * 1000) - 3 * 3600 * 1000  # zombie "started" 3h ago

    def tearDown(self):
        _cleanup(self.conv_id, self.zombie, self.done)

    def _seed_superseded_shape(self):
        """The ms2gipv5 shape: the turn is LONG answered (msg#1 by T_done),
        the tail is the next VU user message, and the zombie twin of that
        OLD turn is still 'running'."""
        messages = [
            {'role': 'user', 'content': 'turn one', 'timestamp': 1},
            {'role': 'assistant', 'content': 'the answered reply', 'thinking': '',
             '_taskId': self.done, '_msgId': 'm-done', 'finishReason': 'stop',
             'timestamp': 2},
            {'role': 'user', 'content': '=== OBJECTIVE === follow-up', 'timestamp': 3,
             '_isVirtualUser': True},
        ]
        _seed_conv(self.conv_id, messages)
        _seed_task(self.zombie, self.conv_id, content='', thinking='zombie partial',
                   status='running', created_at=self.t0,
                   completed_at=self.t0 + 60_000)
        # The newer reply COMPLETED after the zombie started → superseded.
        _seed_task(self.done, self.conv_id, content='the answered reply', thinking='',
                   status='done', created_at=self.t0 - 10 * 60_000,
                   completed_at=self.t0 + 60_001)

    def test_superseded_zombie_is_not_resurrected(self):
        """G3 firing: zero new bubbles — the turn was already answered."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        self._seed_superseded_shape()
        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 3,
                         'recovery resurrected a SUPERSEDED zombie as a new tail '
                         'bubble — the ms2gipv5 four-bubble incident. G3 must '
                         'block the merge entirely.')
        self.assertEqual(out[1].get('content'), 'the answered reply',
                         'the answered bubble was modified by the recovery merge')
        self.assertEqual(_task_status(self.zombie), 'interrupted',
                         'the zombie row itself must still be settled (status flip)')

    def test_frontier_task_still_recovered(self):
        """G3 must NOT over-fire: nothing completed after the zombie started
        (the genuine crash-mid-current-turn) → the shell append survives."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [
            {'role': 'user', 'content': 'turn one', 'timestamp': 1},
            {'role': 'assistant', 'content': 'the answered reply', 'thinking': '',
             '_taskId': self.done, '_msgId': 'm-done', 'finishReason': 'stop',
             'timestamp': 2},
            {'role': 'user', 'content': 'frontier question', 'timestamp': 3},
        ]
        _seed_conv(self.conv_id, messages)
        # The previous turn completed BEFORE the zombie started → frontier.
        _seed_task(self.done, self.conv_id, content='the answered reply', thinking='',
                   status='done', created_at=self.t0 - 2 * 60_000,
                   completed_at=self.t0 - 60_000)
        _seed_task(self.zombie, self.conv_id, content='frontier partial',
                   thinking='frontier reasoning', status='running',
                   created_at=self.t0, completed_at=self.t0 + 60_000)

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 4,
                         'the crash-frontier recovery (the FEATURE) was blocked '
                         '— G3 over-fired')
        shell = out[-1]
        self.assertEqual(shell.get('role'), 'assistant')
        self.assertEqual(shell.get('content'), 'frontier partial')
        self.assertEqual(shell.get('finishReason'), 'interrupted')

    def test_appended_shell_carries_durable_identity(self):
        """ID gate: the shell is born with _msgId + _taskId, so the frontend
        never re-mints (no lite twin, no four-card fan-out) and G1 can find
        the home on the next sweep."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [{'role': 'user', 'content': 'frontier question', 'timestamp': 1}]
        _seed_conv(self.conv_id, messages)
        _seed_task(self.zombie, self.conv_id, content='partial', thinking='r',
                   status='running', created_at=self.t0,
                   completed_at=self.t0 + 60_000)

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 2)
        shell = out[-1]
        self.assertTrue(shell.get('_msgId'),
                        'appended shell has NO _msgId — the frontend will mint a '
                        'fresh one per fetch (the #17 lite-twin duplication)')
        self.assertEqual(shell.get('_taskId'), self.zombie,
                         'appended shell must carry its task id as the durable '
                         'home (G1 idempotence on the next sweep)')

    def test_second_recovery_merges_in_place(self):
        """ID gate consequence: a SECOND crash+recovery on the same task finds
        the stamped home and updates in place — never a second shell."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [{'role': 'user', 'content': 'frontier question', 'timestamp': 1}]
        _seed_conv(self.conv_id, messages)
        _seed_task(self.zombie, self.conv_id, content='partial', thinking='r1',
                   status='running', created_at=self.t0,
                   completed_at=self.t0 + 60_000)

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()
        _, out1 = _read_conv(self.conv_id)
        self.assertEqual(len(out1), 2)
        first_id = out1[-1].get('_msgId')

        # The task "crashed again" with a fresher checkpoint.
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import TASK_RESULTS, upsert
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, TASK_RESULTS, {
            'task_id': self.zombie, 'conv_id': self.conv_id,
            'content': 'partial longer', 'thinking': 'r1-and-then-some', 'error': None,
            'status': 'running', 'tool_rounds': None,
            'segments': json_dumps_pg(_thin_segments_for(_rounds_live(2),
                                                         'partial longer', 'r1-and-then-some')),
            'metadata': json_dumps_pg({'model': 'test-model'}),
            'created_at': self.t0, 'completed_at': self.t0 + 120_000,
        }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                        'status', 'tool_rounds', 'segments', 'metadata',
                        'created_at', 'completed_at'], retry=True)
        db.commit()

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out2 = _read_conv(self.conv_id)
        self.assertEqual(len(out2), 2,
                         'a second recovery APPENDED another shell — the home '
                             'was not findable (missing _taskId on the shell)')
        self.assertEqual(out2[-1].get('_msgId'), first_id,
                         'the shell identity changed across recoveries — the '
                         'frontend re-mint problem is back')
        self.assertEqual(out2[-1].get('content'), 'partial longer',
                         'home-at-tail merge did not refresh the checkpoint')

    def test_tail_owned_by_other_task_is_not_stitched(self):
        """G4: tail assistant explicitly owned by a DIFFERENT (done) task must
        not receive the stale task's checkpoint. Pre-fix this stitched the
        zombie's thinking INTO the good bubble."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [
            {'role': 'user', 'content': 'turn one', 'timestamp': 1},
            {'role': 'assistant', 'content': 'done answer', 'thinking': 'short',
             '_taskId': self.done, '_msgId': 'm-done', 'finishReason': 'stop',
             'timestamp': 2},
        ]
        _seed_conv(self.conv_id, messages)
        # Done BEFORE the zombie started → the zombie is NOT superseded (its
        # user turn is simply gone) — this isolates G4 from G3.
        _seed_task(self.done, self.conv_id, content='done answer', thinking='short',
                   status='done', created_at=self.t0 - 2 * 60_000,
                   completed_at=self.t0 - 60_000)
        _seed_task(self.zombie, self.conv_id,
                   content='a much longer zombie content than the bubble has',
                   thinking='zombie thinking that is definitely longer',
                   status='running', created_at=self.t0,
                   completed_at=self.t0 + 60_000)

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 2,
                         'recovery appended a shell after a completed assistant '
                         'tail — a reply to nothing')
        bubble = out[-1]
        self.assertEqual(bubble.get('content'), 'done answer',
                         'G4 failed: the done bubble was overwritten with the '
                         "zombie's checkpoint (cross-turn stitch)")
        self.assertEqual(bubble.get('thinking'), 'short',
                         'G4 failed: the done bubble thinking was stitched')

    def test_newer_error_turn_also_supersedes(self):
        """Widened G3 surface (owner-reviewed, evidence: 7-day error=112): an
        ERROR-settled newer turn is a settled turn — the stale zombie must
        not be resurrected after the error bubble."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        self._seed_superseded_shape()
        # Re-point the "done" task to error: the turn settled with an error
        # bubble AFTER the zombie started.
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db.execute("UPDATE task_results SET status='error' WHERE task_id=?", (self.done,))
        db.commit()

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 3,
                         'recovery resurrected a zombie whose turn settled with '
                         'an ERROR — an error bubble is a settled turn, G3 must '
                         'treat it as superseding')

    def test_newer_aborted_turn_also_supersedes(self):
        """Widened G3 surface: resurrecting content after a user-initiated
        ABORT would directly contradict the explicit stop."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        self._seed_superseded_shape()
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        db.execute("UPDATE task_results SET status='aborted' WHERE task_id=?", (self.done,))
        db.commit()

        _mark_running(self.zombie)
        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 3,
                         'recovery resurrected a zombie after an ABORTED turn — '
                         'that contradicts the user\u2019s explicit stop, G3 must '
                         'treat aborted as superseding')

    def test_neuter_superseded_gate_restores_resurrection(self):
        """NEUTER: force the G3 probe to 'not superseded' → the resurrection
        comes back, proving the gate is load-bearing."""
        import lib.tasks_pkg.manager._recovery as rec
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        self._seed_superseded_shape()
        orig = rec._task_superseded_by_newer_reply
        rec._task_superseded_by_newer_reply = lambda db, cid, created: False  # NEUTER
        try:
            _mark_running(self.zombie)
            recover_stale_tasks_on_startup()
        finally:
            rec._task_superseded_by_newer_reply = orig

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 4,
                         'NEUTER failed: with G3 forced off the superseded zombie '
                         'was NOT resurrected — the gate is not load-bearing')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_recovery_superseded_gate.__main__', init_schema=False)
    unittest.main(verbosity=2)
