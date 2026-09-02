"""Black-box domain-behavior coverage for the project BOARD in Sidecar mode.

The legacy-mode behavior suite is ``tests/test_project_board.py`` (it seeds
the in-process legacy DB directly).  This module proves the *Sidecar* branch —
today only covered at the storage-op contract level — drives the same public
domain behaviors end to end, which is the de-risking step that lets the legacy
fallback branch be removed later.

Each test uses a distinct ``project_path`` so the module-scoped shared sidecar
(``tests/_chat_sidecar.py``) needs no per-test cleanup.
"""

from __future__ import annotations

import pytest

_AUDIT_SYNTHETIC_REPO_PATHS = {'lib/a.py', 'lib/z.py'}

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("chat_sidecar")]
pytest_plugins = ("tests._chat_sidecar",)

from lib.conversations.project_board import (  # noqa: E402
    answer_task,
    block_task,
    claim_task,
    complete_task,
    delete_task,
    post_task,
    read_board,
)

from lib.storage import get_storage_client  # noqa: E402
from lib.storage.errors import StorageError  # noqa: E402
from tests._seed import seed_conversation  # noqa: E402


_REMOTE_TOKEN = "remote:agent-A:myapp"


def _mk_sidecar_conv(conv_id: str, project_path: str = ""):
    result = seed_conversation(
        conv_id,
        title=conv_id,
        settings={"projectPath": project_path},
        created_at=1,
        updated_at=1,
    )
    assert result["applied"] is True


@pytest.fixture
def _no_post_dispatch(monkeypatch):
    import lib.conversations.project_dispatch as project_dispatch

    monkeypatch.setattr(project_dispatch, "on_epic_posted", lambda *args, **kwargs: 0)


def test_post_then_read_counts_open():
    r = post_task("/sb/p", "cA", "Build the widget", user_id=1)
    assert r["ok"] and r["id"].startswith("pt_")
    board = read_board("/sb/p", user_id=1)
    assert board["open"] == 1 and board["claimed"] == 0 and board["done"] == 0
    assert board["tasks"][0]["title"] == "Build the widget"
    assert board["tasks"][0]["status"] == "open"


def test_complete_moves_to_done():
    tid = post_task("/sb/c", "cA", "epic", user_id=1)["id"]
    assert complete_task("/sb/c", "cA", tid, user_id=1)["ok"]
    board = read_board("/sb/c", user_id=1)
    assert board["done"] == 1 and board["open"] == 0


def test_post_merges_remote_write_set_token(_no_post_dispatch):
    _mk_sidecar_conv("sb-remote-post", _REMOTE_TOKEN)

    tid = post_task(
        "/sb/remote-post",
        "sb-remote-post",
        "remote epic",
        user_id=1,
        write_set=["lib/a.py"],
    )["id"]

    task = read_board("/sb/remote-post", user_id=1)["tasks"][0]
    assert task["id"] == tid
    assert task["write_set"] == ["lib/a.py", _REMOTE_TOKEN]


def test_claim_merges_remote_write_set_token(_no_post_dispatch):
    _mk_sidecar_conv("sb-remote-poster")
    _mk_sidecar_conv("sb-remote-claimer", _REMOTE_TOKEN)
    tid = post_task(
        "/sb/remote-claim",
        "sb-remote-poster",
        "shared epic",
        user_id=1,
        write_set=["lib/z.py"],
    )["id"]

    result = claim_task("/sb/remote-claim", "sb-remote-claimer", tid, user_id=1)

    assert result["ok"] is True
    task = read_board("/sb/remote-claim", user_id=1)["tasks"][0]
    assert task["owner_conv_id"] == "sb-remote-claimer"
    assert task["write_set"] == ["lib/z.py", _REMOTE_TOKEN]


def test_claim_marks_claimed_and_owner():
    tid = post_task("/sb/claim", "cA", "claim me", user_id=1)["id"]
    assert claim_task("/sb/claim", "cB", tid, user_id=1)["ok"]
    board = read_board("/sb/claim", user_id=1)
    assert board["claimed"] == 1
    assert board["tasks"][0]["owner_conv_id"] == "cB"


def test_claim_refresh_with_changed_context_renews_same_lease():
    """A claim refresh is a NEW lifecycle invocation and receipt.

    Regression for the sidecar ``command_id was reused for a different
    request`` failure: the first human claim and the Brain's dispatched
    re-claim/lease refresh share the same epic but differ in ttl/dispatched.
    They must both execute under distinct command IDs; the refresh keeps the
    same owner and cannot erase the dispatched marker.
    """
    tid = post_task("/sb/claim-refresh", "cA", "refresh me", user_id=1)["id"]
    first = claim_task("/sb/claim-refresh", "cA", tid, user_id=1, ttl_ms=60_000)
    second = claim_task(
        "/sb/claim-refresh", "cA", tid, user_id=1, ttl_ms=120_000, dispatched=True
    )

    assert first["ok"] and second["ok"]
    assert second["lease_expires_at"] >= first["lease_expires_at"]
    assert second.get("refreshed") is True
    task = read_board("/sb/claim-refresh", user_id=1)["tasks"][0]
    assert task["owner_conv_id"] == "cA"
    assert task["dispatched"] is True


def test_delete_open_epic_removes_it():
    tid = post_task("/sb/del", "cA", "junk epic", user_id=1)["id"]
    assert delete_task("/sb/del", "cA", tid, user_id=1)["ok"]
    assert read_board("/sb/del", user_id=1)["tasks"] == []


def test_delete_refused_while_active_dependent():
    a = post_task("/sb/deldep", "cA", "dep epic", user_id=1)["id"]
    post_task("/sb/deldep", "cA", "waiting epic", user_id=1, depends_on=[a])
    res = delete_task("/sb/deldep", "cA", a, user_id=1)
    assert not res["ok"] and res["error"] == "has_dependents"
    assert len(read_board("/sb/deldep", user_id=1)["tasks"]) == 2


def test_answer_clears_block_question(_no_post_dispatch, monkeypatch):
    monkeypatch.setattr(
        'lib.conversations.project_dispatch._drain_idle_target',
        lambda *args, **kwargs: None,
    )
    _mk_sidecar_conv("sb-answer-target", "/sb/ans")
    tid = post_task("/sb/ans", "sb-answer-target", "gated epic", user_id=1)["id"]
    blk = block_task(
        "/sb/ans",
        "sb-answer-target",
        tid,
        "need a human",
        user_id=1,
        question="which env?",
    )
    assert blk["ok"]
    board = read_board("/sb/ans", user_id=1)
    assert board["blocked"] == 1
    ans = answer_task("/sb/ans", "sb-answer-target", tid, "prod", user_id=1)
    assert ans["ok"]
    board = read_board("/sb/ans", user_id=1)
    # The answer IMMEDIATELY re-dispatches the epic (on_epic_answered): it
    # leaves the blocked bucket by being re-claimed by the answering conv,
    # not by parking back in open.
    assert board["blocked"] == 0 and board["claimed"] == 1
    task = board["tasks"][0]
    assert task["owner_conv_id"] == "sb-answer-target"
    assert task["human_answer"] == "prod"
    # Legacy read_board projects an empty block_question as None.
    assert not task["block_question"]


def test_atomic_dispatch_commits_claim_and_queue_together(_no_post_dispatch):
    _mk_sidecar_conv("sb-atomic-target", "/sb/atomic")
    task_id = post_task("/sb/atomic", "sb-atomic-target", "atomic epic", user_id=1)[
        "id"
    ]
    result = get_storage_client(write=True).command(
        "board.dispatch",
        {
            "project_path": "/sb/atomic",
            "task_id": task_id,
            "conv_id": "sb-atomic-target",
            "user_id": 1,
            "queue_id": "sb-atomic-queue",
            "message": {"text": "kickoff", "boardTaskId": task_id},
            "config": {"projectPath": "/sb/atomic"},
            "priority": 50,
            "created_at_ms": 123,
        },
        "sb-atomic-dispatch",
    )
    assert result["ok"] is True
    assert result["queueId"] == "sb-atomic-queue"
    task = read_board("/sb/atomic", user_id=1)["tasks"][0]
    assert task["status"] == "claimed"
    assert task["owner_conv_id"] == "sb-atomic-target"
    queue = get_storage_client().query(
        "queue.list", {"conv_id": "sb-atomic-target", "user_id": 1}
    )
    assert [row["queueId"] for row in queue] == ["sb-atomic-queue"]


def test_atomic_dispatch_rolls_back_claim_when_queue_rejects_owner(_no_post_dispatch):
    task_id = post_task(
        "/sb/atomic-rollback", "missing-target", "rollback epic", user_id=1
    )["id"]
    with pytest.raises(StorageError):
        get_storage_client(write=True).command(
            "board.dispatch",
            {
                "project_path": "/sb/atomic-rollback",
                "task_id": task_id,
                "conv_id": "missing-target",
                "user_id": 1,
                "queue_id": "sb-atomic-rejected",
                "message": {"text": "kickoff", "boardTaskId": task_id},
                "config": {"projectPath": "/sb/atomic-rollback"},
                "priority": 50,
                "created_at_ms": 123,
            },
            "sb-atomic-rollback",
        )
    task = read_board("/sb/atomic-rollback", user_id=1)["tasks"][0]
    assert task["status"] == "open"
    assert task["owner_conv_id"] == ""
