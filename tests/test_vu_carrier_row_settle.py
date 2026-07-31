#!/usr/bin/env python3
"""tests/test_vu_carrier_row_settle.py — the VU carrier's task_results row must
reach a TERMINAL status when its synchronous run ends (pt_8a491f9d, the
ms2gipv5 zombie generator).

ROOT CAUSE (production forensics, 2026-07-27, task 06b29421)
------------------------------------------------------------
The autopilot VU sub-task (carrier) runs synchronously under
``_endpoint_managed=True``, which BY DESIGN suppresses the orchestrator's
terminal-status flip + ``persist_task_result``. Its per-round
``checkpoint_task_partial`` writes therefore leave the row at
``status='running'`` forever — the in-memory ``discard_task`` only cleans
the registry. The next startup recovery sweep collected that stale row as a
crash-interrupted turn and resurrected its checkpoint as a phantom tail
bubble (the four-bubble incident; resurrection itself is now gated by G3 in
``manager/_recovery.py``, but the ROW pollution is the generator).

FIX
---
1. ``manager._registry.write_carrier_terminal_row(task, status)`` — chassis
   helper mirroring ``_write_aborted_terminal_floor`` (idempotent,
   last-writer-wins via ``_upsert_task_row``).
2. ``autopilot.run_virtual_user``'s finally (the carrier's lifecycle owner)
   calls it right after ``discard_task``, deriving the status from the
   carrier's OWN end state: aborted → 'aborted'; error → 'error';
   finishReason set → 'done'; died before any finish reason → 'error'
   (never a fake 'done').
3. The parent task is stamped ``_vu_carrier_id`` at carrier creation, and
   the conv-sync freshness guard (``manager/_sync.py``) recognises
   "superseded by own VU carrier" as the DESIGNED HB-1 handoff (debug),
   not the "never aborted — Unexpected" WARNING that fired on EVERY
   autopilot turn (app.log:75363).

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/vu_carrier_settle.db \
        python3 tests/test_vu_carrier_row_settle.py
or via pytest (PYTEST_DISABLE_PLUGIN_AUTOLOAD=1).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/vu_carrier_settle.db')

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _carrier_task(task_id, conv_id, *, aborted=False, error=None,
                  status='running', finish_reason='stop'):
    return {
        'id': task_id, 'convId': conv_id, 'status': status,
        'content': 'simulated user reply', 'thinking': 'vu reasoning',
        'error': error, 'aborted': aborted,
        'finishReason': finish_reason, 'usage': None, 'toolRounds': [],
        'config': {'model': 'test-model'},
        '_vu_subtask': True, '_inline_messages': True,
        '_autopilotParent': 'parent-' + task_id[:4],
    }


def _seed_running_row(task_id, conv_id):
    """What checkpoint_task_partial leaves behind mid-run."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.database._core_schema import TASK_RESULTS, upsert
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': task_id, 'conv_id': conv_id,
        'content': 'partial', 'thinking': 'partial thinking', 'error': None,
        'status': 'running', 'tool_rounds': None, 'segments': None,
        'metadata': json.dumps({'model': 'test-model'}),
        'created_at': now_ms, 'completed_at': now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                    'status', 'tool_rounds', 'segments', 'metadata',
                    'created_at', 'completed_at'], retry=True)
    db.commit()


def _row_status(task_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT status, content, thinking FROM task_results WHERE task_id=?',
                     (task_id,)).fetchone()
    return (row['status'], row['content'], row['thinking']) if row else (None, None, None)


def _cleanup(*task_ids):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        for tid in task_ids:
            db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (tid,))
        db.commit()
    except Exception:
        pass


class TestCarrierTerminalRow(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        uid = str(id(self))
        self.tid = 'tk-vu-' + uid
        self.conv = 'cv-vu-' + uid
        _cleanup(self.tid)

    def tearDown(self):
        _cleanup(self.tid)

    def test_completed_carrier_row_settles_done(self):
        """The normal path: carrier finished (fr=stop) → row flips to done
        and keeps the carrier's content/thinking (the zombie's 4661-char
        thinking would have been a 'running' row forever before this)."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row

        _seed_running_row(self.tid, self.conv)
        self.assertEqual(_row_status(self.tid)[0], 'running')

        write_carrier_terminal_row(_carrier_task(self.tid, self.conv), 'done')

        status, content, thinking = _row_status(self.tid)
        self.assertEqual(status, 'done',
                         "carrier row stayed non-terminal — the zombie generator")
        self.assertEqual(content, 'simulated user reply')
        self.assertEqual(thinking, 'vu reasoning')

    def test_aborted_carrier_row_settles_aborted(self):
        """real_message_preempts_vu / parent_aborted → 'aborted', not 'done'."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row

        _seed_running_row(self.tid, self.conv)
        write_carrier_terminal_row(
            _carrier_task(self.tid, self.conv, aborted=True, finish_reason=None),
            'aborted')
        self.assertEqual(_row_status(self.tid)[0], 'aborted')

    def test_error_carrier_row_settles_error(self):
        """Died before any finish reason → honest 'error', never a fake 'done'."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row

        _seed_running_row(self.tid, self.conv)
        write_carrier_terminal_row(
            _carrier_task(self.tid, self.conv, status='running', finish_reason=None),
            'error')
        self.assertEqual(_row_status(self.tid)[0], 'error')

    def test_settle_is_idempotent_upsert(self):
        """Last-writer-wins keyed on task_id — a second settle does not error
        or duplicate the row."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row
        from lib.database import DOMAIN_CHAT, get_thread_db

        _seed_running_row(self.tid, self.conv)
        write_carrier_terminal_row(_carrier_task(self.tid, self.conv), 'done')
        write_carrier_terminal_row(_carrier_task(self.tid, self.conv), 'done')
        db = get_thread_db(DOMAIN_CHAT)
        n = db.execute('SELECT COUNT(*) AS n FROM task_results WHERE task_id=?',
                       (self.tid,)).fetchone()
        self.assertEqual(n['n'] if hasattr(n, 'keys') else n[0], 1)
        self.assertEqual(_row_status(self.tid)[0], 'done')


class TestOwnerWiring(unittest.TestCase):
    """The settle is only load-bearing if the carrier's lifecycle owner calls
    it. Source-guard on autopilot.run_virtual_user's finally block (the same
    scanner style as tests/test_messages_rows_hook_coverage.py)."""

    def _autopilot_src(self):
        with open(os.path.join(ROOT, 'lib', 'tasks_pkg', 'autopilot.py'),
                  encoding='utf-8') as f:
            return f.read()

    def test_finally_block_calls_settle(self):
        src = self._autopilot_src()
        # The finally that discards the VU carrier must ALSO settle its row.
        m = re.search(
            r"discard_task\(sub_task\['id'\].*?write_carrier_terminal_row\(sub_task,",
            src, re.DOTALL)
        self.assertIsNotNone(
            m, 'autopilot.run_virtual_user discards the VU carrier from the '
               'registry but never settles its task_results row — the zombie '
               'generator (pt_8a491f9d) is back')

    def test_neuter_without_settle_row_stays_running(self):
        """NEUTER: prove the settle is what flips the row — without it the
        exact pre-fix zombie shape (row stuck at 'running') remains."""
        from lib.tasks_pkg.manager import write_carrier_terminal_row  # noqa: F401

        tid = 'tk-neuter-' + str(id(self))
        conv = 'cv-neuter-' + str(id(self))
        try:
            from lib.database import init_db
            init_db()
            _seed_running_row(tid, conv)
            # The pre-fix world: discard happened, no settle. Row must still
            # be 'running' — this is what the 11:42 recovery sweep fed on.
            self.assertEqual(_row_status(tid)[0], 'running',
                             'NEUTER setup broken: row unexpectedly terminal '
                             'without any settle')
        finally:
            _cleanup(tid)

    def test_parent_stamped_with_carrier_id(self):
        """The HB-1 back-pointer must be stamped at carrier creation — the
        freshness guard's registry-free identity channel."""
        src = self._autopilot_src()
        self.assertIn("task['_vu_carrier_id'] = sub_task['id']", src,
                      'the parent no longer records its VU carrier id — the '
                      'conv-sync freshness guard cannot recognise the HB-1 '
                      'handoff and falls back to the Unexpected WARNING')


class TestFreshnessGuardVuBranch(unittest.TestCase):
    """manager/_sync.py: superseded-by-own-VU-carrier is the DESIGNED HB-1
    handoff (debug), never the 'never aborted — Unexpected' WARNING."""

    def test_predicate(self):
        from lib.tasks_pkg.manager._sync import _is_own_vu_carrier
        parent = {'id': 'p1', '_vu_carrier_id': 'c1'}
        self.assertTrue(_is_own_vu_carrier('c1', parent))
        self.assertFalse(_is_own_vu_carrier('other', parent))
        self.assertFalse(_is_own_vu_carrier('', parent))
        self.assertFalse(_is_own_vu_carrier('c1', {'id': 'p1'}))  # no stamp
        self.assertFalse(_is_own_vu_carrier(None, parent))

    def test_guard_source_has_vu_branch_before_warning(self):
        """The HB-1 elif must sit between the follow-up branch and the
        Unexpected WARNING else — order matters (first match wins)."""
        with open(os.path.join(ROOT, 'lib', 'tasks_pkg', 'manager', '_sync.py'),
                  encoding='utf-8') as f:
            src = f.read()
        vu_idx = src.find('elif _is_own_vu_carrier(latest, task):')
        warn_idx = src.find('Unexpected — a new task replaced this one')
        self.assertGreater(vu_idx, 0, 'HB-1 branch missing from the freshness guard')
        self.assertGreater(warn_idx, vu_idx,
                           'the Unexpected WARNING is evaluated BEFORE the HB-1 '
                           'branch — own-VU-carrier syncs still mislabel as '
                           'unexpected (app.log:75363 every autopilot turn)')

    def test_neuter_without_stamp_guard_would_warn(self):
        """NEUTER: with the stamp missing the predicate is False, so the
        guard falls through to the WARNING branch — proving the stamp is
        load-bearing for the mislabel fix."""
        from lib.tasks_pkg.manager._sync import _is_own_vu_carrier
        parent = {'id': 'p1'}  # NEUTER: _vu_carrier_id never stamped
        self.assertFalse(_is_own_vu_carrier('c1', parent),
                         'NEUTER failed: predicate true without the stamp — '
                         'the stamp is not load-bearing')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_vu_carrier_row_settle.__main__', init_schema=False)
    unittest.main(verbosity=2)
