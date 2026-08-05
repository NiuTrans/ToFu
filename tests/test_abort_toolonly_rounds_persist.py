#!/usr/bin/env python3
"""Reproduction: a turn ABORTED after tool rounds completed but with NO
assistant content/thinking yet — are the completed rounds persisted into
``conversations.messages``, or dropped?

REPORTED SYMPTOM
----------------
"The agent had already invoked many tools, then I interrupted it, and all the
tools on the front end simply disappeared — I had to restart the server."

The in-session frontend keeps the rounds (finalizeStreaming re-renders the
bubble from ``msg.toolRounds``; verified by
tests/test_frontend_abort_toolrounds_survive.py::abort_basic). So the loss the
user saw survives across a RELOAD / restart — i.e. it is a PERSISTENCE gap, and
the UI on reload renders from ``conversations.messages``.

THE SUSPECTED SEAM
------------------
``manager._sync_result_to_conversation`` (lib/tasks_pkg/manager/_sync.py:403)
early-returns BEFORE writing when::

    if not content and not thinking and not error:   # <-- does NOT consult toolRounds
        ... reconcile orphan ... ; return

So a turn where the model ran tools but produced no prose/thinking before Stop
(``content=='' and thinking==''``) has its rounds written ONLY to the
``task_results`` aborted-floor (a durable terminal record, but NOT the
conversation), never into ``conversations.messages``. On reload the UI reads
``conversations.messages`` → the rounds are gone permanently.

Corroboration: tests/test_abort_dangling_tool_round.py deliberately injects
``task['content'] = 'Running the command…'`` with the explicit comment
"the persist path … short-circuits when content+thinking+error are all empty"
— i.e. existing coverage sidesteps exactly this shape.

WHAT THIS TEST DOES
-------------------
Drives the REAL production write path — ``create_task`` (registered as the
conv's latest so the freshness guard passes) → ``persist_task_result`` →
``_sync_result_to_conversation`` — for a task whose ``toolRounds`` carry N
completed (``status='done'``) rounds but whose ``content``/``thinking`` are
empty and ``finishReason='aborted'``. It then reads the conversation row back
and asserts on the persisted rounds.

  * ``test_toolonly_abort_rounds_reach_conversation`` — the CONTRACT we want:
    the completed rounds land in ``conversations.messages``. Today this is
    EXPECTED TO FAIL (reproducing the bug). It becomes the green target once
    the skip guard also considers a real tool round.
  * ``test_content_bearing_abort_still_persists`` — CONTROL: the same abort WITH
    content persists fine (proves the write path itself works; isolates the
    empty-content guard as the sole difference).

Standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/toolonly.db \
        python3 tests/test_abort_toolonly_rounds_persist.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/abort_toolonly_unittest.db')

import pytest

# ci_serial: real create_task + persist_task_result write through the shared
# sqlite pool; under the CI parallel lane's contention the seed write exceeded
# the 30s busy timeout ('database is locked', de81786 3.12 leg) while passing
# in seconds uncontended.
pytestmark = [pytest.mark.unit, pytest.mark.ci_serial]


def _seed_conv(conv_id):
    """A conversations row whose last message is an in-flight streaming
    assistant bubble (the shape the frontend persists mid-turn)."""
    from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    messages = [
        {'role': 'user', 'content': 'investigate the exporter path', 'timestamp': 1},
        {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
         'timestamp': 2},
    ]
    db = get_thread_db(DOMAIN_CHAT)
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'abort-toolonly',
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
            return m.get('toolRounds')
    return None


def _read_persisted_messages(conv_id):
    """Return the full persisted messages list (what GET/startup renders from)."""
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


def _mk_done_rounds(n):
    """N completed tool rounds in the shape the streaming executor persists."""
    rounds = []
    for i in range(n):
        rounds.append({
            'roundNum': i + 1,
            'toolName': 'read_files',
            'toolCallId': f'tc{i}',
            'status': 'done',
            'toolContent': f'file contents {i}',
            'results': [{'toolName': 'read_files', 'ok': True}],
            'query': f'read file {i}',
        })
    return rounds


class TestToolOnlyAbortPersist(unittest.TestCase):

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-toolonly-' + str(id(self))
        _cleanup(self.conv_id)
        _seed_conv(self.conv_id)

    def tearDown(self):
        _cleanup(self.conv_id)

    def _make_task(self):
        from lib.tasks_pkg.manager import create_task
        task = create_task(
            self.conv_id,
            [{'role': 'user', 'content': 'investigate the exporter path'}],
            {'model': 'test-model', 'projectEnabled': True},
        )
        return task

    def test_toolonly_abort_rounds_reach_conversation(self):
        """CONTRACT (currently reproduces the bug): a turn aborted after tools
        ran, with NO content/thinking, must still persist its completed rounds
        into conversations.messages — else they vanish on reload."""
        from lib.tasks_pkg.manager import persist_task_result

        task = self._make_task()
        N = 4
        task['toolRounds'] = _mk_done_rounds(N)
        # The reported shape: tools done, but the user hit Stop BEFORE the model
        # emitted any closing prose or separate thinking text.
        task['content'] = ''
        task['thinking'] = ''
        task['error'] = None
        task['finishReason'] = 'aborted'
        task['aborted'] = True
        task['status'] = 'done'

        persist_task_result(task)

        persisted = _read_persisted_tool_rounds(self.conv_id)
        # The heart of the reproduction: are the completed rounds in the conv?
        self.assertIsNotNone(
            persisted,
            'assistant turn missing from conversations.messages after a '
            'tool-only abort — the whole turn was dropped')
        done = [r for r in (persisted or []) if r.get('status') == 'done']
        self.assertEqual(
            len(done), N,
            f'expected {N} completed tool rounds persisted into '
            f'conversations.messages, found {len(done)}. The tool-only abort '
            f'dropped them at the empty-content skip guard '
            f'(_sync.py _sync_result_to_conversation): persisted={persisted!r}')

    def test_reload_reconcile_keeps_toolonly_aborted_turn(self):
        """After the write, the RELOAD/startup path (classify_ghost_tail +
        reconcile_conversation_messages) must KEEP the turn — not sweep it as a
        ghost. The write stamps finishReason='aborted' AND leaves real done
        rounds, so classify_ghost_tail returns None and reconcile makes no
        change. This proves the fix survives a server restart, not just the
        first persist."""
        from lib.tasks_pkg.manager import persist_task_result
        from lib.conversations.reconcile import (
            classify_ghost_tail, reconcile_conversation_messages)

        task = self._make_task()
        N = 4
        task['toolRounds'] = _mk_done_rounds(N)
        task['content'] = ''
        task['thinking'] = ''
        task['error'] = None
        task['finishReason'] = 'aborted'
        task['aborted'] = True
        task['status'] = 'done'
        persist_task_result(task)

        msgs = _read_persisted_messages(self.conv_id)
        self.assertTrue(msgs, 'conversation lost all messages')
        tail = msgs[-1]
        self.assertEqual(tail.get('role'), 'assistant',
                         'tail is not the assistant turn we persisted')
        # The turn carries a truthful terminal reason...
        self.assertEqual(tail.get('finishReason'), 'aborted',
                         'tool-only abort turn must carry finishReason=aborted '
                         'so the reload reconcile does not treat it as a ghost')
        # ...so the reload verdict is KEEP, not delete/interrupt.
        self.assertIsNone(
            classify_ghost_tail(tail),
            'reload classify_ghost_tail would remove the tool-only aborted '
            'turn — the rounds would vanish again on restart')
        # And the full reconcile pass leaves it untouched.
        reconciled, changed = reconcile_conversation_messages(list(msgs))
        self.assertFalse(
            changed,
            'reconcile_conversation_messages mutated a settled tool-only abort '
            f'turn (would drop it on reload): {reconciled!r}')
        done = [r for r in (reconciled[-1].get('toolRounds') or [])
                if r.get('status') == 'done']
        self.assertEqual(len(done), N,
                         'reconcile dropped the completed rounds on reload')

    def test_NC_guard_ignoring_toolrounds_drops_the_turn(self):
        """NEUTER: restore the OLD skip guard (content/thinking/error only, NOT
        consulting has_real_round) and the tool-only aborted turn is dropped
        again — proving the has_real_round check is load-bearing, not decorative.

        Monkeypatches has_real_round to always return False for the duration,
        which makes ``not _has_real_tool_round`` True and re-triggers the
        empty-content skip → orphan reconcile → turn removed."""
        import lib.conversations.reconcile as _rec
        from lib.tasks_pkg.manager import persist_task_result

        task = self._make_task()
        N = 4
        task['toolRounds'] = _mk_done_rounds(N)
        task['content'] = ''
        task['thinking'] = ''
        task['error'] = None
        task['finishReason'] = 'aborted'
        task['aborted'] = True
        task['status'] = 'done'

        _orig = _rec.has_real_round
        _rec.has_real_round = lambda msg: False   # neuter the guard
        try:
            persist_task_result(task)
        finally:
            _rec.has_real_round = _orig

        persisted = _read_persisted_tool_rounds(self.conv_id)
        self.assertIsNone(
            persisted,
            'NEUTER expected the turn to be dropped (reproducing the bug) once '
            'has_real_round no longer protects it — but rounds survived, so the '
            'guard check is not actually what saves them')

    def test_content_bearing_abort_still_persists(self):
        """CONTROL: the SAME abort but WITH content persists fine — proving the
        write path works and isolating the empty-content guard as the only
        difference between survive and vanish."""
        from lib.tasks_pkg.manager import persist_task_result

        task = self._make_task()
        N = 4
        task['toolRounds'] = _mk_done_rounds(N)
        task['content'] = 'Here is what I found so far.'   # <- the only change
        task['thinking'] = ''
        task['error'] = None
        task['finishReason'] = 'aborted'
        task['aborted'] = True
        task['status'] = 'done'

        persist_task_result(task)

        persisted = _read_persisted_tool_rounds(self.conv_id)
        self.assertIsNotNone(persisted, 'content-bearing abort should persist the turn')
        done = [r for r in (persisted or []) if r.get('status') == 'done']
        self.assertEqual(len(done), N,
                         f'content-bearing abort should persist all {N} rounds; '
                         f'found {len(done)}')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_abort_toolonly_rounds_persist.__main__', init_schema=False)
    unittest.main(verbosity=2)
