#!/usr/bin/env python3
"""tests/test_stale_recovery_authoritative.py — server-restart interrupted-turn
recovery must be driven by the AUTHORITATIVE ``task_results.conv_id``, not the
frontend-synced (and often absent) ``settings.activeTaskId``.

ROOT-CAUSE verification for the user-reported bug: "I updated the server while a
conversation was generating, restarted mid-stream, came back, clicked Continue —
and it spawned a brand-new agent from scratch."

Forensics (2026-07-01 logs + DB):
  * Startup ``recover_stale_tasks_on_startup`` reported ``Marked 18 stale
    running task(s) as interrupted`` but ``0 conv(s) cleaned``.
  * The interrupted ``task_results`` row DID carry the correct
    ``conv_id`` (backend-stamped by ``create_task``) with thousands of chars
    of recovered content + thinking.
  * BUT Step 2/3 iterated conversations by ``settings.activeTaskId`` — which is
    a frontend-synced pointer that is null/stale after a mid-stream crash (the
    PUT that persists it may never have landed). So the recovered content was
    NEVER merged into ``conversations.messages`` and the turn was NEVER stamped
    ``finishReason='interrupted'``.
  * Downstream: reload's Case B recovery also keys on ``activeTaskId`` → the
    conversation's last assistant turn stays thin/checkpoint-less →
    ``continueAssistant`` finds no tool checkpoint → falls back to
    ``startAssistantResponse`` = a brand-new agent from scratch.

The fix (lib/tasks_pkg/manager.py::recover_stale_tasks_on_startup) UNIONs the
conversation sources by ``conv_id``: conversations carrying ``activeTaskId`` AND
conversations that OWN an interrupted task via ``task_results.conv_id``. The
merge is driven off the interrupted task, so the turn is recovered even when
``activeTaskId`` was never persisted.

Revert-proofing: the ``test_recovers_when_active_task_id_absent`` case FAILS
against the pre-fix logic (which skips convs without ``activeTaskId``).

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/stale_recovery.db \
        python3 tests/test_stale_recovery_authoritative.py
or via pytest.
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/stale_recovery_unittest.db')

import pytest

pytestmark = pytest.mark.unit


def _seed_conv(conv_id, *, active_task_id=None, last_role='assistant',
               last_content='', last_thinking=''):
    """Seed a conversation whose last message is a thin (checkpoint-less)
    assistant turn — the shape left behind when a mid-stream crash lands before
    the frontend persisted the streamed content into the conversation row."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [{'role': 'user', 'content': 'do a long task', 'timestamp': 1}]
    if last_role == 'assistant':
        messages.append({'role': 'assistant', 'content': last_content,
                         'thinking': last_thinking, 'toolRounds': [],
                         'timestamp': 2})
    settings = {}
    if active_task_id is not None:
        settings['activeTaskId'] = active_task_id
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'stale-recovery',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'settings': json_dumps_pg(settings),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'settings', 'created_at', 'updated_at'], retry=True)
    db.commit()


def _seed_running_task(task_id, conv_id, *, content, thinking, tool_rounds=None):
    """Insert a task_results row in the status='running' shape a checkpoint
    leaves behind — the interrupted-mid-stream state.

    ``tool_rounds`` defaults to a single complete read_files round; callers can
    pass a realistic multi-round shape (e.g. a complete batch followed by a
    trailing partial/searching round that Continue must discard)."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import TASK_RESULTS, upsert
    if tool_rounds is None:
        tool_rounds = [
            {'roundNum': 1, 'toolName': 'read_files', 'toolCallId': 'tc1',
             'status': 'done', 'toolContent': 'file body',
             'assistantContent': 'Reading the file.'},
        ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': task_id, 'conv_id': conv_id,
        'content': content, 'thinking': thinking, 'error': None,
        'status': 'running',
        'tool_rounds': json_dumps_pg(tool_rounds),
        'metadata': json_dumps_pg({'model': 'test-model'}),
        'created_at': now_ms, 'completed_at': now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                    'status', 'tool_rounds', 'metadata', 'created_at',
                    'completed_at'], retry=True)
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


class TestStaleRecoveryAuthoritative(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        uid = str(id(self))
        self.conv_id = 'cv-stalerec-' + uid
        self.task_id = 'tk-stalerec-' + uid
        _cleanup(self.conv_id, self.task_id)

    def tearDown(self):
        _cleanup(self.conv_id, self.task_id)

    def test_recovers_when_active_task_id_absent(self):
        """THE BUG: interrupted task carries conv_id, but the conversation has
        NO activeTaskId. Recovery must still merge the recovered content and
        stamp finishReason='interrupted' — driven by task_results.conv_id."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
        _seed_conv(self.conv_id, active_task_id=None,
                   last_role='assistant', last_content='', last_thinking='')
        _seed_running_task(self.task_id, self.conv_id,
                           content='Recovered partial answer.', thinking='some reasoning')

        recover_stale_tasks_on_startup()

        settings, messages = _read_conv(self.conv_id)
        am = _last_assistant(messages)
        self.assertIsNotNone(am, 'no assistant message after recovery')
        self.assertEqual(am.get('content'), 'Recovered partial answer.',
                         'interrupted content was NOT merged (the orphaned-turn bug)')
        self.assertEqual(am.get('thinking'), 'some reasoning',
                         'interrupted thinking was NOT merged')
        self.assertEqual(am.get('finishReason'), 'interrupted',
                         "turn was NOT stamped finishReason='interrupted'")
        self.assertTrue(am.get('toolRounds'),
                        'recovered toolRounds (the continue checkpoint) were NOT merged')

    def test_recovers_when_active_task_id_present(self):
        """Regression guard: the classic path (activeTaskId set) still works and
        the pointer is cleared."""
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
        _seed_conv(self.conv_id, active_task_id=self.task_id,
                   last_role='assistant', last_content='', last_thinking='')
        _seed_running_task(self.task_id, self.conv_id,
                           content='Recovered via activeTaskId.', thinking='r')

        recover_stale_tasks_on_startup()

        settings, messages = _read_conv(self.conv_id)
        self.assertIsNone(settings.get('activeTaskId'),
                          'activeTaskId should be cleared after recovery')
        am = _last_assistant(messages)
        self.assertEqual(am.get('content'), 'Recovered via activeTaskId.')
        self.assertEqual(am.get('finishReason'), 'interrupted')

    def test_continue_finds_checkpoint_on_recovered_message(self):
        """END-TO-END PAYOFF: after recovery merges the interrupted turn (via
        task_results.conv_id, NO activeTaskId), the SAME function
        /api/chat/continue uses — lib.chat.turn_builder.scan_continue_checkpoint
        — must find a recoverable checkpoint on the merged assistant message.

        This is the exact gate that decides Continue-from-checkpoint vs
        fall-back-to-regenerate-from-scratch: the backend endpoint returns
        {fallback:'regenerate'} when scan returns None, and the frontend then
        pops + starts a brand-new agent. So merging the toolRounds is NOT enough
        — they must satisfy the scan's 'complete batch' shape (toolCallId +
        status=='done' + toolContent, correct batch grouping) AND a trailing
        partial round must be correctly discarded.
        """
        from lib.chat.turn_builder import scan_continue_checkpoint
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup

        # Realistic interrupted shape: one COMPLETE tool batch (llmRound 0)
        # followed by a trailing PARTIAL round the crash left mid-flight —
        # exactly what the streaming executor persists on a mid-stream crash.
        realistic_rounds = [
            {'roundNum': 1, 'llmRound': 0, 'toolName': 'read_files',
             'toolCallId': 'tc_read_1', 'status': 'done',
             'toolContent': 'contents of foo.py', 'toolArgs': '{"path": "foo.py"}',
             'assistantContent': 'Let me read the file.'},
            # Trailing partial: announced but never finished (no toolContent,
            # status still 'searching') — must be DISCARDED by the scan.
            {'roundNum': 2, 'llmRound': 1, 'toolName': 'run_command',
             'toolCallId': 'tc_run_2', 'status': 'searching',
             'toolContent': None, 'toolArgs': '{"command": "pytest"}'},
        ]
        _seed_conv(self.conv_id, active_task_id=None,
                   last_role='assistant', last_content='', last_thinking='')
        _seed_running_task(self.task_id, self.conv_id,
                           content='Partial answer before crash.',
                           thinking='mid reasoning',
                           tool_rounds=realistic_rounds)

        recover_stale_tasks_on_startup()

        settings, messages = _read_conv(self.conv_id)
        am = _last_assistant(messages)
        self.assertIsNotNone(am, 'no assistant message after recovery')
        self.assertTrue(am.get('toolRounds'),
                        'recovery merged no toolRounds — nothing for Continue to scan')

        # THE PAYOFF: the real Continue gate must find a checkpoint.
        scan = scan_continue_checkpoint(am)
        self.assertIsNotNone(
            scan,
            'scan_continue_checkpoint returned None on the recovered message — '
            'Continue would FALL BACK to regenerate-from-scratch (the bug lives). '
            'The merged toolRounds did not satisfy the complete-batch shape.')
        self.assertGreaterEqual(
            len(scan['kept_rounds']), 1,
            'checkpoint kept 0 rounds — Continue has nothing to resume from')
        # The complete read_files round is kept; the trailing searching round is
        # discarded (the scan breaks at the first non-'done' round).
        self.assertEqual(len(scan['kept_rounds']), 1,
                         'expected exactly the 1 complete round kept')
        self.assertEqual(scan['discarded_rounds'], 1,
                         'expected the trailing partial round to be discarded')
        self.assertTrue(scan['tool_history'],
                        'tool_history is empty — frontend continueAssistant would '
                        'see toolHistory.length===0 and regenerate from scratch')
        self.assertEqual(scan['kept_rounds'][0]['toolCallId'], 'tc_read_1')

    def test_task_marked_interrupted_in_task_results(self):
        """Step 1 invariant: the running task row is flipped to 'interrupted'."""
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
        _seed_conv(self.conv_id, active_task_id=None)
        _seed_running_task(self.task_id, self.conv_id, content='x', thinking='')

        recover_stale_tasks_on_startup()

        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute('SELECT status FROM task_results WHERE task_id=?',
                         (self.task_id,)).fetchone()
        self.assertEqual(row['status'], 'interrupted')


if __name__ == '__main__':
    unittest.main(verbosity=2)
