#!/usr/bin/env python3
"""tests/test_recovery_merge_guards.py — startup recovery must NEVER stitch one
task's data into another turn's message.

FORENSICS (2026-07-26, conv ms1auj3n2cxs87, PostgreSQL — JOURNAL 续49)
---------------------------------------------------------------------
After a server restart the conv's last turn rendered as rows of EMPTY tool
cards. Root cause chain:

  1. task 9b38f0ec finalized as server_offline into msg#7 (43 live rounds).
  2. A PEER message landed as msg#8 (user) — the tail stopped being the
     crashed task's assistant bubble.
  3. The peer message spawned LIVE task ea441582.
  4. Boot recovery's "tail is user → append new assistant" branch
     (manager/_recovery.py) had NO guard: it appended a shell carrying the
     OLD task's segments-rebuilt toolRounds (41 rounds, wire-replay shape —
     no query/results/roundNum). The live task ADOPTED that shell as its own
     bubble and wrote its real output into it; the stale 41 rounds rendered
     as 41 empty cards until the live task's finalize overwrote them
     (_TERMINAL_OWNED_FIELDS, manager/_sync.py).

Had the live task failed/been aborted instead, the shell would have stayed
PERMANENTLY — old _taskId, display-less rounds, no overwriter.

TWO GATES (epic pt_311cbd7a31ad4391):
  G1 _merge_home_index — if the task already has a message home (a msg with
     its _taskId) anywhere but the tail, skip the merge entirely.
  G2 _conv_has_live_task_for_recovery — if the conv has a LIVE task
     (in-memory registry, or a task_results row still 'running' after the
     step-1 stale sweep), skip the merge entirely.

DISPLAY PROJECTION (epic pt_9409bf7133c049cb ②):
  _tool_rounds_from_task_row now projects roundNum/query/results onto
  rebuilt rounds (query via tool_round_label — the same builder the live
  pipeline uses; results as a single entry marked recovered:true — live
  per-tool results cannot be faithfully rebuilt from persisted segments,
  the material does not exist).

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/recov_guards.db \
        python3 tests/test_recovery_merge_guards.py
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
os.environ.setdefault('TOFU_DB_PATH', '/tmp/recov_guards_unittest.db')

import pytest

pytestmark = pytest.mark.unit


# ── Seed helpers (same shape as tests/test_recovery_toolrounds_from_segments) ──

def _rounds_live(n=3):
    """Live-fidelity rounds (what a real task writes into messages)."""
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
        'id': conv_id, 'user_id': 1, 'title': 'recov-guards',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'settings': json_dumps_pg(settings),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'settings', 'created_at', 'updated_at'], retry=True)
    db.commit()


def _seed_task(task_id, conv_id, *, content, thinking, rounds, status='running'):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import TASK_RESULTS, upsert
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
        'created_at': now_ms, 'completed_at': now_ms,
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


class TestRecoveryMergeGuards(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        uid = str(id(self))
        self.conv_id = 'cv-guard-' + uid
        self.task_id = 'tk-guard-' + uid
        _cleanup(self.conv_id, self.task_id)

    def tearDown(self):
        _cleanup(self.conv_id, self.task_id)

    # ── G1: task already has a home MID-HISTORY → no append, home untouched ──
    def test_taskid_home_mid_history_blocks_merge(self):
        """The ms1auj3n shape: crashed task's bubble sits at idx 1 with its
        _taskId; a newer user message is the tail. Recovery must NOT append
        a shell after it (the stitch), and must NOT touch the home."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        home_rounds = _rounds_live(3)
        messages = [
            {'role': 'user', 'content': 'task one', 'timestamp': 1},
            {'role': 'assistant', 'content': '', 'thinking': '',
             'toolRounds': home_rounds, '_taskId': self.task_id,
             'finishReason': 'server_offline', 'timestamp': 2},
            {'role': 'user', 'content': 'a peer message landed', 'timestamp': 3},
        ]
        _seed_conv(self.conv_id, messages, active_task_id=self.task_id)
        _seed_task(self.task_id, self.conv_id,
                   content='partial', thinking='reasoning',
                   rounds=_rounds_live(2))

        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 3,
                         'recovery appended a shell AFTER the newer tail — the '
                         'cross-turn stitch (ms1auj3n msg#9). G1 must block it.')
        home = out[1]
        self.assertEqual(home.get('toolRounds'), home_rounds,
                         'the task home was modified by the recovery merge')
        self.assertEqual(home.get('finishReason'), 'server_offline')

    # ── G1 negative: home IS the tail → the normal merge still runs ──
    def test_taskid_home_at_tail_still_merges(self):
        """Behaviour preservation: the crash-interrupted bubble at the tail
        (carrying the task's _taskId) is exactly what recovery is FOR — its
        content/thinking still get merged."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [
            {'role': 'user', 'content': 'task one', 'timestamp': 1},
            {'role': 'assistant', 'content': '', 'thinking': '',
             'toolRounds': [], '_taskId': self.task_id, 'timestamp': 2},
        ]
        _seed_conv(self.conv_id, messages, active_task_id=self.task_id)
        _seed_task(self.task_id, self.conv_id,
                   content='Partial answer recovered.', thinking='r',
                   rounds=_rounds_live(2))

        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 2, 'no append expected when home is the tail')
        am = out[-1]
        self.assertEqual(am.get('content'), 'Partial answer recovered.')
        self.assertEqual(am.get('finishReason'), 'interrupted')
        self.assertTrue(am.get('toolRounds'),
                        'tail-home merge must still restore rounds from segments')

    # ── G2: a LIVE task (in-memory registry) blocks the merge ──
    def test_live_task_in_registry_blocks_merge(self):
        """No home anywhere, tail is user — the legitimate-append shape, EXCEPT
        a live task owns the conv right now (the ea441582 case). The appended
        shell would be adopted as that task's bubble → must not happen."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
        from lib.tasks_pkg.manager import tasks, tasks_lock

        messages = [
            {'role': 'user', 'content': 'task one', 'timestamp': 1},
            {'role': 'user', 'content': 'peer message', 'timestamp': 2},
        ]
        _seed_conv(self.conv_id, messages, active_task_id=self.task_id)
        _seed_task(self.task_id, self.conv_id,
                   content='partial', thinking='r', rounds=_rounds_live(2))

        fake_live = {'convId': self.conv_id, 'status': 'running', 'id': 'live-1'}
        with tasks_lock:
            tasks['live-1'] = fake_live
        try:
            recover_stale_tasks_on_startup()
        finally:
            with tasks_lock:
                tasks.pop('live-1', None)

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 2,
                         'recovery appended a shell while a LIVE task owns the '
                         'conv — the shell gets adopted as the live bubble. '
                         'G2 must block the merge.')

    # ── G2 negative: no live task, no home → the legitimate append survives ──
    def test_no_home_no_live_task_appends_shell(self):
        """The shape G1/G2 must NOT break: crash BEFORE the assistant bubble
        was ever appended (tail=user, no _taskId home, no live task). The
        shell append is the only way the interrupted turn survives."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [
            {'role': 'user', 'content': 'task one', 'timestamp': 1},
            {'role': 'user', 'content': 'second user msg', 'timestamp': 2},
        ]
        _seed_conv(self.conv_id, messages, active_task_id=self.task_id)
        _seed_task(self.task_id, self.conv_id,
                   content='partial', thinking='r', rounds=_rounds_live(2))

        recover_stale_tasks_on_startup()

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 3,
                         'legitimate shell append (crash-before-append) was '
                         'blocked — G1/G2 over-fire')
        self.assertEqual(out[-1].get('role'), 'assistant')
        self.assertEqual(out[-1].get('finishReason'), 'interrupted')

    # ── NEUTER G1: delete the home check → the stitch comes back ──
    def test_neuter_home_gate_restores_stitch(self):
        """Patch _merge_home_index to "no home" (the pre-fix world) against the
        G1 fixture: the shell append MUST reappear — proving the gate is
        load-bearing, not decorative."""
        import lib.tasks_pkg.manager._recovery as rec
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        messages = [
            {'role': 'user', 'content': 'task one', 'timestamp': 1},
            {'role': 'assistant', 'content': '', 'thinking': '',
             'toolRounds': _rounds_live(3), '_taskId': self.task_id,
             'finishReason': 'server_offline', 'timestamp': 2},
            {'role': 'user', 'content': 'a peer message landed', 'timestamp': 3},
        ]
        _seed_conv(self.conv_id, messages, active_task_id=self.task_id)
        _seed_task(self.task_id, self.conv_id,
                   content='partial', thinking='r', rounds=_rounds_live(2))

        orig = rec._merge_home_index
        rec._merge_home_index = lambda msgs, tid: None   # NEUTER
        try:
            recover_stale_tasks_on_startup()
        finally:
            rec._merge_home_index = orig

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 4,
                         'NEUTER failed: with the home gate removed the stitch '
                         '(shell append) did NOT reappear — the gate is not '
                         'load-bearing')

    # ── NEUTER G2: live-check forced False → shell appended despite live task ──
    def test_neuter_live_gate_restores_stitch(self):
        import lib.tasks_pkg.manager._recovery as rec
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
        from lib.tasks_pkg.manager import tasks, tasks_lock

        messages = [
            {'role': 'user', 'content': 'task one', 'timestamp': 1},
            {'role': 'user', 'content': 'peer message', 'timestamp': 2},
        ]
        _seed_conv(self.conv_id, messages, active_task_id=self.task_id)
        _seed_task(self.task_id, self.conv_id,
                   content='partial', thinking='r', rounds=_rounds_live(2))

        with tasks_lock:
            tasks['live-1'] = {'convId': self.conv_id, 'status': 'running', 'id': 'live-1'}
        orig = rec._conv_has_live_task_for_recovery
        rec._conv_has_live_task_for_recovery = lambda cid, db: False   # NEUTER
        try:
            recover_stale_tasks_on_startup()
        finally:
            rec._conv_has_live_task_for_recovery = orig
            with tasks_lock:
                tasks.pop('live-1', None)

        _, out = _read_conv(self.conv_id)
        self.assertEqual(len(out), 3,
                         'NEUTER failed: with the live gate forced off, the '
                         'shell append did NOT reappear — the gate is not '
                         'load-bearing')


class TestLiveTaskProbe(unittest.TestCase):
    """_conv_has_live_task_for_recovery — probe semantics in isolation."""

    def setUp(self):
        from lib.database import init_db
        init_db()
        uid = str(id(self))
        self.conv_id = 'cv-probe-' + uid
        self.task_id = 'tk-probe-' + uid
        _cleanup(self.conv_id, self.task_id)

    def tearDown(self):
        _cleanup(self.conv_id, self.task_id)

    def test_db_probe_running_row_means_live(self):
        """A task_results row still 'running' for the conv ⇒ live. Once it is
        NOT running (the step-1 stale sweep marks pre-boot rows interrupted),
        the probe goes quiet."""
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.tasks_pkg.manager._recovery import _conv_has_live_task_for_recovery

        _seed_task(self.task_id, self.conv_id, content='x', thinking='',
                   rounds=_rounds_live(1), status='running')
        db = get_thread_db(DOMAIN_CHAT)
        self.assertTrue(_conv_has_live_task_for_recovery(self.conv_id, db))

        db.execute("UPDATE task_results SET status='interrupted' WHERE task_id=?",
                   (self.task_id,))
        db.commit()
        self.assertFalse(_conv_has_live_task_for_recovery(self.conv_id, db))

    def test_fail_closed_when_both_probes_dead(self):
        """If BOTH probes raise, the merge must be skipped (fail closed) — a
        stale bubble is cheap, a stitched one is not."""
        import lib.tasks_pkg.manager._recovery as rec

        class _DeadDB:
            def execute(self, *a, **k):
                raise RuntimeError('db dead')

        # Break the registry probe by breaking the manager import target.
        import lib.tasks_pkg.manager as mgr
        orig_tasks = mgr.tasks
        class _DeadTasks(dict):
            def values(self):
                raise RuntimeError('registry dead')
        mgr.tasks = _DeadTasks()
        try:
            self.assertTrue(rec._conv_has_live_task_for_recovery(self.conv_id, _DeadDB()),
                            'both probes dead must fail CLOSED (skip the merge)')
        finally:
            mgr.tasks = orig_tasks


class TestDisplayProjection(unittest.TestCase):
    """B②: rebuilt rounds must carry roundNum/query/results before persisting."""

    def test_rebuild_carries_display_projection(self):
        from lib.tasks_pkg.manager._recovery import _tool_rounds_from_task_row
        from lib.tasks_pkg.tool_display import tool_round_label

        rounds = [
            {'roundNum': 1, 'llmRound': 0, 'toolName': 'read_files',
             'toolCallId': 'tc_1', 'status': 'done',
             'toolContent': 'file body here', 'toolArgs': '{"path": "foo.py"}'},
        ]
        task_row = {'tool_rounds': None,
                    'segments': json.dumps(_thin_segments_for(rounds, 'c', 't'))}
        out = _tool_rounds_from_task_row(task_row)
        self.assertEqual(len(out), 1)
        r = out[0]
        self.assertEqual(r.get('roundNum'), 1)
        self.assertEqual(r.get('query'), tool_round_label('read_files', {'path': 'foo.py'}),
                         'query must come from the SAME builder the live pipeline uses')
        self.assertTrue(r['query'], 'query empty — the empty-card bug is back')
        res = r.get('results')
        self.assertIsInstance(res, list) and self.assertTrue(res,
                                                             'results missing — frontend reads results[0] for badge/snippet')
        self.assertTrue(res[0].get('recovered'),
                        'synthetic recovery results must be marked recovered:true '
                        '(honesty: not live-fidelity data)')
        self.assertIn('file body here', res[0].get('snippet', ''))

    def test_projection_idempotent_for_live_rounds(self):
        """Source 1 (tool_rounds column) rounds already carry live display
        fields — projection must be a no-op, never a downgrade."""
        from lib.tasks_pkg.manager._recovery import _tool_rounds_from_task_row

        live = _rounds_live(2)
        task_row = {'tool_rounds': json.dumps(live), 'segments': None}
        out = _tool_rounds_from_task_row(task_row)
        self.assertEqual(out, live,
                         'live-fidelity rounds were mutated by the projection')

    def test_neuter_without_projection_rounds_are_displayless(self):
        """NEUTER: identity-patch the projection → the rebuilt rounds lose
        query/results again (the pre-fix empty-card shape). Proves the
        projection is load-bearing."""
        import lib.tasks_pkg.manager._recovery as rec

        rounds = [
            {'roundNum': 1, 'llmRound': 0, 'toolName': 'read_files',
             'toolCallId': 'tc_1', 'status': 'done',
             'toolContent': 'file body here', 'toolArgs': '{"path": "foo.py"}'},
        ]
        task_row = {'tool_rounds': None,
                    'segments': json.dumps(_thin_segments_for(rounds, 'c', 't'))}
        orig = rec._project_display_fields
        rec._project_display_fields = lambda rs: rs   # NEUTER
        try:
            out = rec._tool_rounds_from_task_row(task_row)
        finally:
            rec._project_display_fields = orig
        self.assertEqual(len(out), 1)
        self.assertFalse(out[0].get('query'),
                         'NEUTER failed: rounds still have query without the '
                         'projection — it is not load-bearing')
        self.assertFalse(out[0].get('results'),
                         'NEUTER failed: rounds still have results without the '
                         'projection — it is not load-bearing')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_recovery_merge_guards.__main__', init_schema=False)
    unittest.main(verbosity=2)
