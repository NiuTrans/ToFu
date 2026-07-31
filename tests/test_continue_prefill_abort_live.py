#!/usr/bin/env python3
"""tests/test_continue_prefill_abort_live.py — prove (or falsify) the LIVE
manual-Stop → persist → /api/chat/continue chain, and pin the segments-missing
content fallback.

WHY
---
P1b added ``'aborted'`` to ``RESUMABLE_FINISH_REASONS`` so a manual Stop on a
no-tools turn can resume via lossless assistant prefill. But
``resume_prefill_from_segments`` opens with ``if not segments: return None`` —
so the fix only fires when the stopped message actually PERSISTED a segment
timeline carrying a terminal deliverable. Owner challenge: prove the whole
chain end-to-end instead of unit-testing the verdict/prefill with hand-built
segments, and plug the hole for any message whose segments are missing/thin
(legacy rows, assemble failure, the superseded-fragment stamp path, a
frontend-race sync) — those silently fell back to a full regeneration, the
exact "button says Continue, actually regenerates" lie.

TWO SUITES
----------
* ``TestLiveAbortChain`` (integration): drives the REAL persist path
  (``create_task`` + ``persist_task_result`` against a real sqlite
  ``conversations`` row), reads the settled message back from the DB, then
  replays the EXACT ``/api/chat/continue`` decision order
  (``scan_continue_checkpoint`` → ``resume_prefill_from_segments``) and asserts
  a no-tools manually-Stopped turn resumes via PREFILL, not regenerate.

* ``TestSegmentsMissingContentFallback`` (failing-first for the hole): a
  message that has content + a resumable finishReason but NO segments must
  fall back to the plain ``content`` channel as the prefill (the
  ``deliverable_text`` precedent) — never silently regenerate.

Run standalone:
    TOFU_DB_BACKEND=sqlite TOFU_DB_PATH=/tmp/abort_live.db \
        python3 tests/test_continue_prefill_abort_live.py
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TOFU_DB_BACKEND', 'sqlite')
os.environ.setdefault('TOFU_DB_PATH', '/tmp/abort_live_unittest.db')

import pytest

pytestmark = pytest.mark.unit

PARTIAL = 'The three causes are: (1)'       # the mid-answer tail a Stop leaves
CAPABLE = 'gpt-4o'                          # model_supports_assistant_prefill → True
CLAUDE = 'claude-sonnet-4-5'               # prefill fail-closed


def _continue_decision(msg, model, *, with_content_fallback):
    """Replay the EXACT /api/chat/continue branch order (routes/chat.py).

    Returns 'checkpoint' | 'prefill' | 'regenerate'. ``with_content_fallback``
    mirrors the route passing ``content=msg['content']`` into
    ``resume_prefill_from_segments`` (the hole fix); the pre-fix route did not.
    """
    from lib.chat.turn_builder import scan_continue_checkpoint
    from lib.tasks_pkg.segments import resume_prefill_from_segments
    scan = scan_continue_checkpoint(msg)
    if scan is not None:
        return 'checkpoint'
    kw = {'finish_reason': msg.get('finishReason') or ''}
    if with_content_fallback:
        kw['content'] = msg.get('content') or ''
    prefill = resume_prefill_from_segments(msg.get('segments'), model, **kw)
    return 'prefill' if prefill else 'regenerate'


class TestLiveAbortChain(unittest.TestCase):
    """Full-chain proof: a no-tools turn manually Stopped while latest persists
    a resumable segment timeline, and Continue replays to a PREFILL resume."""

    def setUp(self):
        from lib.database import init_db
        init_db()
        self.conv_id = 'cv-abortlive-' + str(id(self))
        self._cleanup()
        self._seed_conv()

    def tearDown(self):
        self._cleanup()

    def _seed_conv(self):
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert
        messages = [
            {'role': 'user', 'content': 'list the three causes', 'timestamp': 1, '_msgId': 'u0'},
            # The in-flight assistant bubble the stream was writing into.
            {'role': 'assistant', 'content': '', 'thinking': '', 'toolRounds': [],
             'timestamp': 2, '_msgId': 'a0'},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        now_ms = int(time.time() * 1000)
        upsert(db, CONVERSATIONS, {
            'id': self.conv_id, 'user_id': 1, 'title': 'abort-live',
            'messages': json_dumps_pg(messages), 'msg_count': len(messages),
            'created_at': now_ms, 'updated_at': now_ms,
        }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                        'created_at', 'updated_at'], retry=True)
        db.commit()

    def _read_last_assistant(self):
        from lib.database import DOMAIN_CHAT, get_thread_db
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute('SELECT messages FROM conversations WHERE id=? AND user_id=1',
                         (self.conv_id,)).fetchone()
        msgs = json.loads(row[0]) if row and isinstance(row[0], str) else (row[0] if row else [])
        for m in reversed(msgs):
            if m.get('role') == 'assistant':
                return m
        return None

    def _cleanup(self):
        from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
        try:
            db = get_thread_db(DOMAIN_CHAT)
            db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1',
                                  (self.conv_id,))
            db.commit()
        except Exception:
            pass

    def test_manual_stop_no_tools_persists_resumable_segments_and_prefills(self):
        from lib.tasks_pkg.manager import create_task, persist_task_result
        task = create_task(self.conv_id, [{'role': 'user', 'content': 'list the three causes'}],
                           {'model': CAPABLE})
        # The orchestrator's abort finalize leaves the task in exactly this shape:
        # terminal prose in content, no tool rounds, finishReason='aborted'.
        task['content'] = PARTIAL
        task['thinking'] = ''
        task['aborted'] = True
        task['status'] = 'aborted'
        task['finishReason'] = 'aborted'
        persist_task_result(task)

        msg = self._read_last_assistant()
        self.assertIsNotNone(msg, 'assistant message vanished after persist')
        # 1. The settled message carries the manual-Stop terminal reason.
        self.assertEqual(msg.get('finishReason'), 'aborted',
                         'abort path did not stamp finishReason=aborted on the message')
        # 2. The partial prose survived.
        self.assertEqual((msg.get('content') or ''), PARTIAL)
        # 3. THE CRUX — the segment timeline actually persisted, with a
        #    terminal deliverable (without it resume_prefill is dead code).
        segs = msg.get('segments') or []
        deliverable = [s for s in segs
                       if s.get('type') == 'text' and s.get('terminal') and s.get('deliverable')]
        self.assertTrue(deliverable,
                        'no terminal deliverable segment persisted — prefill resume is dead code')
        self.assertEqual(deliverable[0].get('text'), PARTIAL)
        # 4. Replay the continue decision: prefill, not regenerate.
        self.assertEqual(_continue_decision(msg, CAPABLE, with_content_fallback=True),
                         'prefill',
                         'live chain fell back to regenerate despite persisted segments')


class TestSegmentsMissingContentFallback(unittest.TestCase):
    """The hole: a content-bearing, resumable message with NO usable segment
    timeline must fall back to the content channel — never silently regenerate."""

    def test_aborted_message_without_segments_resumes_via_content(self):
        # The superseded-fragment / legacy / assemble-failure shape: content +
        # finishReason='aborted', but segments is absent.
        msg = {'role': 'assistant', 'content': PARTIAL, 'thinking': '',
               'toolRounds': [], 'finishReason': 'aborted'}
        self.assertEqual(_continue_decision(msg, CAPABLE, with_content_fallback=True),
                         'prefill',
                         'segments-missing aborted turn silently regenerates — the hole')

    def test_length_message_without_segments_resumes_via_content(self):
        msg = {'role': 'assistant', 'content': PARTIAL, 'finishReason': 'length'}
        self.assertEqual(_continue_decision(msg, CAPABLE, with_content_fallback=True),
                         'prefill')

    def test_content_fallback_fail_closed_for_claude(self):
        msg = {'role': 'assistant', 'content': PARTIAL, 'finishReason': 'aborted'}
        self.assertEqual(_continue_decision(msg, CLAUDE, with_content_fallback=True),
                         'regenerate',
                         'content fallback must stay fail-closed for Claude (no prefill)')

    def test_content_fallback_not_applied_to_clean_finish(self):
        # A settled (clean-stop) message must NOT resume from content.
        msg = {'role': 'assistant', 'content': PARTIAL, 'finishReason': 'stop'}
        self.assertEqual(_continue_decision(msg, CAPABLE, with_content_fallback=True),
                         'regenerate',
                         'content fallback wrongly resumed a clean-stop (settled) turn')

    def test_empty_content_still_regenerates(self):
        msg = {'role': 'assistant', 'content': '', 'finishReason': 'aborted'}
        self.assertEqual(_continue_decision(msg, CAPABLE, with_content_fallback=True),
                         'regenerate',
                         'empty turn must keep regenerating (nothing to resume)')

    def test_pre_fix_route_without_content_kwarg_still_regenerates(self):
        """Documents the hole precisely: the PRE-FIX route did NOT pass content,
        so a segments-missing aborted turn always regenerated. This stays red-
        equivalent (asserts regenerate) to prove the fallback is what closes it."""
        msg = {'role': 'assistant', 'content': PARTIAL, 'finishReason': 'aborted'}
        self.assertEqual(_continue_decision(msg, CAPABLE, with_content_fallback=False),
                         'regenerate')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_continue_prefill_abort_live.__main__', init_schema=False)
    unittest.main(verbosity=2)
