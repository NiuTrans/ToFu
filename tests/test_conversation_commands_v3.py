"""Outcome contracts for the sole turn-mutation HTTP surface.

Conversation Sync v3 is a thin adapter over the shared command service. These
tests exercise command idempotency, lifecycle authority, storage errors, and
the complete mutation surface without retaining a second protocol adapter.
"""

from __future__ import annotations

import pytest


pytestmark = [pytest.mark.api, pytest.mark.auth_mode("open")]
pytest_plugins = ('tests._chat_sidecar',)
CONVERSATION_ID = "conv-command-api"


def _registered_start(task_id: str):
    def start(*args, **kwargs):
        kwargs["on_task_registered"](task_id)
        return task_id, None

    return start


@pytest.fixture()
def conversation_command_db(chat_sidecar):
    from tests._seed import delete_conversation, seed_conversation

    delete_conversation(CONVERSATION_ID)
    seed_conversation(CONVERSATION_ID, title='Commands', messages=[])
    try:
        yield
    finally:
        delete_conversation(CONVERSATION_ID)


def _create_path(conversation_id: str = CONVERSATION_ID) -> str:
    return f"/api/v3/conversations/{conversation_id}/turns"


def _append_settled(flask_client, command_id: str, actor: str, content: str):
    projection = {"content": content}
    if actor == "planner":
        from lib.plan_contract import proposed_plan_document
        proposed_plan = proposed_plan_document(content=content)
        if proposed_plan is not None:
            projection["proposedPlan"] = proposed_plan
    response = flask_client.post(f"{_create_path()}/settled", json={
        "commandId": command_id,
        "actor": actor,
        "kind": "plan_fixture",
        "projection": projection,
    })
    assert response.status_code == 200
    return response.get_json()["turn"]


def test_busy_lane_queue_returns_real_pair_and_cancel_deletes_it(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    monkeypatch.setattr(
        task_start_runtime,
        "start_conversation_attempt_executor",
        _registered_start("active-queue-fence"),
    )
    active = flask_client.post(_create_path(), json={
        "commandId": "queue-fence-active",
        "inputTurn": {"content": "hold the lane"},
        "config": {"model": "gpt-4o"},
    })
    assert active.status_code == 200

    queued_response = flask_client.post(_create_path(), json={
        "commandId": "queue-real-pair",
        "inputTurn": {"content": "restore me on cancel"},
        "message": {"text": "restore me on cancel"},
        "config": {"model": "gpt-4o"},
        "injectMode": "queue",
    })
    assert queued_response.status_code == 200
    queued = queued_response.get_json()
    assert queued["queued"] is True
    assert queued["submittedTurn"]["presentationId"] == "queue-real-pair:input"
    assert queued["turn"]["presentationId"] == "queue-real-pair:output"
    assert queued["attempt"]["queueBinding"] == {
        "queueId": queued["queueId"], "state": "pending",
    }
    assert queued["queueItem"]["inputTurnId"] == queued["submittedTurn"]["turnId"]
    assert queued["queueItem"]["outputTurnId"] == queued["turn"]["turnId"]

    snapshot_response = flask_client.get(
        f"/api/v3/conversations/{CONVERSATION_ID}/sync"
    )
    assert snapshot_response.status_code == 200
    snapshot = snapshot_response.get_json()
    assert snapshot["scope"] == {
        "kind": "conversation",
        "ownerId": 1,
        "threadId": CONVERSATION_ID,
    }
    snapshot_queue_item = next(
        item for item in snapshot["queueItems"]
        if item["queueId"] == queued["queueId"]
    )
    assert snapshot_queue_item["inputTurnId"] == queued["submittedTurn"]["turnId"]
    assert snapshot_queue_item["outputTurnId"] == queued["turn"]["turnId"]
    assert snapshot_queue_item["attemptId"] == queued["attempt"]["attemptId"]

    cancelled_response = flask_client.delete(
        f"/api/v3/conversations/{CONVERSATION_ID}/queue/{queued['queueId']}"
    )
    assert cancelled_response.status_code == 200
    cancelled = cancelled_response.get_json()
    assert cancelled["cancelled"] is True
    assert cancelled["inputTurn"]["projection"]["content"] == (
        "restore me on cancel"
    )
    assert set(cancelled["deletedTurnIds"]) == {
        queued["submittedTurn"]["turnId"], queued["turn"]["turnId"],
    }
    replay = flask_client.delete(
        f"/api/v3/conversations/{CONVERSATION_ID}/queue/{queued['queueId']}"
    )
    assert replay.status_code == 200
    assert replay.get_json()["deletedTurnIds"] == cancelled["deletedTurnIds"]


def test_regenerate_rebinds_plan_turn_identity_to_normalized_target_mode(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import get_turn, read_events, record_task_event
    import lib.conversation_sync.task_start as task_start_runtime

    task_ids = iter(("plan-task", "standard-task"))
    monkeypatch.setattr(
        task_start_runtime,
        "start_conversation_attempt_executor",
        lambda _conv_id, _config, **kwargs: (
            (task_id := next(task_ids)),
            kwargs["on_task_registered"](task_id),
        )[0:2],
    )
    created_response = flask_client.post(_create_path(), json={
        "commandId": "plan-create",
        "inputTurn": {"content": "design this"},
        "config": {"model": "gpt-4o", "planMode": True},
    })
    assert created_response.status_code == 200
    created = created_response.get_json()
    assert (created["turn"]["actor"], created["turn"]["kind"]) == (
        "planner", "plan"
    )
    first_attempt_id = created["attempt"]["attemptId"]
    assert record_task_event({
        "_attemptId": first_attempt_id,
        "_userId": 1,
        "id": "plan-task",
        "status": "done",
        "finishReason": "stop",
        "content": "the plan",
        "thinking": "",
        "toolRounds": [],
        "config": {"model": "gpt-4o", "planMode": True},
    }, {"type": "done", "finishReason": "stop"})

    turn_id = created["turn"]["turnId"]
    settled = get_turn(CONVERSATION_ID, turn_id, user_id=1)
    regenerated_response = flask_client.post(
        f"{_create_path()}/{turn_id}/attempts",
        json={
            "commandId": "plan-to-standard",
            "operation": "regenerate",
            "expectedProjectionRevision": settled["projectionRevision"],
            "config": {"model": "gpt-4o", "planMode": False},
        },
    )
    assert regenerated_response.status_code == 200
    regenerated = regenerated_response.get_json()
    assert (regenerated["turn"]["actor"], regenerated["turn"]["kind"]) == (
        "assistant", "reply"
    )
    persisted = get_turn(CONVERSATION_ID, turn_id, user_id=1)
    assert (persisted["actor"], persisted["kind"]) == ("assistant", "reply")
    attempt_events = read_events(
        regenerated["attempt"]["attemptId"], user_id=1
    )
    first_payload = attempt_events[0]["payload"]
    assert first_payload["turnState"]["actor"] == "assistant"
    assert first_payload["turnState"]["kind"] == "reply"


def test_create_retry_settle_and_same_turn_regenerate(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import (
        build_api_messages,
        list_turns,
        read_events,
        record_task_event,
    )
    import lib.conversation_sync.task_start as task_start_runtime

    task_ids = iter(("command-task-1", "command-task-2"))
    starts = []

    def fake_start(conv_id, config, **kwargs):
        task_id = next(task_ids)
        starts.append((task_id, dict(config)))
        kwargs["on_task_registered"](task_id)
        return task_id, None

    monkeypatch.setattr(task_start_runtime, "start_conversation_attempt_executor", fake_start)
    body = {
        "commandId": "lost-ack-command",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    }
    first_response = flask_client.post(_create_path(), json=body)
    assert first_response.status_code == 200
    first = first_response.get_json()
    assert first["turn"]["status"] == "pending"
    assert first["submittedTurn"]["actor"] == "human"

    submitted_turn = first["submittedTurn"]
    assert first["attempt"]["operation"] == "generate"
    assert "_needsStart" not in first
    attempt_id = first["attempt"]["attemptId"]
    turn_id = first["turn"]["turnId"]

    assert [event["type"] for event in read_events(attempt_id, user_id=1)] == [
        "status_changed", "status_changed",
    ]
    retry = flask_client.post(_create_path(), json=body).get_json()
    assert retry["idempotentReplay"] is True
    assert retry["turn"]["turnId"] == turn_id
    assert retry["attempt"]["attemptId"] == attempt_id
    assert len(starts) == 1
    assert starts[0][1]["excludeLast"] is True
    context = build_api_messages(
        CONVERSATION_ID, turn_id, {"excludeLast": True}, user_id=1)
    assert context[-1]["role"] == "user"
    assert context[-1]["content"] == "hello"

    task = {
        "_attemptId": attempt_id,
        "_userId": 1,
        "id": "command-task-1",
        "status": "done",
        "finishReason": "stop",
        "content": "answer",
        "thinking": "",
        "toolRounds": [],
        "model": "gpt-4o",
        "config": {"model": "gpt-4o"},
    }
    assert record_task_event(task, {"type": "done", "finishReason": "stop"})
    latest = next(
        turn for turn in list_turns(CONVERSATION_ID, user_id=1)["turns"]
        if turn["turnId"] == turn_id
    )
    live_input_projection = {
        "content": "hello",
        "contextSnapshot": {
            "blockId": "turn-context",
            "snapshot": {
                "roots": [
                    {"path": "/workspace/original", "short": "original", "ro": False},
                    {"path": "/workspace/added", "short": "added", "ro": False},
                ],
                "model": "gpt-4o",
            },
        },
    }
    stale_input = flask_client.post(
        f"{_create_path()}/{turn_id}/attempts",
        json={
            "commandId": "regenerate-stale-input-command",
            "operation": "regenerate",
            "expectedProjectionRevision": latest["projectionRevision"],
            "inputUpdate": live_input_projection,
            "expectedInputProjectionRevision": (
                submitted_turn["projectionRevision"] - 1
            ),
            "config": {"model": "gpt-4o"},
        },
    )
    assert stale_input.status_code == 409

    regenerated = flask_client.post(
        f"{_create_path()}/{turn_id}/attempts",
        json={
            "commandId": "regenerate-command",
            "operation": "regenerate",
            "expectedProjectionRevision": latest["projectionRevision"],
            "inputUpdate": live_input_projection,
            "expectedInputProjectionRevision": submitted_turn["projectionRevision"],
            "config": {"model": "gpt-4o"},
        },
    ).get_json()
    assert regenerated["turn"]["turnId"] == turn_id
    assert regenerated["attempt"]["attemptId"] != attempt_id
    assert regenerated["turn"]["status"] == "pending"
    regenerated_input = regenerated["submittedTurn"]["projection"]
    assert regenerated_input["content"] == live_input_projection["content"]
    assert regenerated_input["contextSnapshot"] == live_input_projection["contextSnapshot"]
    assert regenerated["submittedTurn"]["projectionRevision"] == (
        submitted_turn["projectionRevision"] + 1
    )
    assert len(starts) == 2

    task.update(status="running", content="stale overwrite")
    assert record_task_event(task, {"type": "delta", "content": "stale"}) is False


def test_stale_attempt_returns_latest_authoritative_turn(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", _registered_start("conflict-task"))
    created = flask_client.post(_create_path(), json={
        "commandId": "create-conflict-turn",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    }).get_json()
    turn = created["turn"]
    response = flask_client.post(
        f"{_create_path()}/{turn['turnId']}/attempts",
        json={
            "commandId": "stale-operation",
            "operation": "regenerate",
            "expectedProjectionRevision": turn["projectionRevision"] - 1,
            "config": {},
        },
    )
    assert response.status_code == 409
    payload = response.get_json()
    assert payload["error"]["kind"] == "stale_projection"
    assert payload["latestTurn"]["turnId"] == turn["turnId"]


def test_start_failure_is_terminal_actionable_and_idempotent(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    starts = []

    def fail_start(*args, **kwargs):
        starts.append(True)
        return None, object()

    monkeypatch.setattr(task_start_runtime, "start_conversation_attempt_executor", fail_start)
    body = {
        "commandId": "start-failure-command",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    }
    response = flask_client.post(_create_path(), json=body)
    assert response.status_code == 500
    payload = response.get_json()
    assert payload["error"]["kind"] == "task_start_failed"
    assert payload["error"]["retryable"] is True
    assert payload["error"]["hint"]
    assert payload["latestTurn"]["status"] == "failed"
    assert payload["latestTurn"]["settlement"]["error"]["kind"] == \
        "task_start_failed"

    retry = flask_client.post(_create_path(), json=body).get_json()
    assert retry["idempotentReplay"] is True
    assert retry["turn"]["turnId"] == payload["latestTurn"]["turnId"]
    assert starts == [True]


def test_abort_targets_named_attempt_and_commits_terminal_event(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import get_turn, read_events
    import lib.conversation_sync.task_start as task_start_runtime

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", _registered_start("abort-task"))
    created = flask_client.post(_create_path(), json={
        "commandId": "abort-only-this-attempt",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    }).get_json()
    attempt_id = created["attempt"]["attemptId"]
    turn_id = created["turn"]["turnId"]

    response = flask_client.post(f"/api/v3/attempts/{attempt_id}/abort")
    assert response.status_code == 200
    assert response.get_json()["attemptId"] == attempt_id
    turn = get_turn(CONVERSATION_ID, turn_id, user_id=1)
    assert turn["status"] == "interrupted"
    assert turn["settlement"]["cause"] == "user_abort"
    terminal = read_events(attempt_id, user_id=1)[-1]
    assert terminal["type"] == "terminal_settlement"
    assert terminal["payload"]["status"] == "interrupted"


def test_first_command_creates_conversation_and_turns_atomically(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import list_turns
    import lib.conversation_sync.task_start as task_start_runtime

    fresh_id = "fresh-command-conversation"
    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", _registered_start("first-task"))
    from tests._seed import delete_conversation
    delete_conversation(fresh_id)

    response = flask_client.post(_create_path(fresh_id), json={
        "commandId": "fresh-first-command",
        "inputTurn": {"content": "first"},
        "config": {"model": "gpt-4o"},
        "conversation": {
            "allowCreate": True,
            "title": "First",
            "settings": {"model": "gpt-4o"},
            "createdAt": 123,
        },
    })
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["submittedTurn"]["ordinal"] == 0
    assert payload["turn"]["ordinal"] == 1
    assert [turn["turnId"] for turn in list_turns(fresh_id, user_id=1)["turns"]] == [
        payload["submittedTurn"]["turnId"], payload["turn"]["turnId"],
    ]


def test_settled_turn_command_creates_and_appends_typed_image_documents(
    flask_client, conversation_command_db,
):
    from lib.turn_lifecycle import list_turns
    from tests._seed import delete_conversation

    conversation_id = "settled-image-command"
    delete_conversation(conversation_id)
    path = f"{_create_path(conversation_id)}/settled"
    input_body = {
        "commandId": "image-input-command",
        "actor": "human",
        "kind": "image_generation_prompt",
        "projection": {"content": "draw a lighthouse", "timestamp": 123},
        "conversation": {
            "allowCreate": True,
            "title": "Lighthouse",
            "createdAt": 100,
            "settings": {"imageGenMode": True},
        },
    }
    try:
        created = flask_client.post(path, json=input_body)
        assert created.status_code == 200
        replay = flask_client.post(path, json=input_body)
        assert replay.status_code == 200
        assert replay.get_json()["turn"]["turnId"] == created.get_json()["turn"]["turnId"]

        output = flask_client.post(path, json={
            "commandId": "image-result-command",
            "actor": "assistant",
            "kind": "image_generation_result",
            "projection": {
                "content": "A lighthouse at dusk",
                "imageGeneration": {
                    "blockId": "image-generation",
                    "mode": "generate",
                    "status": "completed",
                    "results": [{
                        "ok": True,
                        "prompt": "draw a lighthouse",
                        "model": "image-model",
                        "imageUrl": "/generated/lighthouse.png",
                        "elapsedSeconds": 2.5,
                    }],
                },
            },
        })
        assert output.status_code == 200
        turns = list_turns(conversation_id, user_id=1)["turns"]
        assert [turn["kind"] for turn in turns] == [
            "image_generation_prompt", "image_generation_result",
        ]
        projection = turns[-1]["projection"]
        assert projection["imageGeneration"]["results"][0]["imageUrl"] == \
            "/generated/lighthouse.png"
        assert projection["segments"][-1]["blockId"] == "text:terminal"
    finally:
        delete_conversation(conversation_id)


def test_lane_and_delete_commands_use_stable_turn_identity(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import list_turns, record_task_event
    import lib.conversation_sync.task_start as task_start_runtime

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", _registered_start("lane-task"))
    created = flask_client.post(_create_path(), json={
        "commandId": "lane-parent-command",
        "inputTurn": {"content": "anchor"},
        "config": {},
    }).get_json()
    parent = created["submittedTurn"]
    lane_response = flask_client.post(
        f"{_create_path()}/{parent['turnId']}/lanes",
        json={
            "title": "Side path",
            "anchorText": "anchor",
            "expectedProjectionRevision": parent["projectionRevision"],
        },
    )
    assert lane_response.status_code == 200
    lane_id = lane_response.get_json()["lane"]["laneId"]
    deleted_lane = flask_client.delete(
        f"{_create_path()}/{parent['turnId']}/lanes/{lane_id}")
    assert deleted_lane.status_code == 200
    assert deleted_lane.get_json()["deletedLaneId"] == lane_id

    assert record_task_event({
        "_attemptId": created["attempt"]["attemptId"],
        "_userId": 1,
        "id": "lane-task",
        "status": "done",
        "finishReason": "stop",
        "content": "done",
        "thinking": "",
        "toolRounds": [],
        "config": {},
    }, {"type": "done", "finishReason": "stop"})

    turn_ids = [created["submittedTurn"]["turnId"], created["turn"]["turnId"]]
    deleted_turns = flask_client.post(
        f"{_create_path()}/delete", json={"turnIds": turn_ids})
    assert deleted_turns.status_code == 200
    assert set(deleted_turns.get_json()["deletedTurnIds"]) == set(turn_ids)
    assert list_turns(CONVERSATION_ID, user_id=1)["turns"] == []


def _settle_attempt(attempt_id: str, task_id: str, content: str = "done") -> None:
    from lib.turn_lifecycle import record_task_event

    assert record_task_event({
        "_attemptId": attempt_id,
        "_userId": 1,
        "id": task_id,
        "status": "done",
        "finishReason": "stop",
        "content": content,
        "thinking": "",
        "toolRounds": [],
        "config": {},
    }, {"type": "done", "finishReason": "stop"})


def _create_pair(flask_client, command_id: str, content: str, **extra):
    body = {
        "commandId": command_id,
        "inputTurn": {"content": content},
        "config": {},
    }
    body.update(extra)
    response = flask_client.post(_create_path(), json=body)
    assert response.status_code == 200
    return response.get_json()


def _regenerate(flask_client, turn, command_id: str):
    return flask_client.post(
        f"{_create_path()}/{turn['turnId']}/attempts",
        json={
            "commandId": command_id,
            "operation": "regenerate",
            "expectedProjectionRevision": turn["projectionRevision"],
            "config": {},
        },
    )


def test_regenerate_truncates_lane_tail_atomically(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import list_turns
    import lib.conversation_sync.task_start as task_start_runtime

    task_ids = iter(("tail-task-1", "tail-task-2", "tail-task-3", "tail-task-4"))

    def fake_start(conv_id, config, **kwargs):
        task_id = next(task_ids)
        kwargs["on_task_registered"](task_id)
        return task_id, None

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", fake_start)
    first = _create_pair(flask_client, "tail-command-1", "one")
    _settle_attempt(first["attempt"]["attemptId"], "tail-task-1")
    second = _create_pair(flask_client, "tail-command-2", "two")
    _settle_attempt(second["attempt"]["attemptId"], "tail-task-2")
    third = _create_pair(flask_client, "tail-command-3", "three")
    _settle_attempt(third["attempt"]["attemptId"], "tail-task-3")

    tail_ids = {
        second["submittedTurn"]["turnId"], second["turn"]["turnId"],
        third["submittedTurn"]["turnId"], third["turn"]["turnId"],
    }
    target = next(
        turn for turn in list_turns(CONVERSATION_ID, user_id=1)["turns"]
        if turn["turnId"] == first["turn"]["turnId"]
    )
    client = get_storage_client(write=True)
    cursor_before_regenerate = client.query(
        "turn.sync.changes",
        {"conversation_id": CONVERSATION_ID, "user_id": 1, "after": 0},
    )["head"]
    regenerated = _regenerate(flask_client, target, "tail-regenerate")
    assert regenerated.status_code == 200
    body = regenerated.get_json()
    assert body["turn"]["turnId"] == target["turnId"]
    assert body["turn"]["status"] == "pending"
    assert set(body["deletedTurnIds"]) == tail_ids
    remaining = list_turns(CONVERSATION_ID, user_id=1)["turns"]
    assert {turn["turnId"] for turn in remaining} == {
        first["submittedTurn"]["turnId"], first["turn"]["turnId"],
    }

    # Deleting tail attempts expires their old replay prefix by design. A
    # zero cursor must reset to a snapshot once that creates a gap; the live
    # client contract is the cursor it held immediately before regenerate.
    changes = client.query(
        "turn.sync.changes",
        {
            "conversation_id": CONVERSATION_ID,
            "user_id": 1,
            "after": cursor_before_regenerate,
        },
    )
    deleted_events = [
        event for event in changes["events"] if event["type"] == "turn.deleted"
    ]
    assert len(deleted_events) == 1
    assert set(deleted_events[0]["payload"]["deletedTurnIds"]) == tail_ids

    replay = _regenerate(flask_client, target, "tail-regenerate")
    assert replay.status_code == 200
    replay_body = replay.get_json()
    assert replay_body["idempotentReplay"] is True
    assert replay_body["attempt"]["attemptId"] == body["attempt"]["attemptId"]
    assert set(replay_body["deletedTurnIds"]) == tail_ids


def test_regenerate_with_live_tail_turn_fails_closed(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import list_turns
    import lib.conversation_sync.task_start as task_start_runtime

    task_ids = iter(("live-tail-task-1", "live-tail-task-2"))

    def fake_start(conv_id, config, **kwargs):
        task_id = next(task_ids)
        kwargs["on_task_registered"](task_id)
        return task_id, None

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", fake_start)
    first = _create_pair(flask_client, "live-tail-command-1", "one")
    _settle_attempt(first["attempt"]["attemptId"], "live-tail-task-1")
    second = _create_pair(flask_client, "live-tail-command-2", "two")

    target = next(
        turn for turn in list_turns(CONVERSATION_ID, user_id=1)["turns"]
        if turn["turnId"] == first["turn"]["turnId"]
    )
    response = _regenerate(flask_client, target, "live-tail-regenerate")
    assert response.status_code == 409
    assert response.get_json()["error"]["kind"] == "database_conflict"
    remaining = list_turns(CONVERSATION_ID, user_id=1)["turns"]
    assert {turn["turnId"] for turn in remaining} == {
        first["submittedTurn"]["turnId"], first["turn"]["turnId"],
        second["submittedTurn"]["turnId"], second["turn"]["turnId"],
    }
    untouched = next(
        turn for turn in remaining if turn["turnId"] == first["turn"]["turnId"]
    )
    assert untouched["currentAttemptId"] == first["attempt"]["attemptId"]
    assert untouched["status"] == "completed"


def test_regenerate_truncates_branch_lanes_rooted_in_the_tail(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.turn_lifecycle import list_turns
    import lib.conversation_sync.task_start as task_start_runtime

    task_ids = iter((
        "branch-tail-task-1", "branch-tail-task-2",
        "branch-tail-task-3", "branch-tail-task-4",
    ))

    def fake_start(conv_id, config, **kwargs):
        task_id = next(task_ids)
        kwargs["on_task_registered"](task_id)
        return task_id, None

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", fake_start)
    first = _create_pair(flask_client, "branch-tail-command-1", "one")
    _settle_attempt(first["attempt"]["attemptId"], "branch-tail-task-1")
    second = _create_pair(flask_client, "branch-tail-command-2", "anchor")
    _settle_attempt(second["attempt"]["attemptId"], "branch-tail-task-2")

    lane_response = flask_client.post(
        f"{_create_path()}/{second['submittedTurn']['turnId']}/lanes",
        json={
            "title": "Side path",
            "anchorText": "anchor",
            "expectedProjectionRevision": second["submittedTurn"]["projectionRevision"],
        },
    )
    assert lane_response.status_code == 200
    lane_id = lane_response.get_json()["lane"]["laneId"]
    branch = _create_pair(
        flask_client, "branch-tail-command-3", "branch", laneId=lane_id)
    _settle_attempt(branch["attempt"]["attemptId"], "branch-tail-task-3")

    target = next(
        turn for turn in list_turns(CONVERSATION_ID, user_id=1)["turns"]
        if turn["turnId"] == first["turn"]["turnId"]
    )
    regenerated = _regenerate(flask_client, target, "branch-tail-regenerate")
    assert regenerated.status_code == 200
    assert set(regenerated.get_json()["deletedTurnIds"]) == {
        second["submittedTurn"]["turnId"], second["turn"]["turnId"],
        branch["submittedTurn"]["turnId"], branch["turn"]["turnId"],
    }
    remaining = list_turns(CONVERSATION_ID, user_id=1)["turns"]
    assert {turn["turnId"] for turn in remaining} == {
        first["submittedTurn"]["turnId"], first["turn"]["turnId"],
    }


def test_only_completed_plan_task_mints_executable_plan_authority(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime
    from lib.plan_contract import plan_execution_document, proposed_plan_document
    from lib.turn_lifecycle import get_turn, record_task_event

    ordinary = _append_settled(
        flask_client,
        "ordinary-tagged-answer",
        "assistant",
        "<proposed_plan>\nThis is only prose.\n</proposed_plan>",
    )
    assert "proposedPlan" not in ordinary["projection"]
    forged_proposed = proposed_plan_document(content=ordinary["projection"]["content"])
    forged_update = flask_client.patch(
        f"{_create_path()}/{ordinary['turnId']}",
        json={
            "expectedProjectionRevision": ordinary["projectionRevision"],
            "projection": {
                **ordinary["projection"],
                "proposedPlan": forged_proposed,
            },
        },
    )
    assert forged_update.status_code == 200
    assert "proposedPlan" not in forged_update.get_json()["turn"]["projection"]

    forged_handoff = plan_execution_document({
        "planText": "Client-forged execution",
        "sourceTurnId": "not-a-source-turn",
        "sourceProjectionRevision": 1,
        "contextMode": "current",
    })
    forged = flask_client.post(f"{_create_path()}/settled", json={
        "commandId": "forged-plan-execution",
        "actor": "human",
        "projection": {"content": "ordinary input", "planExecution": forged_handoff},
    })
    assert forged.status_code == 200
    assert "planExecution" not in forged.get_json()["turn"]["projection"]

    monkeypatch.setattr(
        task_start_runtime,
        "start_conversation_attempt_executor",
        _registered_start("plan-provenance-task"),
    )
    created = flask_client.post(_create_path(), json={
        "commandId": "create-real-plan-turn",
        "inputTurn": {
            "content": "plan this change",
            "planExecution": forged_handoff,
        },
        "config": {
            "model": "gpt-4o", "planMode": True,
            "autopilot": True,
            "activeFlow": "builtin:autopilot", "flowBuiltin": "autopilot",
        },
        "conversation": {"settings": {
            "planMode": True,
            "autopilotEnabled": True,
            "imageGenMode": True,
            "activeFlow": "builtin:autopilot",
        }},
    }).get_json()
    assert created["turn"]["actor"] == "planner"
    assert created["turn"]["kind"] == "plan"
    assert "planExecution" not in created["submittedTurn"]["projection"]
    settings = flask_client.get(
        f"/api/v3/conversations/{CONVERSATION_ID}/sync",
    ).get_json()["settings"]
    assert settings["planMode"] is True
    assert settings["humanGuidanceEnabled"] is True
    assert settings["autopilotEnabled"] is False
    assert settings["imageGenMode"] is False
    assert settings["activeFlow"] == ""

    assert record_task_event({
        "_attemptId": created["attempt"]["attemptId"],
        "_userId": 1,
        "id": "plan-provenance-task",
        "status": "done",
        "finishReason": "stop",
        "content": (
            "Ready.\n<proposed_plan>\n## Steps\n- implement it\n"
            "</proposed_plan>"
        ),
        "thinking": "",
        "toolRounds": [],
        "config": {"planMode": True},
    }, {"type": "done", "finishReason": "stop"})

    source = get_turn(
        CONVERSATION_ID, created["turn"]["turnId"], user_id=1)
    assert source["status"] == "completed"
    assert source["projection"]["proposedPlan"]["text"] == \
        "## Steps\n- implement it"
    assert source["projection"]["proposedPlan"]["blockId"] == \
        "proposed-plan"
    preserved = flask_client.patch(
        f"{_create_path()}/{source['turnId']}",
        json={
            "expectedProjectionRevision": source["projectionRevision"],
            "projection": {
                **source["projection"],
                "translatedContent": "translated presentation",
            },
        },
    )
    assert preserved.status_code == 200
    assert preserved.get_json()["turn"]["projection"]["proposedPlan"] == \
        source["projection"]["proposedPlan"]


def test_execute_plan_fresh_context_is_exact_durable_and_idempotent(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime
    from lib.turn_lifecycle import build_api_messages

    _append_settled(
        flask_client, "plan-discussion", "human", "unrelated old discussion")
    source = _append_settled(
        flask_client,
        "plan-source",
        "planner",
        "Ready.\n\n<proposed_plan>\n## Steps\n- change the parser\n"
        "</proposed_plan>",
    )
    proposed = source["projection"]["proposedPlan"]
    starts = []

    def fake_start(conv_id, config, **kwargs):
        starts.append(dict(config))
        kwargs["on_task_registered"]("plan-execution-task")
        return "plan-execution-task", None

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", fake_start)
    body = {
        "commandId": "execute-fresh-plan",
        "expectedProjectionRevision": source["projectionRevision"],
        "planId": proposed["planId"],
        "contextMode": "fresh",
        "config": {"model": "gpt-4o", "planMode": True},
    }
    response = flask_client.post(
        f"{_create_path()}/{source['turnId']}/plan/execute", json=body)
    assert response.status_code == 200
    accepted = response.get_json()
    assert accepted["submittedTurn"]["kind"] == "plan_execution"
    handoff = accepted["submittedTurn"]["projection"]["planExecution"]
    assert handoff["sourceTurnId"] == source["turnId"]
    assert handoff["sourceProjectionRevision"] == source["projectionRevision"]
    assert handoff["planId"] == proposed["planId"]
    assert handoff["contextMode"] == "fresh"
    assert starts[0]["planMode"] is False
    assert starts[0]["humanGuidanceEnabled"] is True

    messages = build_api_messages(
        CONVERSATION_ID,
        accepted["turn"]["turnId"],
        starts[0],
        user_id=1,
    )
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "<accepted_plan_json>" in messages[0]["content"]
    assert "change the parser" in messages[0]["content"]
    assert "unrelated old discussion" not in messages[0]["content"]
    snapshot = flask_client.get(
        f"/api/v3/conversations/{CONVERSATION_ID}/sync").get_json()
    assert snapshot["settings"]["planMode"] is False

    replay = flask_client.post(
        f"{_create_path()}/{source['turnId']}/plan/execute", json=body)
    assert replay.status_code == 200
    assert replay.get_json()["idempotentReplay"] is True
    assert len(starts) == 1


def test_execute_plan_current_context_keeps_prior_turns(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime
    from lib.turn_lifecycle import build_api_messages

    _append_settled(
        flask_client, "current-discussion", "human", "keep this context marker")
    source = _append_settled(
        flask_client,
        "current-plan-source",
        "planner",
        "<proposed_plan>\nDo the current-context work.\n</proposed_plan>",
    )
    proposed = source["projection"]["proposedPlan"]
    starts = []

    def fake_start(conv_id, config, **kwargs):
        starts.append(dict(config))
        kwargs["on_task_registered"]("current-plan-task")
        return "current-plan-task", None

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", fake_start)
    accepted = flask_client.post(
        f"{_create_path()}/{source['turnId']}/plan/execute",
        json={
            "commandId": "execute-current-plan",
            "expectedProjectionRevision": source["projectionRevision"],
            "planId": proposed["planId"],
            "contextMode": "current",
            "config": {"model": "gpt-4o", "planMode": True},
        },
    ).get_json()
    messages = build_api_messages(
        CONVERSATION_ID, accepted["turn"]["turnId"], starts[0], user_id=1)
    assert messages[0]["content"] == "keep this context marker"
    assert messages[-1]["role"] == "user"
    assert "Do the current-context work." in messages[-1]["content"]


def test_execute_plan_uses_the_expanded_branch_lane_context(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime
    from lib.turn_lifecycle import build_api_messages, get_turn, record_task_event

    parent = _append_settled(
        flask_client, "branch-plan-parent", "human", "branch anchor context")
    lane_response = flask_client.post(
        f"{_create_path()}/{parent['turnId']}/lanes",
        json={
            "title": "Plan branch",
            "expectedProjectionRevision": parent["projectionRevision"],
        },
    )
    assert lane_response.status_code == 200
    lane_id = lane_response.get_json()["lane"]["laneId"]
    starts = []
    task_ids = iter(("branch-planner-task", "branch-plan-task"))

    def fake_start(conv_id, config, **kwargs):
        starts.append(dict(config))
        task_id = next(task_ids)
        kwargs["on_task_registered"](task_id)
        return task_id, None

    monkeypatch.setattr(
        task_start_runtime, "start_conversation_attempt_executor", fake_start)
    planned = flask_client.post(_create_path(), json={
        "commandId": "create-branch-plan",
        "inputTurn": {"content": "plan this branch"},
        "config": {"model": "gpt-4o", "planMode": True},
        "laneId": lane_id,
        "parentTurnId": parent["turnId"],
    }).get_json()
    assert planned["turn"]["actor"] == "planner"
    assert record_task_event({
        "_attemptId": planned["attempt"]["attemptId"],
        "_userId": 1,
        "id": "branch-planner-task",
        "status": "done",
        "finishReason": "stop",
        "content": (
            "<proposed_plan>\nExecute inside the branch.\n</proposed_plan>"
        ),
        "thinking": "",
        "toolRounds": [],
        "config": {"planMode": True},
    }, {"type": "done", "finishReason": "stop"})
    source = get_turn(
        CONVERSATION_ID, planned["turn"]["turnId"], user_id=1)
    accepted = flask_client.post(
        f"{_create_path()}/{source['turnId']}/plan/execute",
        json={
            "commandId": "execute-branch-plan",
            "expectedProjectionRevision": source["projectionRevision"],
            "planId": source["projection"]["proposedPlan"]["planId"],
            "contextMode": "current",
            "config": {"model": "gpt-4o", "planMode": True},
        },
    ).get_json()
    assert accepted["submittedTurn"]["laneId"] == lane_id
    assert accepted["turn"]["laneId"] == lane_id
    messages = build_api_messages(
        CONVERSATION_ID, accepted["turn"]["turnId"], starts[1], user_id=1)
    assert messages[0]["role"] == "user"
    assert "branch anchor context" in messages[0]["content"]
    assert "plan this branch" in messages[0]["content"]
    assert "Execute inside the branch." in messages[-1]["content"]


def test_execute_plan_rejects_a_mismatched_plan_identity_before_start(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    source = _append_settled(
        flask_client,
        "identity-plan-source",
        "planner",
        "<proposed_plan>\nUse the accepted identity.\n</proposed_plan>",
    )
    starts = []
    monkeypatch.setattr(
        task_start_runtime,
        "start_conversation_attempt_executor",
        lambda *args, **kwargs: starts.append(True),
    )
    response = flask_client.post(
        f"{_create_path()}/{source['turnId']}/plan/execute",
        json={
            "commandId": "execute-wrong-plan-id",
            "expectedProjectionRevision": source["projectionRevision"],
            "planId": "plan_000000000000000000000000",
            "contextMode": "current",
            "config": {"model": "gpt-4o"},
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["kind"] == "stale_projection"
    assert starts == []


def test_execute_plan_rejects_source_after_lane_advanced(
    flask_client, conversation_command_db, monkeypatch,
):
    import lib.conversation_sync.task_start as task_start_runtime

    source = _append_settled(
        flask_client,
        "stale-plan-source",
        "planner",
        "<proposed_plan>\nOld plan.\n</proposed_plan>",
    )
    _append_settled(
        flask_client, "later-discussion", "human", "please revise it")
    starts = []
    monkeypatch.setattr(
        task_start_runtime,
        "start_conversation_attempt_executor",
        lambda *args, **kwargs: starts.append(True),
    )
    response = flask_client.post(
        f"{_create_path()}/{source['turnId']}/plan/execute",
        json={
            "commandId": "execute-stale-plan",
            "expectedProjectionRevision": source["projectionRevision"],
            "planId": source["projection"]["proposedPlan"]["planId"],
            "contextMode": "current",
            "config": {"model": "gpt-4o"},
        },
    )
    assert response.status_code == 409
    assert response.get_json()["error"]["kind"] == "lane_advanced"
    assert starts == []


def test_storage_timeouts_keep_retryable_taxonomy(
    flask_client, conversation_command_db, monkeypatch,
):
    from lib.storage.errors import StorageError
    from routes import conversation_sync_v3 as sync_routes

    def writer_timeout(*args, **kwargs):
        raise StorageError(
            "database_timeout",
            "Storage writer acquisition timed out",
            True,
            25,
            "storage-op-test",
        )

    monkeypatch.setattr(
        sync_routes.conversation_turn_commands, "create_turn", writer_timeout)
    submit = flask_client.post(_create_path(), json={
        "commandId": "submit-under-wedge",
        "inputTurn": {"content": "hello"},
        "config": {"model": "gpt-4o"},
    })
    monkeypatch.setattr(
        sync_routes.conversation_turn_commands, "create_attempt", writer_timeout)
    attempt = flask_client.post(
        f"{_create_path()}/turn-1/attempts",
        json={
            "commandId": "retry-under-wedge",
            "operation": "regenerate",
            "expectedProjectionRevision": 1,
            "config": {"model": "gpt-4o"},
        },
    )
    for response in (submit, attempt):
        assert response.status_code == 503
        assert response.headers["Retry-After"] == "1"
        body = response.get_json()
        assert body["error"]["kind"] == "server_busy"
        assert body["error"]["storageCode"] == "database_timeout"
        assert body["error"]["retryable"] is True
        assert body["retryAfterMs"] == 25
        assert body["operationId"] == "storage-op-test"
