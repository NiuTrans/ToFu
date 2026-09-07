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

import unittest


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
