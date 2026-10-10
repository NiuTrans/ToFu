"""Projection edits keep the terminal segment aligned with ``content``.

The HTTP mutation contract is covered by the turn-command tests.  This file
pins the pure projection rule once, without recreating the removed positional
message API or a second persistence harness.
"""

import pytest


pytestmark = pytest.mark.unit


def _segments_with_terminal():
    return [
        {
            "type": "thinking",
            "text": "reasoning",
            "deliverable": False,
            "llmRound": 0,
        },
        {
            "type": "text",
            "text": "let me check",
            "deliverable": False,
            "llmRound": 0,
        },
        {
            "type": "tool_use",
            "id": "tc1",
            "name": "read_files",
            "input": "{}",
            "llmRound": 0,
            "result": {"content": "file body", "status": "done"},
        },
        {
            "type": "text",
            "text": "ORIGINAL ANSWER",
            "deliverable": True,
            "terminal": True,
        },
    ]


def test_edit_rewrites_only_the_terminal_deliverable():
    from lib.tasks_pkg.segments import apply_edited_deliverable, derive_content

    original = _segments_with_terminal()
    updated = apply_edited_deliverable(original, "EDITED ANSWER")

    assert updated is not None
    assert derive_content(updated) == "EDITED ANSWER"
    assert updated[:-1] == original[:-1]
    assert updated[-1]["text"] == "EDITED ANSWER"
    assert original[-1]["text"] == "ORIGINAL ANSWER"


def test_edit_noops_when_projection_is_already_aligned_or_empty():
    from lib.tasks_pkg.segments import apply_edited_deliverable

    assert apply_edited_deliverable(None, "answer") is None
    assert apply_edited_deliverable([], "answer") is None
    assert apply_edited_deliverable(
        _segments_with_terminal(), "ORIGINAL ANSWER"
    ) is None


def test_edit_appends_a_missing_terminal_deliverable():
    from lib.tasks_pkg.segments import apply_edited_deliverable, derive_content

    partial = _segments_with_terminal()[:-1]
    updated = apply_edited_deliverable(partial, "NEW ANSWER")

    assert updated is not None
    assert updated[:-1] == partial
    assert updated[-1]["terminal"] is True
    assert updated[-1]["deliverable"] is True
    assert derive_content(updated) == "NEW ANSWER"
    assert apply_edited_deliverable(partial, "") is None


def test_edit_isolates_malformed_segment_siblings():
    from lib.tasks_pkg.segments import apply_edited_deliverable, derive_content

    updated = apply_edited_deliverable(
        [None, "broken", *_segments_with_terminal()], "SAFE ANSWER")

    assert updated is not None
    assert derive_content(updated) == "SAFE ANSWER"
    assert all(isinstance(segment, dict) for segment in updated)
