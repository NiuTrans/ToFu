#!/usr/bin/env python3
"""tests/test_task_birth_row.py — durable-at-birth ``task_results`` row
(epic pt_f5771a2e, fix B1).

WHY
---
The running-checkpoint writers (``checkpoint_task_partial``) only fire on
content/thinking deltas and per-round boundaries — and no-op while
``content_len == 0 and thinking_len == 0``. A task killed by a server restart
BEFORE its first content delta therefore left NO ``task_results`` row at all,
and every recovery reader — the SSE cold replay
(``lib.chat_dispatch.build_cold_replay_response``), the poll DB path
(``routes/chat_poll_abort.py``), and the startup recovery stale-scan
(``lib/tasks_pkg/manager/_recovery.py``) — found NOTHING: poll and stream
returned 404 'Task not found', and the frontend minted a terminal error
bubble for what was really a TRANSPORT-level task loss. This is the exact
ms43foj3 incident (2026-07-28): the continue task 3cfee531 was killed by a
restart 87s in; its first round was pure ``tool_calls`` (zero content/
thinking deltas) so no row ever existed → poll 404 + stream 404.

The fix: ``create_task`` writes the ``task_results`` row AT CREATION
(``status='running'``, empty content, model/preset meta). Every task is
discoverable from second 0; the checkpoint/persist writers upsert over it
last-writer-wins.

TESTS
  1. ``test_birth_row_written_at_creation`` — row exists with
     status='running', matching conv_id, created_at>0, model/preset meta.
  2. ``test_poll_db_path_finds_birth_row_after_registry_loss`` — the
     incident replay: task created, then REMOVED from the in-memory
     registry (simulating the restart wipe before any checkpoint);
     ``GET /api/v1/chat/poll/<id>`` must NOT 404 (pre-fix: 404).
  3. ``test_birth_row_skipped_when_conv_missing`` — the orphan contract of
     ``_upsert_task_row`` is preserved (a task whose conv row is gone gets
     no row, and creation never raises).
  4. ``test_NC_birth_block_removed_leaves_no_row`` — NEUTER via
     ``tests._nc_harness.neutered_source``: strip the birth-write block
     from ``_registry.py`` and prove create_task leaves no row again
     (i.e. the block, not some other writer, is what saves the task).
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/birth_row_unittest.db')

import pytest

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
REGISTRY_PATH = os.path.join(ROOT, 'lib', 'tasks_pkg', 'manager', '_registry.py')


def _init_db():
    from lib.database import init_db
    init_db()


def _seed_conv(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [{'role': 'user', 'content': 'hi', 'timestamp': 1, '_msgId': 'u0'}]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'birth-row',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _cleanup(conv_id, task_id=None):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1',
                              (conv_id,))
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE conv_id=?', (conv_id,))
        if task_id:
            db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?',
                                  (task_id,))
        db.commit()
    except Exception:
        pass
    if task_id:
        try:
            from lib.tasks_pkg.manager import discard_task
            discard_task(task_id, conv_id)
        except Exception:
            pass


def _birth_row(task_id):
    """The EXACT row lookup the SSE cold replay + poll DB path perform."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    return db.execute(
        'SELECT conv_id, content, thinking, error, status, tool_rounds, metadata '
        'FROM task_results WHERE task_id=?',
        (task_id,)).fetchone()


class TestBirthRow(unittest.TestCase):

    def setUp(self):
        _init_db()
        self.conv_id = 'cv-birth-' + str(id(self)) + '-' + str(int(time.time() * 1000) % 100000)
        _cleanup(self.conv_id)
        _seed_conv(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    def test_birth_row_written_at_creation(self):
        from lib.tasks_pkg.manager import create_task
        task = create_task(self.conv_id, [{'role': 'user', 'content': 'hi'}],
                           {'model': 'kimi-k3', 'preset': 'kimi-k3'})
        try:
            row = _birth_row(task['id'])
            self.assertIsNotNone(
                row,
                'no task_results row at creation — a pre-first-checkpoint death '
                'is invisible to the cold replay / poll DB path / startup recovery '
                '(the ms43foj3 404 incident)')
            self.assertEqual(row['status'], 'running')
            self.assertEqual(row['conv_id'], self.conv_id)
            self.assertEqual(row['content'] or '', '')
            self.assertEqual(row['thinking'] or '', '')
            meta = json.loads(row['metadata']) if row['metadata'] else {}
            self.assertEqual(meta.get('model'), 'kimi-k3')
            self.assertEqual(meta.get('preset'), 'kimi-k3')
        finally:
            _cleanup(self.conv_id, task['id'])

    def test_db_lookup_finds_birth_row_after_registry_loss(self):
        """The incident replay: task created (birth row written), then the
        in-memory registry loses it (the restart wipe before any checkpoint).
        The EXACT row lookup the SSE cold replay + poll DB path perform must
        resolve the task from the DB row — NOT come back empty (the 404)."""
        from lib.tasks_pkg.manager import create_task, tasks, tasks_lock
        task = create_task(self.conv_id, [{'role': 'user', 'content': 'hi'}],
                           {'model': 'kimi-k3'})
        tid = task['id']
        try:
            # Simulate the restart: the process-local registry forgets the task.
            with tasks_lock:
                tasks.pop(tid, None)
            row = _birth_row(tid)
            self.assertIsNotNone(
                row,
                'post-restart DB lookup finds nothing — this is the 404 the '
                'frontend turned into a terminal error bubble')
            self.assertEqual(row['status'], 'running',
                             'birth row must read running until recovery settles it')
        finally:
            _cleanup(self.conv_id, tid)

    def test_birth_row_skipped_when_conv_missing(self):
        """The _upsert_task_row orphan contract survives: a task whose parent
        conv row does NOT exist gets no birth row (no resurrecting orphan
        rows), and creation never raises."""
        from lib.tasks_pkg.manager import create_task
        ghost_conv = 'cv-birth-ghost-' + str(int(time.time() * 1000) % 100000)
        _cleanup(ghost_conv)
        task = create_task(ghost_conv, [{'role': 'user', 'content': 'hi'}],
                           {'model': 'kimi-k3'})
        try:
            row = _birth_row(task['id'])
            self.assertIsNone(
                row,
                'orphan guard broken: a task with no parent conversation row '
                'must NOT leave a task_results row')
        finally:
            _cleanup(ghost_conv, task['id'])

    def test_NC_birth_block_removed_leaves_no_row(self):
        """NEUTER: strip the durable-at-birth write from _registry.py and prove
        create_task leaves NO row again — the block is load-bearing, not some
        other writer."""
        from tests._nc_harness import neutered_source
        anchor = (
            "        _upsert_task_row(\n"
            "            task, conv_id or '', content='', thinking='', status='running',\n"
            "            error_json=None, tr_json=None,\n"
            "            meta_json=(json.dumps(_birth_meta, ensure_ascii=False)\n"
            "                       if _birth_meta else None))")
        assert anchor in open(REGISTRY_PATH, encoding='utf-8').read(), (
            'NC anchor drifted — the durable-at-birth write no longer looks '
            'like the pinned shape; update the neuter target')
        with neutered_source(
                REGISTRY_PATH,
                anchor,
                "        pass  # NC: durable-at-birth write removed") as mod:
            task = mod.create_task(self.conv_id,
                                   [{'role': 'user', 'content': 'hi'}],
                                   {'model': 'kimi-k3'})
            try:
                row = _birth_row(task['id'])
                self.assertIsNone(
                    row,
                    'NC did not bite: a row exists even with the birth write '
                    'removed — some OTHER writer is covering (test no longer '
                    'pins the durable-at-birth block)')
            finally:
                _cleanup(self.conv_id, task['id'])


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_task_birth_row.__main__', init_schema=False)
    unittest.main(verbosity=2)
