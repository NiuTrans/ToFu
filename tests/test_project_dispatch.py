"""Black-box behavior contract for autonomous project dispatch.

The suite deliberately knows nothing about storage tables or source layout.
It drives the public Board API against a real Sidecar and inspects only
semantic Board and Queue projections.  These are the invariants that make
dispatch safe: eligible work is selected, ineligible work is skipped, and a
successful kickoff commits its claim and durable queue item exactly once.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("chat_sidecar")]
pytest_plugins = ("tests._chat_sidecar",)

from lib.conversations.project_board import (  # noqa: E402
    claim_task,
    complete_task,
    post_task,
    read_board,
)
from lib.conversations.project_dispatch import (  # noqa: E402
    BRAIN_DISPATCH_MARKER,
    dispatch_epic,
    select_dispatchable,
    sweep_dispatch,
)
from lib.storage import get_storage_client  # noqa: E402
from tests._seed import seed_conversation  # noqa: E402


USER_ID = 41


@pytest.fixture(autouse=True)
def _keep_kickoffs_durable(monkeypatch):
    """Keep each test at the dispatch boundary instead of spawning agents."""
    import lib.conversations.project_dispatch as project_dispatch

    monkeypatch.setattr(
        project_dispatch,
        "on_epic_posted",
        lambda *args, **kwargs: 0,
    )
    monkeypatch.setattr(
        project_dispatch,
        "_drain_idle_target",
        lambda *args, **kwargs: None,
    )


def _seed_target(conv_id: str, project_path: str) -> None:
    result = seed_conversation(
        conv_id,
        user_id=USER_ID,
        title=conv_id,
        settings={"projectPath": project_path},
        created_at=1,
        updated_at=1,
    )
    assert result["applied"] is True


def _queue_rows(conv_id: str) -> list[dict]:
    return get_storage_client().query(
        "queue.list",
        {"conv_id": conv_id, "user_id": USER_ID},
    ) or []


def _remove_queue_row(conv_id: str, queue_id: str) -> None:
    result = get_storage_client(write=True).command(
        "queue.remove",
        {
            "conv_id": conv_id,
            "queue_id": queue_id,
            "user_id": USER_ID,
        },
        f"dispatch-test-remove:{uuid.uuid4().hex}",
    )
    assert result["removed"] is True


def test_select_includes_open_epic_after_dependencies_finish():
    path = "/dispatch/select-ready"
    dependency = post_task(path, "select-ready", "dependency", user_id=USER_ID)[
        "id"
    ]
    assert complete_task(path, "select-ready", dependency, user_id=USER_ID)["ok"]
    epic = post_task(
        path,
        "select-ready",
        "dependent work",
        user_id=USER_ID,
        depends_on=[dependency],
    )["id"]

    selected = select_dispatchable(path, user_id=USER_ID)

    assert epic in {task["id"] for task in selected}


def test_select_excludes_epic_with_unfinished_dependency():
    path = "/dispatch/select-waiting"
    dependency = post_task(path, "select-waiting", "dependency", user_id=USER_ID)[
        "id"
    ]
    epic = post_task(
        path,
        "select-waiting",
        "dependent work",
        user_id=USER_ID,
        depends_on=[dependency],
    )["id"]

    selected_ids = {
        task["id"] for task in select_dispatchable(path, user_id=USER_ID)
    }

    assert dependency in selected_ids
    assert epic not in selected_ids


def test_select_excludes_live_claim():
    path = "/dispatch/select-claimed"
    epic = post_task(path, "claim-author", "claimed work", user_id=USER_ID)["id"]
    assert claim_task(path, "claim-owner", epic, user_id=USER_ID)["ok"]

    selected = select_dispatchable(path, user_id=USER_ID)

    assert epic not in {task["id"] for task in selected}


def test_dispatch_atomically_claims_and_enqueues_owned_target():
    path = "/dispatch/atomic-success"
    target = "dispatch-atomic-target"
    _seed_target(target, path)
    epic_id = post_task(path, target, "atomic work", user_id=USER_ID)["id"]
    epic = select_dispatchable(path, user_id=USER_ID)[0]

    result = dispatch_epic(path, epic, target, user_id=USER_ID)

    assert result["ok"] is True
    task = read_board(path, user_id=USER_ID)["tasks"][0]
    assert task["id"] == epic_id
    assert task["status"] == "claimed"
    assert task["owner_conv_id"] == target
    rows = _queue_rows(target)
    assert len(rows) == 1
    assert rows[0]["queueId"] == result["queueId"]
    assert rows[0]["kind"] == "workflow_step"
    assert rows[0]["payload"][BRAIN_DISPATCH_MARKER] is True
    assert rows[0]["payload"]["boardTaskId"] == epic_id
    assert epic_id not in {
        task["id"] for task in select_dispatchable(path, user_id=USER_ID)
    }


def test_dispatch_refuses_epic_claimed_by_another_conversation():
    path = "/dispatch/claim-conflict"
    target = "dispatch-conflict-target"
    _seed_target(target, path)
    epic_id = post_task(path, "claim-author", "exclusive work", user_id=USER_ID)[
        "id"
    ]
    assert claim_task(path, "other-owner", epic_id, user_id=USER_ID)["ok"]

    result = dispatch_epic(
        path,
        {"id": epic_id, "title": "exclusive work"},
        target,
        user_id=USER_ID,
    )

    assert result["ok"] is False
    assert _queue_rows(target) == []
    task = read_board(path, user_id=USER_ID)["tasks"][0]
    assert task["owner_conv_id"] == "other-owner"


def test_completion_event_dispatches_newly_unblocked_epic():
    path = "/dispatch/dependency-event"
    target = "dispatch-dependency-target"
    _seed_target(target, path)
    dependency = post_task(path, target, "dependency", user_id=USER_ID)["id"]
    dependent = post_task(
        path,
        target,
        "dependent work",
        user_id=USER_ID,
        depends_on=[dependency],
    )["id"]

    assert complete_task(path, target, dependency, user_id=USER_ID)["ok"]

    task = next(
        row
        for row in read_board(path, user_id=USER_ID)["tasks"]
        if row["id"] == dependent
    )
    assert task["status"] == "claimed"
    assert task["owner_conv_id"] == target
    assert [row["payload"]["boardTaskId"] for row in _queue_rows(target)] == [
        dependent
    ]


def test_sweep_dispatches_first_epic_on_a_cold_board():
    path = "/dispatch/cold-start"
    target = "dispatch-cold-target"
    _seed_target(target, path)
    epic_id = post_task(path, target, "first work", user_id=USER_ID)["id"]

    dispatched = sweep_dispatch(path, user_id=USER_ID)

    assert dispatched == 1
    task = read_board(path, user_id=USER_ID)["tasks"][0]
    assert task["id"] == epic_id and task["status"] == "claimed"
    assert len(_queue_rows(target)) == 1


def test_second_sweep_cannot_redispatch_after_queue_is_drained():
    path = "/dispatch/sweep-idempotency"
    target = "dispatch-idempotent-target"
    _seed_target(target, path)
    post_task(path, target, "single work item", user_id=USER_ID)

    first_count = sweep_dispatch(path, user_id=USER_ID)
    first_rows = _queue_rows(target)
    assert len(first_rows) == 1
    _remove_queue_row(target, first_rows[0]["queueId"])
    second_count = sweep_dispatch(path, user_id=USER_ID)

    assert first_count == 1
    assert second_count == 0
    assert _queue_rows(target) == []


def test_sweep_skips_a_busy_target(monkeypatch):
    import lib.conversations.project_dispatch as project_dispatch

    path = "/dispatch/busy-target"
    target = "dispatch-busy-target"
    _seed_target(target, path)
    post_task(path, target, "wait for idle", user_id=USER_ID)
    monkeypatch.setattr(
        project_dispatch,
        "_conv_has_live_task",
        lambda conv_id, *, user_id: conv_id == target and user_id == USER_ID,
    )

    dispatched = project_dispatch.sweep_dispatch(path, user_id=USER_ID)

    assert dispatched == 0
    assert _queue_rows(target) == []


def test_sweep_honors_per_pass_cap():
    path = "/dispatch/bounded-sweep"
    target = "dispatch-bounded-target"
    _seed_target(target, path)
    for index in range(5):
        post_task(path, target, f"work {index}", user_id=USER_ID)

    dispatched = sweep_dispatch(path, user_id=USER_ID, max_per_sweep=2)

    assert dispatched == 2
    assert len(_queue_rows(target)) == 2
