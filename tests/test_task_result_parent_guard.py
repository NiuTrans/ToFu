"""Owner and parent fences for durable executor checkpoints."""

from __future__ import annotations

import time

import pytest

from tests._seed import seed_conversation


pytest_plugins = ("tests._chat_sidecar",)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("chat_sidecar")]


def _task(conversation_id: str, *, user_id=1, inline=False) -> dict:
    task = {
        "id": f"task-result-{time.time_ns()}",
        "convId": conversation_id,
        "_userId": user_id,
        "created_at": time.time(),
    }
    if inline:
        task["_inline_messages"] = True
    return task


def _write(task: dict) -> bool:
    from lib.tasks_pkg.manager._persist import _upsert_task_row

    return _upsert_task_row(
        task,
        task["convId"],
        content="checkpoint",
        thinking="",
        status="done",
        error_json=None,
        tr_json=None,
        meta_json=None,
    )


def _record(task_id: str):
    from lib.storage import get_storage_client

    return get_storage_client().query(
        "record.get", {"namespace": "task_results", "key": task_id}
    )


def test_conversation_backed_checkpoint_requires_its_owner_parent():
    conversation_id = f"task-parent-{time.time_ns()}"
    seed_conversation(conversation_id, user_id=7)

    owned = _task(conversation_id, user_id=7)
    foreign = _task(conversation_id, user_id=8)

    assert _write(owned) is True
    assert _record(owned["id"])["value"]["user_id"] == 7
    assert _write(foreign) is False
    assert _record(foreign["id"]) is None


def test_deleted_or_missing_parent_cannot_be_resurrected_by_late_checkpoint():
    task = _task(f"missing-parent-{time.time_ns()}")

    assert _write(task) is False
    assert _record(task["id"]) is None


def test_inline_checkpoint_has_no_conversation_parent():
    task = _task("", user_id=9, inline=True)

    assert _write(task) is True
    assert _record(task["id"])["value"]["user_id"] == 9


def test_checkpoint_without_owner_fails_closed():
    task = _task("", inline=True)
    task.pop("_userId")

    with pytest.raises(ValueError, match="task result checkpoint"):
        _write(task)
