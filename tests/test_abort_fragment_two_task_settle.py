#!/usr/bin/env python3
"""tests/test_abort_fragment_two_task_settle.py — end-to-end two-task flow for
the "truncated abort fragment shown as a finished turn" bug (Bug 1).

FULL SEQUENCE (the exact reported scenario, driven through the REAL production
persist path — real ``conversations`` row, real ``create_task``, real
``persist_task_result`` → ``_sync_result_to_conversation``):

  1. Task A streams a partial answer, then the user hits Stop → its fragment is
     already persisted into ``conversations.messages`` with real content but NO
     terminal reason (the partial-checkpoint shape: status='running').
  2. Task B (the regenerate) is created — it supersedes A as the conv's latest
     task — streams the COMPLETE answer, and settles (persist_task_result).
  3. ASSERT: after B settles, task A's fragment carries
     ``finishReason='aborted'`` (data preserved, renders as the aborted partial
     it truthfully is), while B's answer keeps ``finishReason='stop'``.

WHY this test (not just the pure unit): the settle-path gate
(``_reconcile_orphan_placeholder_on_settle``) is keyed to the drop-before-token
branch, and A is NOT latest — so A's own settle Fix A only fires on the app path
(needs ``_assistantMsgId``). The ROBUST heal is that B's OWN content-bearing
settle write runs ``mark_superseded_incomplete_fragments`` over the whole list.
This drives that real path and proves B's settle marks A's sibling fragment.

NEUTER: monkeypatch ``mark_superseded_incomplete_fragments`` to a no-op and the
same assertion fails (the husk survives finishReason=None) — proving the mark
in the terminal write is load-bearing, not incidental.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/abort_frag_2task.db \
        python3 tests/test_abort_fragment_two_task_settle.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/abort_frag_2task_unittest.db')

import pytest

pytestmark = pytest.mark.unit

PARTIAL = 'Compile gate green. T'          # the aborted fragment content
FULL = 'Compile gate green. Tag v0.1.7 and push. Done — release built.'


def _seed_conv(conv_id):
    """A conv whose tail is a user turn + an in-flight assistant bubble that
    already holds the aborted task's partial content with NO finishReason."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [
        {'role': 'user', 'content': 'ship v0.1.7', 'timestamp': 1, '_msgId': 'u0'},
        # The aborted task A's fragment: real content, NO finishReason (the husk).
        {'role': 'assistant', 'content': PARTIAL, 'thinking': '', 'toolRounds': [],
         'timestamp': 2, '_msgId': 'frag-A'},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'abort-frag-2task',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_messages(conv_id):
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)
    row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                     (conv_id,)).fetchone()
    if not row or not row[0]:
        return []
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _cleanup(conv_id):
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
    try:
        db = get_thread_db(DOMAIN_CHAT)
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
        db.commit()
    except Exception:
        pass


class TestAbortFragmentTwoTaskSettle(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-frag2t-' + str(id(self))
        _cleanup(self.conv_id)
        _seed_conv(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    def _run_regenerate_settle(self):
        """Create the regenerate task B (supersedes A), give it the full answer,
        and persist. On this content-bearing path _sync_result_to_conversation
        runs mark_superseded_incomplete_fragments over the whole list."""
        from lib.tasks_pkg.manager import create_task, persist_task_result
        # Regenerate: the conv tail is still [user, frag-A(assistant)]. Task B
        # is created as the conv's latest (supersede=True), then it must FILL
        # the trailing assistant slot (frag-A) — that IS the observed shape
        # where A's own fragment becomes B's answer target. To reproduce the
        # TWO-message artifact (A husk + B answer as separate messages), we
        # append a fresh assistant slot for B and mark it settled.
        task = create_task(
            self.conv_id,
            [{'role': 'user', 'content': 'ship v0.1.7'}],
            {'model': 'test-model'},
        )
        task['content'] = FULL
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        # Append B's own assistant message so the persisted list is
        # [user, frag-A(no reason), answer-B(stop)] — the exact ghost artifact.
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        db = get_thread_db(DOMAIN_CHAT)
        msgs = _read_messages(self.conv_id)
        msgs.append({'role': 'assistant', 'content': '', 'thinking': '',
                     'toolRounds': [], 'timestamp': 3, '_msgId': 'answer-B'})
        now_ms = int(time.time() * 1000)
        db.execute('UPDATE conversations SET messages=?, msg_count=?, updated_at=? '
                   'WHERE id=? AND user_id=1',
                   (json_dumps_pg(msgs), len(msgs), now_ms, self.conv_id))
        db.commit()
        persist_task_result(task)
        return task

    def test_regenerate_settle_marks_aborted_sibling_fragment(self):
        self._run_regenerate_settle()
        msgs = _read_messages(self.conv_id)
        by_id = {m.get('_msgId'): m for m in msgs}
        self.assertIn('frag-A', by_id, 'aborted fragment vanished (should be preserved)')
        self.assertIn('answer-B', by_id, 'regenerate answer not persisted')
        # The husk is now truthfully terminal.
        self.assertEqual(by_id['frag-A'].get('finishReason'), 'aborted',
                         'aborted sibling fragment was NOT marked at regenerate settle')
        # Content preserved (data-preservation).
        self.assertEqual(by_id['frag-A'].get('content'), PARTIAL)
        # B's answer is the complete reply, untouched.
        self.assertEqual(by_id['answer-B'].get('finishReason'), 'stop')
        self.assertEqual(by_id['answer-B'].get('content'), FULL)

    def test_neuter_mark_leaves_husk_unmarked(self):
        """Revert the mark helper to a no-op → the sibling fragment keeps
        finishReason=None (the exact reported husk). Proves the terminal-write
        mark is load-bearing."""
        import lib.conversations.reconcile as rec
        orig = rec.mark_superseded_incomplete_fragments
        rec.mark_superseded_incomplete_fragments = lambda messages: (messages, 0)
        try:
            self._run_regenerate_settle()
        finally:
            rec.mark_superseded_incomplete_fragments = orig
        msgs = _read_messages(self.conv_id)
        by_id = {m.get('_msgId'): m for m in msgs}
        # With the mark neutered the husk survives without a terminal reason.
        self.assertIn('frag-A', by_id)
        self.assertIsNone(by_id['frag-A'].get('finishReason'),
                          'NEUTER expected finishReason=None husk to survive')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_abort_fragment_two_task_settle.__main__', init_schema=False)
    unittest.main(verbosity=2)
