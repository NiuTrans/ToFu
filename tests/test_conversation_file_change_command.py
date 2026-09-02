"""Stable Turn commands own file-change undo/redo projection state."""

from __future__ import annotations

from copy import deepcopy

import pytest


pytestmark = pytest.mark.unit


def _service(monkeypatch, *, mutate):
    import lib.conversation_sync.command_service as module
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.turn_lifecycle import LifecycleConflict

    state = {
        "conversationRevision": 7,
        "turn": {
            "conversationId": "conv-a",
            "turnId": "turn-a",
            "currentAttemptId": "attempt-a",
            "projectionRevision": 3,
            "projection": {
                "content": "done",
                "fileChanges": {
                    "blockId": "file-changes",
                    "taskId": "task-a",
                    "count": 1,
                    "state": "applied",
                    "files": [{"path": "a.txt", "action": "created"}],
                },
            },
        },
        "writes": [],
    }

    def get_turn(_conversation_id, _turn_id, *, user_id):
        assert user_id == 41
        return deepcopy(state["turn"])

    def update_turn(
        _conversation_id,
        _turn_id,
        *,
        projection,
        expected_projection_revision,
        user_id,
    ):
        assert user_id == 41
        current_revision = state["turn"]["projectionRevision"]
        if expected_projection_revision != current_revision:
            raise LifecycleConflict(
                "stale_projection", "stale", deepcopy(state["turn"])
            )
        state["turn"]["projection"] = deepcopy(projection)
        state["turn"]["projectionRevision"] += 1
        state["conversationRevision"] += 1
        state["writes"].append(deepcopy(projection["fileChanges"]))
        return {
            "conversationRevision": state["conversationRevision"],
            "turn": deepcopy(state["turn"]),
        }

    monkeypatch.setattr(module, "get_turn", get_turn)
    monkeypatch.setattr(module, "update_turn_projection", update_turn)
    monkeypatch.setattr(
        module,
        "get_conversation_revision",
        lambda _conversation_id, *, user_id: state["conversationRevision"],
    )
    monkeypatch.setattr(
        module,
        "get_attempt",
        lambda _attempt_id, *, user_id: {"taskId": "fallback-task"},
    )
    return ConversationTurnCommandService(
        build_user_message=lambda payload, config, conv_id, user_id: payload,
        was_aborted_after=lambda *args: False,
        start_task=lambda *args: ("unused", None),
        mutate_file_changes=mutate,
    ), state


def test_undo_and_redo_are_two_phase_idempotent_turn_commands(monkeypatch):
    effects = []

    def mutate(operation, task_id, conversation_id, user_id):
        effects.append((operation, task_id, conversation_id, user_id))
        return {"ok": True, "undone": 1} if operation == "undo" else {
            "ok": True, "redone": 1
        }

    service, state = _service(monkeypatch, mutate=mutate)
    undo = service.mutate_turn_file_changes(
        "conv-a",
        "turn-a",
        41,
        {"commandId": "undo-1", "expectedProjectionRevision": 3},
        operation="undo",
    ).value

    assert [write["state"] for write in state["writes"]] == [
        "undoing", "undone"
    ]
    assert undo["turn"]["projection"]["fileChanges"]["state"] == "undone"
    assert undo["effect"] == {"ok": True, "undone": 1}
    assert effects == [("undo", "task-a", "conv-a", 41)]

    replay = service.mutate_turn_file_changes(
        "conv-a",
        "turn-a",
        41,
        {"commandId": "undo-1", "expectedProjectionRevision": 3},
        operation="undo",
    ).value
    assert replay["idempotentReplay"] is True
    assert effects == [("undo", "task-a", "conv-a", 41)]

    current_revision = state["turn"]["projectionRevision"]
    redo = service.mutate_turn_file_changes(
        "conv-a",
        "turn-a",
        41,
        {"commandId": "redo-1", "expectedProjectionRevision": current_revision},
        operation="redo",
    ).value
    assert [write["state"] for write in state["writes"][-2:]] == [
        "redoing", "applied"
    ]
    assert redo["turn"]["projection"]["fileChanges"]["state"] == "applied"
    assert effects[-1] == ("redo", "task-a", "conv-a", 41)


def test_failed_file_effect_returns_projection_to_visible_source_state(monkeypatch):
    def fail(*_args):
        raise ValueError("workspace changed since generation")

    service, state = _service(monkeypatch, mutate=fail)
    with pytest.raises(ValueError, match="workspace changed"):
        service.mutate_turn_file_changes(
            "conv-a",
            "turn-a",
            41,
            {"commandId": "undo-failed", "expectedProjectionRevision": 3},
            operation="undo",
        )

    assert [write["state"] for write in state["writes"]] == [
        "undoing", "applied"
    ]
    block = state["turn"]["projection"]["fileChanges"]
    assert block["commandId"] == "undo-failed"
    assert block["error"] == "workspace changed since generation"


def test_file_command_rejects_untyped_or_wrong_state_turns(monkeypatch):
    service, state = _service(
        monkeypatch, mutate=lambda *_args: {"ok": True}
    )
    state["turn"]["projection"].pop("fileChanges")
    with pytest.raises(ValueError, match="no authoritative"):
        service.mutate_turn_file_changes(
            "conv-a", "turn-a", 41,
            {"commandId": "undo-x", "expectedProjectionRevision": 3},
            operation="undo",
        )
