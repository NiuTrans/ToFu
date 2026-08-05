#!/usr/bin/env python3
"""tests/test_recovery_toolrounds_from_segments.py — after an OS-kill, Continue
must NOT regenerate from scratch just because ``task_results.tool_rounds`` is
NULL. The rounds are recoverable from the sibling ``segments`` column.

ROOT-CAUSE forensics (2026-07-20, conv mrt1ijef, PostgreSQL)
------------------------------------------------------------
The owner's server was SIGKILLed by the shared-cgroup OOM mid-generation. On
restart, ``recover_stale_tasks_on_startup`` reported the conv "recovered", yet
clicking Continue could regenerate from scratch instead of resuming the 20+
tool rounds visible on screen. DB dump of the interrupted rows:

    task_id   status       content  tool_rounds  segments
    09c6d8c6  interrupted  0        NULL         len=100982
    f43c2e3e  interrupted  9        NULL         len=110146

The structural gap (two halves):
  * PERSIST side: normal DB-backed chats set ``_tool_rounds_have_dedicated_home``
    True, so ``persist_task_result`` / ``checkpoint_task_partial`` write
    ``task_results.tool_rounds = NULL`` on purpose (the rounds live in
    ``conversations.messages``). The ``segments`` column IS written every
    checkpoint (``assemble_segments``) — so it carries the rounds.
  * RECOVER side: ``recover_stale_tasks_on_startup`` ONLY merged
    ``task_row['tool_rounds']`` into the conversation — a guaranteed no-op when
    that column is NULL. It NEVER consulted ``segments``. So when the
    ``conversations.messages`` copy was staler than the crash point (the 5s
    partial-sync coalescing window), recovery had NO way to restore the rounds,
    ``scan_continue_checkpoint`` found no complete batch → Continue fell back to
    regenerate-from-scratch.

The fix (lib/tasks_pkg/manager/_recovery.py): when ``task_row['tool_rounds']``
is NULL/empty, rebuild the rounds from ``task_row['segments']`` via
``_rounds_view_from_segments`` (which sources toolCallId/status/toolContent
straight from the thin persisted segments — no ``_round`` mirror needed) and
merge THAT.

Revert-proofing (NEUTER): ``test_neuter_ignoring_segments_reproduces_bug``
drives the pre-fix behaviour (ignore segments) against the identical DB state
and asserts Continue would fall back — proving the segments-restore is
load-bearing, not decorative.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/recov_seg.db \
        python3 tests/test_recovery_toolrounds_from_segments.py
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
os.environ.setdefault('TOFU_DB_PATH', '/tmp/recov_seg_unittest.db')

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


# The realistic interrupted shape: ONE complete tool batch (llmRound 0) plus a
# trailing PARTIAL round the crash left mid-flight (status='searching', no
# toolContent). Continue must keep the complete round and discard the partial.
def _crash_rounds():
    return [
        {'roundNum': 1, 'llmRound': 0, 'toolName': 'read_files',
         'toolCallId': 'tc_read_1', 'status': 'done',
         'toolContent': 'contents of foo.py', 'toolArgs': '{"path": "foo.py"}',
         'assistantContent': 'Let me read the file.'},
        {'roundNum': 2, 'llmRound': 1, 'toolName': 'run_command',
         'toolCallId': 'tc_run_2', 'status': 'searching',
         'toolContent': None, 'toolArgs': '{"command": "pytest"}'},
    ]


def _thin_segments_for(rounds, content, thinking):
    """Build the THIN (persisted) segment blob exactly as the checkpoint writer
    would: assemble_segments over the rounds + terminal content, then strip the
    _round mirror via segments_to_json. This is byte-for-byte the shape the
    ``segments`` column holds for an interrupted task."""
    from lib.tasks_pkg.segments import assemble_segments, segments_to_json
    task = {'content': content, 'thinking': thinking, 'toolRounds': rounds}
    return segments_to_json(assemble_segments(task))


def _seed_conv(conv_id, *, active_task_id=None):
    """A conv whose last assistant turn is THIN (checkpoint-less) — the shape
    left behind when the crash landed before partial-sync wrote the rounds into
    conversations.messages."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [
        {'role': 'user', 'content': 'do a long multi-tool task', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    settings = {}
    if active_task_id is not None:
        settings['activeTaskId'] = active_task_id
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'recov-seg',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'settings': json_dumps_pg(settings),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'settings', 'created_at', 'updated_at'], retry=True)
    db.commit()


def _seed_task_null_tr_with_segments(task_id, conv_id, *, content, thinking,
                                     rounds):
    """The EXACT production row: status='running', tool_rounds=NULL, but the
    segments column populated (what a normal DB-backed chat checkpoints)."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import TASK_RESULTS, upsert
    thin = _thin_segments_for(rounds, content, thinking)
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': task_id, 'conv_id': conv_id,
        'content': content, 'thinking': thinking, 'error': None,
        'status': 'running',
        'tool_rounds': None,                       # ← the NULL that broke recovery
        'segments': json_dumps_pg(thin),           # ← but segments carry the rounds
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


def _last_assistant(messages):
    for m in reversed(messages or []):
        if m.get('role') == 'assistant':
            return m
    return None


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


class TestRecoveryToolRoundsFromSegments(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        uid = str(id(self))
        self.conv_id = 'cv-recovseg-' + uid
        self.task_id = 'tk-recovseg-' + uid
        _cleanup(self.conv_id, self.task_id)

    def tearDown(self):
        _cleanup(self.conv_id, self.task_id)

    # ── The end-to-end payoff: recovery restores rounds from segments so the
    #    real Continue gate finds a checkpoint (no regenerate-from-scratch). ──
    def test_continue_finds_checkpoint_when_tool_rounds_null_but_segments_present(self):
        from lib.chat.turn_builder import scan_continue_checkpoint
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        _seed_conv(self.conv_id, active_task_id=None)
        _seed_task_null_tr_with_segments(
            self.task_id, self.conv_id,
            content='Partial answer before the OOM kill.',
            thinking='mid reasoning', rounds=_crash_rounds())

        recover_stale_tasks_on_startup()

        settings, messages = _read_conv(self.conv_id)
        am = _last_assistant(messages)
        self.assertIsNotNone(am, 'no assistant message after recovery')
        self.assertTrue(
            am.get('toolRounds'),
            'recovery merged NO toolRounds — the segments column was ignored, '
            'so Continue has nothing to scan (the production bug).')

        # THE GATE that decides Continue-from-checkpoint vs regenerate.
        scan = scan_continue_checkpoint(am)
        self.assertIsNotNone(
            scan,
            'scan_continue_checkpoint returned None — Continue would FALL BACK '
            'to regenerate-from-scratch. Recovery must rebuild rounds from '
            'segments when tool_rounds is NULL.')
        self.assertEqual(len(scan['kept_rounds']), 1,
                         'expected the 1 complete round kept')
        self.assertEqual(scan['discarded_rounds'], 1,
                         'expected the trailing partial round discarded')
        self.assertEqual(scan['kept_rounds'][0]['toolCallId'], 'tc_read_1')
        self.assertTrue(scan['tool_history'],
                        'tool_history empty — Continue would regenerate')

    def test_recovered_content_and_finish_reason_still_set(self):
        """Behaviour preservation: the content/thinking merge + interrupted
        stamp still happen (the segments-restore is ADDITIVE)."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
        _seed_conv(self.conv_id, active_task_id=self.task_id)
        _seed_task_null_tr_with_segments(
            self.task_id, self.conv_id,
            content='Partial answer.', thinking='r', rounds=_crash_rounds())

        recover_stale_tasks_on_startup()

        settings, messages = _read_conv(self.conv_id)
        self.assertIsNone(settings.get('activeTaskId'),
                          'activeTaskId not cleared')
        am = _last_assistant(messages)
        self.assertEqual(am.get('content'), 'Partial answer.')
        self.assertEqual(am.get('finishReason'), 'interrupted')

    def test_neuter_ignoring_segments_reproduces_bug(self):
        """NEUTER: simulate the PRE-FIX recovery (only merge tool_rounds, ignore
        segments) against the identical NULL-tool_rounds row, and assert
        Continue would fall back. Proves the segments-restore is load-bearing.

        We reproduce the pre-fix merge inline rather than call the (fixed)
        production function so the assertion pins the OLD behaviour."""
        from lib.chat.turn_builder import scan_continue_checkpoint

        rounds = _crash_rounds()
        content = 'Partial answer before the OOM kill.'
        # Pre-fix recovery merged ONLY task_row['tool_rounds'] — which is NULL.
        task_tool_rounds = None  # the production NULL
        last_msg = {'role': 'assistant', 'content': '', 'thinking': '',
                    'toolRounds': []}
        # This is the exact pre-fix guard (recovery._recovery.py:174):
        if task_tool_rounds:
            tr = json.loads(task_tool_rounds)
            if tr and len(tr) > len(last_msg.get('toolRounds') or []):
                last_msg['toolRounds'] = tr
        last_msg['content'] = content
        last_msg['finishReason'] = 'interrupted'

        # With segments ignored, the recovered message has NO rounds → scan None.
        self.assertFalse(last_msg.get('toolRounds'),
                         'neuter precondition: pre-fix leaves toolRounds empty')
        scan = scan_continue_checkpoint(last_msg)
        self.assertIsNone(
            scan,
            'NEUTER FAILED TO REPRODUCE: scan should be None when segments are '
            'ignored and tool_rounds is NULL — if it is not None the fix is not '
            'load-bearing.')
        # And to prove the fix path WOULD work, restore from segments here:
        from lib.tasks_pkg.segments import _rounds_view_from_segments
        thin = _thin_segments_for(rounds, content, '')
        rebuilt = _rounds_view_from_segments(thin)
        last_msg['toolRounds'] = rebuilt
        scan2 = scan_continue_checkpoint(last_msg)
        self.assertIsNotNone(
            scan2,
            'segments-restore path failed: rebuilt rounds did not satisfy scan')
        self.assertEqual(len(scan2['kept_rounds']), 1)


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_recovery_toolrounds_from_segments.__main__',
                        init_schema=False)
    unittest.main(verbosity=2)
