"""Projection patches for bounded windows stay proportional to what changed."""

from __future__ import annotations

import json
import random

import pytest

from lib.turn_projection_patch import apply_projection_patch, build_projection_patch


pytestmark = pytest.mark.unit


def _row(index: int) -> dict:
    return {
        "id": f"status:{index}", "phase": "tool", "detail": f"step {index}",
        "detailKey": "phase.tool", "count": 1, "tStart": 1000 + index,
        "tEnd": 1001 + index, "lastObservedAt": 1001 + index,
    }


def _projection(rows: list[dict], entries: list[dict]) -> dict:
    return {
        "content": "answer",
        "timingTrace": {"statusHistory": rows, "statusDroppedCount": 0},
        "activityTimeline": {"entries": entries, "droppedCount": 0},
        "toolRounds": [{"name": "rg", "status": "done"}],
    }


def _patch(before: dict, after: dict) -> dict:
    patch = build_projection_patch(before, after, base_revision=7, target_revision=8)
    assert apply_projection_patch(before, patch) == after
    return patch


def test_full_window_eviction_removes_the_head_instead_of_rewriting_rows():
    rows = [_row(index) for index in range(128)]
    entries = [{"id": f"activity:{index}", "seq": index} for index in range(128)]
    updated_tail = {**rows[-1], "count": 2, "lastObservedAt": 9999}
    before = _projection(rows, entries)
    after = _projection(
        [*rows[1:-1], updated_tail, _row(128)],
        [*entries[2:], {"id": "activity:128", "seq": 128}, {"id": "activity:129", "seq": 129}],
    )

    operations = _patch(before, after)["operations"]

    assert {"op": "remove", "path": ["timingTrace", "statusHistory", 0]} in operations
    assert operations.count(
        {"op": "remove", "path": ["activityTimeline", "entries", 0]}) == 2
    assert {"op": "append", "path": ["timingTrace", "statusHistory"],
            "value": [_row(128)]} in operations
    # Two evictions, one in-place update (two fields) and two appends; the
    # index-aligned diff rewrote every field of all 256 retained rows.
    assert len(operations) == 7


def test_eviction_behind_a_retained_head_removes_only_the_evicted_rows():
    # A window that retains its head row (an error, a compaction receipt)
    # evicts behind it; index alignment re-sent every later row per event.
    entries = [{"id": f"activity:{index}", "seq": index} for index in range(128)]
    newest = {"id": "activity:128", "seq": 128}
    before = _projection([], entries)

    one = _patch(before, _projection([], [entries[0], *entries[2:], newest]))
    two = _patch(before, _projection([], [entries[0], *entries[3:], newest]))

    assert one["operations"] == [
        {"op": "remove", "path": ["activityTimeline", "entries", 1]},
        {"op": "append", "path": ["activityTimeline", "entries"], "value": [newest]},
    ]
    assert two["operations"] == [
        {"op": "remove", "path": ["activityTimeline", "entries", 1]},
        {"op": "remove", "path": ["activityTimeline", "entries", 1]},
        {"op": "append", "path": ["activityTimeline", "entries"], "value": [newest]},
    ]


def test_append_only_and_in_place_list_updates_keep_index_alignment():
    rows = [_row(index) for index in range(4)]
    before = _projection(rows, [])
    after = _projection([*rows[:-1], {**rows[-1], "count": 3}, _row(4)], [])

    operations = _patch(before, after)["operations"]

    assert not any(operation["op"] == "remove" for operation in operations)
    assert operations == [
        {"op": "set", "path": ["timingTrace", "statusHistory", 3, "count"], "value": 3},
        {"op": "append", "path": ["timingTrace", "statusHistory"], "value": [_row(4)]},
    ]


def test_rotation_keeps_index_alignment_when_eviction_would_resend_more():
    before = {"items": ["a", "b", "c", "d"]}
    after = {"items": ["d", "a", "b", "c"]}

    operations = _patch(before, after)["operations"]

    assert [operation["op"] for operation in operations] == ["set"] * 4


def test_randomized_list_edits_round_trip_without_growing_the_patch():
    generator = random.Random(20261031)
    alphabet = ["a", "b", "c", {"k": 1}, {"k": 2}, [1], [1, 2]]
    for _ in range(4000):
        before = [generator.choice(alphabet) for _ in range(generator.randrange(0, 9))]
        after = list(before)
        for _ in range(generator.randrange(0, 4)):
            edit = generator.randrange(4)
            if edit == 0 and after:
                del after[0]
            elif edit == 1:
                after.append(generator.choice(alphabet))
            elif edit == 2 and after:
                after[generator.randrange(len(after))] = generator.choice(alphabet)
            elif edit == 3:
                after.insert(generator.randrange(len(after) + 1), generator.choice(alphabet))
        patch = _patch({"items": before}, {"items": after})
        aligned_items = sum(1 for old, new in zip(before, after) if old != new)
        aligned_items += max(0, len(after) - len(before))
        removed = sum(1 for operation in patch["operations"] if operation["op"] == "remove")
        assert removed <= aligned_items, json.dumps({"before": before, "after": after})
