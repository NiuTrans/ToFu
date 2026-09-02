"""Project context projections reuse one owner-scoped storage snapshot."""

from __future__ import annotations

import pytest

from lib.tasks_pkg.context_composer._models import ComposeRequest
from lib.tasks_pkg.context_composer._providers import _project_blocks

pytestmark = pytest.mark.unit


def test_board_injection_can_render_an_already_read_snapshot(monkeypatch):
    import lib.conversations.project_board as project_board

    snapshot = {
        "tasks": [{
            "id": "pt_snapshot",
            "kind": "epic",
            "title": "Own the parser migration",
            "status": "claimed",
            "owner_conv_id": "sibling-conversation",
        }],
    }
    monkeypatch.setattr(
        project_board,
        "read_board",
        lambda *args, **kwargs: pytest.fail("snapshot renderer reread storage"),
    )

    rendered = project_board.render_board_injection_block(
        "/tmp/project",
        current_conv_id="current-conversation",
        user_id=7,
        board_snapshot=snapshot,
    )

    assert "pt_snapshot" in rendered
    assert "sibling-conversation" in rendered


def test_project_blocks_share_board_and_digest_snapshots(monkeypatch):
    import lib.conversations.project_board as project_board
    import lib.conversations.project_charter as project_charter
    import lib.conversations.project_summary as project_summary
    import lib.conversations.project_watch as project_watch

    board_snapshot = {
        "tasks": [{
            "id": "pt_active",
            "kind": "epic",
            "title": "Active work",
            "status": "claimed",
            "owner_conv_id": "sibling-conversation",
        }],
    }
    board_reads = []
    rendered_snapshots = []
    projection_calls = []
    projection = project_summary.ProjectDigestProjection(
        text="This project has 1 related conversation(s).",
        entries=({
            "id": "related-conversation",
            "title": "Related work",
            "summary": "The sibling shipped the parser.",
        },),
    )

    monkeypatch.setattr(
        project_charter, "render_charter_injection_block", lambda *a, **k: ""
    )
    monkeypatch.setattr(
        project_watch, "render_goals_injection_block", lambda *a, **k: ""
    )

    def _read_board(*args, **kwargs):
        board_reads.append((args, kwargs))
        return board_snapshot

    def _render_board(*args, board_snapshot=None, **kwargs):
        rendered_snapshots.append(board_snapshot)
        return "[PROJECT BOARD] one active claim"

    def _build_projection(*args, **kwargs):
        projection_calls.append((args, kwargs))
        return projection

    monkeypatch.setattr(project_board, "read_board", _read_board)
    monkeypatch.setattr(
        project_board, "render_board_injection_block", _render_board
    )
    monkeypatch.setattr(
        project_summary,
        "build_project_digest_projection",
        _build_projection,
    )

    task = {}
    request = ComposeRequest(
        project_path="/tmp/project",
        project_enabled=True,
        conv_id="current-conversation",
        user_id=7,
        tool_names=frozenset({"list_conversations"}),
        task=task,
    )
    blocks = _project_blocks(request, "parser")

    assert len(board_reads) == 1
    assert rendered_snapshots == [board_snapshot]
    assert len(projection_calls) == 1
    assert projection_calls[0][1]["conv_tools_available"] is True
    assert projection_calls[0][1]["query"] == "parser"
    assert {block.id: block.content for block in blocks}["project_board"]
    related = task["_relatedConversations"]
    assert related == {
        "count": 1,
        "items": [{
            "id": "related-conversation",
            "title": "Related work",
            "summary": "The sibling shipped the parser.",
        }],
        "toolsAvailable": True,
    }
    related["items"][0]["title"] = "task-local mutation"
    assert projection.entries[0]["title"] == "Related work"
