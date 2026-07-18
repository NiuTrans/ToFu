#!/usr/bin/env python3
"""tests/test_abort_dangling_tool_round.py — dangling tool-round finalize on abort.

ROOT-CAUSE fix verification for the "Stop while run_command shows Running… and
it stays Running… forever" bug.

The abort short-circuits in ``_execute_tool_one`` (lib/tasks_pkg/executor.py
top) and the streaming executor return WITHOUT calling ``_finalize_tool_round``,
so a tool round announced via ``tool_start`` (``status='searching'``) but never
executed keeps ``status='searching'`` with empty ``results``. The frontend
renders that as a permanent "Running…" spinner — live AND after reload, because
the stale round is persisted verbatim into ``conversations.messages``.

The fix is a single backend sweep at task termination,
``orchestrator._finalize_dangling_tool_rounds(task)``, invoked from
``_finalize_and_emit_done`` right after ``finishReason`` is set. It walks
``task['toolRounds']`` and finalizes every dangling round to ``status='aborted'``
+ emits a terminal ``tool_result``-class event, so the live stream and the
persisted DB state agree.

These tests drive the REAL production path:
  * a real ``conversations`` row + ``create_task`` (real task dict, registered
    as the conv's latest task so the persist freshness-guard passes),
  * a real ``run_command`` sleep subprocess started on a worker thread and
    aborted mid-flight via the same ``task['aborted']`` cooperative-abort
    signal the Stop button sets,
  * the REAL ``_execute_tool_one`` abort short-circuit (which reproduces the
    bug: leaves the round 'searching'),
  * the REAL terminal sweep ``_finalize_dangling_tool_rounds``,
  * the REAL ``persist_task_result`` → ``_sync_result_to_conversation`` write
    into ``conversations.messages``.

We then assert NO round is left 'searching' in the PERSISTED conversation.

Revert-proofing: neuter the sweep (monkeypatch it to a no-op) and the same
assertion fails, reproducing the stuck-"Running…" state — proving the test
guards the real fix, not a mocked round dict.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/abort_dangling.db \
        python3 tests/test_abort_dangling_tool_round.py
or via pytest.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/abort_dangling_unittest.db')

import pytest

pytestmark = pytest.mark.unit


# ── DB helpers (mirror tests/test_swarm_snapshot_persist.py) ──────────

def _seed_conv(conv_id):
    """Insert a conversations row whose last message is a streaming assistant
    bubble — the shape the frontend persists while a turn is in-flight."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [
        {'role': 'user', 'content': 'run a long command', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'abort-dangling',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_persisted_tool_rounds(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row or not row[0]:
        return None
    msgs = json.loads(row[0]) if isinstance(row[0], str) else row[0]
    for m in reversed(msgs):
        if m.get('role') == 'assistant':
            return m.get('toolRounds') or []
    return None


def _cleanup(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


def _announce_searching_round(task, command):
    """Append a run_command tool round in the EXACT shape the production
    streaming executor / parse_tool_calls emits via ``tool_start`` — i.e.
    ``status='searching'`` with no results yet."""
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    fn_args = {'command': command, 'description': 'long sleep'}
    tc_id = 'tc_sleep_1'
    args_str = json.dumps(fn_args)
    _, round_entry, _ = _build_tool_round_entry(
        'run_command', fn_args, tc_id, args_str,
        tool_round_num=0, project_enabled=True,
        conv_id=task.get('convId') or task.get('id'),
    )
    task['toolRounds'].append(round_entry)
    return round_entry, fn_args, tc_id


class TestAbortDanglingToolRound(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-abort-' + str(id(self))
        _cleanup(self.conv_id)
        _seed_conv(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    # ── helper: build a real task for this conv ──
    def _make_task(self, command):
        from lib.tasks_pkg.manager import create_task
        task = create_task(
            self.conv_id,
            [{'role': 'user', 'content': 'run a long command'}],
            {'model': 'test-model', 'projectEnabled': True},
        )
        # Give the assistant turn some content so the persist path actually
        # writes (it short-circuits when content+thinking+error are all empty).
        task['content'] = 'Running the command…'
        round_entry, fn_args, tc_id = _announce_searching_round(task, command)
        return task, round_entry, fn_args, tc_id

    def test_real_run_command_abort_leaves_no_searching_round(self):
        """Full production path: start a real `sleep` via run_command on a
        worker thread, abort mid-flight, run the terminal sweep + persist, and
        assert the PERSISTED conversation has no 'searching' tool round."""
        from lib.tasks_pkg.orchestrator import _finalize_dangling_tool_rounds
        from lib.tasks_pkg.executor import _execute_tool_one
        from lib.tasks_pkg.manager import persist_task_result

        task, round_entry, fn_args, tc_id = self._make_task('sleep 30')

        # Run the REAL run_command path on a worker thread (the real subprocess
        # path requires a project; we pass cwd as project_path). It blocks in
        # the sleep until the cooperative-abort poll kills the subprocess.
        result = {}

        def _worker():
            result['ret'] = _execute_tool_one(
                task, {'id': tc_id, 'function': {'name': 'run_command'}},
                'run_command', tc_id, fn_args,
                round_entry['roundNum'], round_entry,
                task['config'], os.getcwd(), True,
            )

        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        # Let the subprocess actually start, then hit Stop.
        time.sleep(1.0)
        self.assertEqual(round_entry['status'], 'searching',
                         'round should still be searching mid-flight')
        task['aborted'] = True
        th.join(timeout=15)
        self.assertFalse(th.is_alive(), 'run_command worker did not stop on abort')

        # The cooperative-abort run_command path returns killed output and
        # finalizes the round itself; but the executor abort short-circuit (and
        # cancelled streaming futures) is the path that leaves a round dangling.
        # Either way the terminal sweep is the single source of truth — run it.
        task['finishReason'] = 'aborted'
        task['status'] = 'done'
        _finalize_dangling_tool_rounds(task)

        # In-memory: no searching round survives the sweep.
        self.assertFalse(
            any(r.get('status') == 'searching' for r in task['toolRounds']),
            'a tool round was left searching in the live task after the sweep')

        # Persist via the REAL production path and assert against the DB.
        persist_task_result(task)
        persisted = _read_persisted_tool_rounds(self.conv_id)
        self.assertIsNotNone(persisted, 'conversation toolRounds not persisted')
        self.assertTrue(persisted, 'expected at least one persisted tool round')
        stuck = [r for r in persisted if r.get('status') == 'searching']
        self.assertEqual(stuck, [],
                         f'PERSISTED conversation still has searching round(s): {stuck}')

    def test_executor_abort_shortcircuit_then_sweep(self):
        """Reproduce the executor abort short-circuit directly: with
        task['aborted'] already set, the REAL _execute_tool_one returns
        'Task aborted by user.' WITHOUT finalizing → round stays 'searching'.
        The terminal sweep then finalizes it; the persisted conv is clean."""
        from lib.tasks_pkg.orchestrator import _finalize_dangling_tool_rounds
        from lib.tasks_pkg.executor import _execute_tool_one
        from lib.tasks_pkg.manager import persist_task_result

        task, round_entry, fn_args, tc_id = self._make_task('sleep 30')

        # Pre-abort: the executor's top-of-function guard fires immediately.
        task['aborted'] = True
        ret = _execute_tool_one(
            task, {'id': tc_id, 'function': {'name': 'run_command'}},
            'run_command', tc_id, fn_args,
            round_entry['roundNum'], round_entry,
            task['config'], os.getcwd(), True,
        )
        # Confirm we reproduced the BUG: the short-circuit left it searching.
        self.assertIn('aborted', (ret[1] or '').lower())
        self.assertEqual(round_entry['status'], 'searching',
                         'precondition: executor abort short-circuit must leave '
                         'the round searching (the bug being fixed)')

        # The fix: terminal sweep finalizes the dangling round.
        task['finishReason'] = 'aborted'
        task['status'] = 'done'
        n = _finalize_dangling_tool_rounds(task)
        self.assertGreaterEqual(n, 1, 'sweep should finalize the dangling round')
        self.assertEqual(round_entry['status'], 'aborted')

        persist_task_result(task)
        persisted = _read_persisted_tool_rounds(self.conv_id)
        stuck = [r for r in persisted if r.get('status') == 'searching']
        self.assertEqual(stuck, [],
                         f'PERSISTED conversation still has searching round(s): {stuck}')

    def test_stream_state_snapshot_carries_finalized_tool_rounds(self):
        """End-to-end backend-authored snapshot: after a task with a dangling
        round is swept + persisted to task_results, hitting the REAL SSE
        endpoint /api/chat/stream/<id> (DB-served path, task gone from memory)
        emits a typed `state` event whose toolRounds carry the FINALIZED status
        — NOT 'searching'. This is what lets the frontend render cold without
        recomputing state (the unification: backend is the source of truth)."""
        import asyncio
        import importlib.util

        from lib import auth_mode as _auth_mode
        from lib.database import (DOMAIN_CHAT, db_execute_with_retry,
                                  get_thread_db, json_dumps_pg)
        from lib.database._core_schema import TASK_RESULTS, upsert
        from lib.tasks_pkg.orchestrator import _finalize_dangling_tool_rounds

        # Build a task with a dangling searching round, sweep it.
        task, round_entry, _, _ = self._make_task('sleep 30')
        task['aborted'] = True
        task['finishReason'] = 'aborted'
        task['status'] = 'done'
        _finalize_dangling_tool_rounds(task)
        self.assertEqual(round_entry['status'], 'aborted')

        # Persist a task_results row directly (the DB-served stream path reads
        # task_results when the task is gone from the in-memory registry). This
        # is an INLINE-style row so the tool_rounds blob is stored on it.
        tr = task['toolRounds']
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        tid = task['id']
        upsert(db, TASK_RESULTS, {
            'task_id': tid, 'conv_id': self.conv_id,
            'content': task['content'], 'thinking': '', 'error': None,
            'status': 'done', 'tool_rounds': json_dumps_pg(tr),
            'metadata': json_dumps_pg({'finishReason': 'aborted'}),
            'created_at': now_ms, 'completed_at': now_ms,
        }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                        'status', 'tool_rounds', 'metadata', 'created_at',
                        'completed_at'], retry=True)
        db.commit()

        _prev = os.environ.pop('TOFU_AUTH_MODE', None)
        _auth_mode.reset_for_tests()
        _auth_mode.set_mode('open', set_by='stream-state-test')
        spec = importlib.util.spec_from_file_location(
            'server', os.path.join(os.path.dirname(os.path.dirname(__file__)),
                                    'server.py'))
        mod = importlib.util.module_from_spec(spec)
        mod.__name__ = 'server'
        spec.loader.exec_module(mod)
        app = mod.app

        captured = {}

        async def _t():
            async with app.test_client() as client:
                r = await client.get(f'/api/chat/stream/{tid}')
                body = (await r.get_data()).decode('utf-8', errors='replace')
                captured['body'] = body

        try:
            asyncio.run(_t())
        finally:
            _auth_mode.reset_for_tests()
            if _prev is not None:
                os.environ['TOFU_AUTH_MODE'] = _prev
            else:
                os.environ['TOFU_AUTH_MODE'] = 'private'
            _auth_mode.reset_for_tests()
            db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (tid,))
            db.commit()

        # Parse SSE frames; find the `state` event.
        state_ev = None
        for line in captured.get('body', '').splitlines():
            line = line.strip()
            if not line.startswith('data:'):
                continue
            payload = line[len('data:'):].strip()
            if not payload:
                continue
            try:
                ev = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if ev.get('type') == 'state':
                state_ev = ev
                break

        self.assertIsNotNone(state_ev,
                             'SSE stream did not emit a state snapshot '
                             f'(body={captured.get("body","")[:300]!r})')
        rounds = state_ev.get('toolRounds') or []
        self.assertTrue(rounds, 'state snapshot carried no toolRounds')
        stuck = [r for r in rounds if r.get('status') == 'searching']
        self.assertEqual(stuck, [],
                         'backend-authored state snapshot STILL has a searching '
                         f'round (would render Running… on reconnect): {stuck}')
        self.assertTrue(any(r.get('status') == 'aborted' for r in rounds),
                        'expected the finalized round to be status=aborted in '
                        'the snapshot')

    def test_sweep_preserves_completed_and_interactive_rounds(self):
        """The sweep must NOT touch rounds that are already terminal or are
        legitimately waiting on an external actor (awaiting_human/stdin/etc)."""
        from lib.tasks_pkg.orchestrator import _finalize_dangling_tool_rounds
        task, _, _, _ = self._make_task('sleep 30')
        # Replace the lone searching round with a mix.
        task['toolRounds'] = [
            {'roundNum': 1, 'toolName': 'read_files', 'status': 'done',
             'results': [{'toolName': 'read_files'}]},
            {'roundNum': 2, 'toolName': 'ask_human', 'status': 'awaiting_human',
             'guidanceId': 'g1'},
            {'roundNum': 3, 'toolName': 'run_command', 'status': 'searching',
             'results': None, 'query': '$ sleep 30'},
        ]
        task['finishReason'] = 'aborted'
        n = _finalize_dangling_tool_rounds(task)
        self.assertEqual(n, 1, 'only the dangling searching round should be swept')
        by_rn = {r['roundNum']: r for r in task['toolRounds']}
        self.assertEqual(by_rn[1]['status'], 'done')          # untouched
        self.assertEqual(by_rn[2]['status'], 'awaiting_human')  # untouched
        self.assertEqual(by_rn[3]['status'], 'aborted')        # swept


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    # setUp() calls init_db() itself, so only force sqlite + assert here.
    guard_standalone_db('test_abort_dangling_tool_round.__main__', init_schema=False)
    unittest.main(verbosity=2)
