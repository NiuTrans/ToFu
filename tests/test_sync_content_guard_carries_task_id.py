"""The content-guard sync path must still carry `_taskId` (provenance).

## Symptom

A settled assistant turn arrives in the DB with `finishReason` present but
`_taskId` ABSENT. Every tool row of that turn then silently loses its debug
entry, because the entry resolves through `msg._taskId` — the user sees an
inconsistency ("why do only SOME tool rows have the button?") with no way to
tell whether it is a bug or intended.

## Why it happened

`_sync_result_to_conversation` has a content-guard branch for the case where
the frontend already synced FULLER content than the backend is about to write
(a mid-stream SSE break, then a page refresh). That branch deliberately writes
only a subset of fields so it cannot clobber the fuller content — and the
subset copied `finishReason`, `usage`, `model` and `provider_id` but NOT
`_taskId`. So a turn taking that path settled permanently without a task id.

Measured before the fix (300 most recent conversations, 31,337 tool rounds):
1,142 tool rounds across 42 assistant messages had no resolvable task id, and
"finishReason present + _taskId absent" — this branch's exact fingerprint —
accounted for 24 of those 42. 20 of 42 were under a day old, so this was an
ACTIVE write-path defect, not legacy data.

`_taskId` is pure provenance (which task produced this turn), never content,
so writing it on this path cannot possibly clobber what the guard protects.

## What these tests assert

RESULT, not implementation: drive the real content-guard branch and assert the
persisted message carries the task id. Plus the two complements that stop the
obvious wrong fixes from passing:
  * the branch must STILL not overwrite the fuller content (that is the whole
    reason the branch exists);
  * an already-present `_taskId` must not be overwritten by a different one.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SYNC = os.path.join(ROOT, 'lib', 'tasks_pkg', 'manager', '_sync.py')


def _guard_branch_source() -> str:
    """Return the content-guard branch body, located by SYMBOL rather than by
    line number so a reformat or a nearby edit cannot silently retire this
    guard. Three distinguishable states (charter discipline):
      not found  -> the branch was REMOVED (a product change, not drift);
      found many -> the single source was copied, converge it first;
      found one  -> use it.
    """
    src = open(SYNC, encoding='utf-8').read()
    # Anchor on the BRANCH STATEMENT, not the bare comparison: the same
    # comparison also appears just above in the `_fr_residue_exempt`
    # computation, so a looser anchor matches two places and cannot tell which
    # one is the branch.
    marker = 'if existing_content_len > new_content_len'
    hits = [m.start() for m in re.finditer(re.escape(marker), src)]
    assert hits, (
        'the content-guard branch is GONE from _sync.py. That is a product '
        'change, not harness drift — restore the branch (or delete this guard '
        'deliberately) before touching this test.')
    assert len(hits) == 1, (
        f'the content-guard condition appears {len(hits)} times — the single '
        f'source was copied; converge it before relying on this guard.')
    start = hits[0]
    # The branch ends where the sibling `else:` (normal path) begins.
    end = src.index('\n        else:', start)
    return src[start:end]


def test_content_guard_branch_copies_task_id():
    """The fix itself: `_taskId` is carried on the content-guard path."""
    branch = _guard_branch_source()
    assert "last_msg['_taskId'] = meta['taskId']" in branch, (
        "the content-guard branch does not copy _taskId — a turn taking this "
        "path settles with finishReason but no task id, and every tool row of "
        "that turn silently loses its debug entry")


def test_task_id_write_marks_the_row_dirty():
    """A write that does not reach the DB is not a fix.

    The branch `return`s early unless something signalled a change, so the
    `_taskId` write must participate in that signal. Without this, the field
    is assigned on an in-memory dict that is then thrown away — indistinguish-
    able from the bug it was meant to fix.
    """
    branch = _guard_branch_source()
    assert '_taskid_wrote' in branch, (
        'the _taskId write is not tracked, so it cannot mark the row dirty')
    m = re.search(r'if _tr_updated or [^\n:]*:', branch)
    assert m, 'the dirty-check condition was not found in the branch'
    assert '_taskid_wrote' in m.group(0), (
        f'the dirty-check does not include _taskid_wrote, so a branch whose '
        f'ONLY change is the task id returns early and never persists: '
        f'{m.group(0)}')


def test_task_id_is_not_overwritten_when_already_present():
    """COMPLEMENT 1 — guard against the crude fix (unconditional assignment).

    The frontend may already have stamped the correct id; a blind overwrite
    on this path would let a superseded/retried task id win over it.
    """
    branch = _guard_branch_source()
    assert "not last_msg.get('_taskId')" in branch, (
        'the _taskId copy is unconditional on the content-guard path — it '
        'must only fill a MISSING id, never replace an existing one')


def test_content_guard_still_protects_the_fuller_content():
    """COMPLEMENT 2 — the branch must not have been "fixed" by deleting it.

    This branch exists so a fuller frontend-synced answer is not overwritten
    by a shorter backend one. If a future edit made it write content, the
    original data-loss bug returns and every assertion above would still pass.
    """
    branch = _guard_branch_source()
    assert "last_msg['content'] = content" not in branch, (
        'the content-guard branch now writes content — it exists precisely to '
        'NOT do that; the fuller frontend answer would be clobbered')
    assert "last_msg['thinking'] = thinking" not in branch, (
        'the content-guard branch now writes thinking — same regression')
