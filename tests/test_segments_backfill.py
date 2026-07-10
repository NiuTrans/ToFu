"""Regression: segment-timeline segments must SURVIVE a client-strip PUT AND be
recoverable for turns persisted before the fix — durably, not display-only.

WHY
---
Segment-timeline delivery (epic pt_cb8f98b0cb9b47fb) has three moving parts:

  1. save_conv PRESERVE-MERGE — keeps segments in the messages column for turns
     that re-sync after the fix (tested in test_api_integration.py).
  2. GET-path BACKSTOP — rehydrates segments from task_results, DISPLAY-ONLY
     (tested in test_api_integration.py).
  3. BACKFILL MIGRATION — PERSISTS task_results.segments into the messages
     column so the recovery survives task_results retention/cleanup. Without it,
     the backstop's recovery has an expiry date (once task rows are reaped, a
     pre-fix conversation renders grouped forever). This is the "a write-path
     fix is incomplete without a backfill" lesson from the conv-OOM work.

The backstop and the migration share ONE fill core
(lib/conversations/segments_backfill.py). This suite pins that core + the
migration's reuse of it, each with a neuter that flips the assertion.
"""

from __future__ import annotations

import importlib.util
import json
import os

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_migration():
    path = os.path.join(HERE, '_migrate_backfill_segments_from_task_results.py')
    spec = importlib.util.spec_from_file_location('_mig_seg', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _thin_segments():
    """The thin persisted render form task_results.segments stores."""
    return [
        {'type': 'thinking', 'text': 'reason', 'deliverable': False, 'llmRound': 0},
        {'type': 'text', 'text': 'searching', 'deliverable': False, 'llmRound': 0},
        {'type': 'tool_use', 'id': 'tc1', 'name': 'web_search', 'input': '{}',
         'llmRound': 0, 'result': {'content': 'hit', 'status': 'done'}},
        {'type': 'text', 'text': 'answer', 'deliverable': True, 'terminal': True},
    ]


def _messages_missing_segments():
    return [
        {'role': 'user', 'content': 'q', '_msgId': 'u1'},
        {'role': 'assistant', 'content': 'answer', '_msgId': 'a1', '_taskId': 'task-1',
         'toolRounds': [{'toolCallId': 'tc1', 'toolName': 'web_search',
                         'status': 'done', 'toolContent': 'hit'}]},
    ]


# ══════════════════════════════════════════════════════════════════════
#  SHARED FILL CORE (lib/conversations/segments_backfill.py)
# ══════════════════════════════════════════════════════════════════════

def test_collect_taskids_only_segmentless_assistant():
    from lib.conversations.segments_backfill import collect_taskids_needing_segments
    msgs = [
        {'role': 'user', 'content': 'q', '_taskId': 'tu'},                 # user → skip
        {'role': 'assistant', '_taskId': 't1'},                            # needs fill
        {'role': 'assistant', '_taskId': 't2', 'segments': [{'type': 'text'}]},  # has → skip
        {'role': 'assistant', 'content': 'x'},                             # no _taskId → skip
    ]
    need = collect_taskids_needing_segments(msgs)
    assert set(need.keys()) == {'t1'}, f'only the segment-less assistant w/ _taskId qualifies: {need!r}'


def test_parse_segments_json_shape_guard():
    from lib.conversations.segments_backfill import parse_segments_json
    assert parse_segments_json(None) is None
    assert parse_segments_json('null') is None
    assert parse_segments_json('') is None
    assert parse_segments_json('[]') is None            # empty list → None
    assert parse_segments_json('not json{') is None
    assert parse_segments_json('[{"type":"text"}]') == [{'type': 'text'}]
    assert parse_segments_json([{'type': 'text'}]) == [{'type': 'text'}]  # already decoded


def test_fill_messages_with_segments_splices_by_taskid():
    from lib.conversations.segments_backfill import (
        collect_taskids_needing_segments, fill_messages_with_segments,
    )
    msgs = _messages_missing_segments()
    need = collect_taskids_needing_segments(msgs)
    filled = fill_messages_with_segments(need, {'task-1': json.dumps(_thin_segments())})
    assert filled == 1
    asst = msgs[1]
    assert isinstance(asst['segments'], list) and len(asst['segments']) == 4
    assert [s['type'] for s in asst['segments']] == ['thinking', 'text', 'tool_use', 'text']


def test_fill_idempotent_no_double_fill():
    """A message already carrying segments is never re-filled — this is what
    makes the migration safe to run twice."""
    from lib.conversations.segments_backfill import (
        collect_taskids_needing_segments, fill_messages_with_segments,
    )
    msgs = _messages_missing_segments()
    need = collect_taskids_needing_segments(msgs)
    fill_messages_with_segments(need, {'task-1': json.dumps(_thin_segments())})
    # Second pass: collect now finds nothing (segments present).
    need2 = collect_taskids_needing_segments(msgs)
    assert need2 == {}, 'a filled message must not be re-collected (idempotent)'
    filled2 = fill_messages_with_segments(need2, {'task-1': json.dumps(_thin_segments())})
    assert filled2 == 0


def test_fill_no_source_leaves_segmentless():
    """No task_results source → message stays segment-less (→ grouped render),
    never a crash."""
    from lib.conversations.segments_backfill import (
        collect_taskids_needing_segments, fill_messages_with_segments,
    )
    msgs = _messages_missing_segments()
    need = collect_taskids_needing_segments(msgs)
    filled = fill_messages_with_segments(need, {})  # no rows
    assert filled == 0
    assert not msgs[1].get('segments')


def test_fill_core_neuter_flips():
    """NEUTER of the fill core: monkeypatch the shape guard to reject everything
    → nothing fills, proving parse_segments_json is the load-bearing dependency."""
    import lib.conversations.segments_backfill as B
    msgs = _messages_missing_segments()
    need = B.collect_taskids_needing_segments(msgs)
    orig = B.parse_segments_json
    try:
        B.parse_segments_json = lambda raw: None  # neuter: always reject
        filled = B.fill_messages_with_segments(need, {'task-1': json.dumps(_thin_segments())})
        assert filled == 0, 'neuter did not bite: fill should be a no-op when the guard rejects'
        assert not msgs[1].get('segments')
    finally:
        B.parse_segments_json = orig


# ══════════════════════════════════════════════════════════════════════
#  MIGRATION reuses the shared core (single source of truth)
# ══════════════════════════════════════════════════════════════════════

def test_migration_reuses_shared_fill_core_no_reimplementation():
    """The migration must import the shared fill primitives, not re-implement
    the "which message needs filling" / shape-guard logic (single source of
    truth with the GET-path backstop — a divergent copy would drift)."""
    mig = _load_migration()
    import lib.conversations.segments_backfill as B
    assert mig.collect_taskids_needing_segments is B.collect_taskids_needing_segments
    assert mig.fill_messages_with_segments is B.fill_messages_with_segments


def test_get_route_reuses_shared_fill_core():
    """The GET-path backstop must also bind the SAME shared primitives, so the
    route and the migration can never diverge."""
    import routes.conversations as R
    import lib.conversations.segments_backfill as B
    assert R.collect_taskids_needing_segments is B.collect_taskids_needing_segments
    assert R.fill_messages_with_segments is B.fill_messages_with_segments


def test_migration_as_list_coerces():
    """_as_list tolerates both a JSONB list and a JSON-text messages column."""
    mig = _load_migration()
    assert mig._as_list([{'a': 1}]) == [{'a': 1}]
    assert mig._as_list('[{"a":1}]') == [{'a': 1}]
    assert mig._as_list('not json{') is None
