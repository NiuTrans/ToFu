"""HTTP smoke tests for the retained first-party surfaces.

Conversation setup uses the same Sidecar turn authority as production. There
are deliberately no archive-array PUT fixtures or incident-specific merge
tests: the route that made those states possible no longer exists.
"""

from __future__ import annotations

import re
import time

import pytest


@pytest.fixture(autouse=True)
def _no_background_llm(monkeypatch):
    import lib.tasks_pkg.spawn as task_spawn

    monkeypatch.setattr(task_spawn, "spawn_task", lambda _task: None)


def _create_turn_conversation(monkeypatch, conv_id: str, pairs: int = 1):
    from lib.turn_lifecycle import (
        bind_task,
        claim_attempt_start,
        create_turn_pair,
        record_task_event,
    )

    created = []
    for index in range(pairs):
        result = create_turn_pair(
            conv_id,
            command_id=f"{conv_id}:pair:{index}",
            input_projection={
                "content": f"question {index}",
                "_msgId": f"{conv_id}:input:{index}",
            },
            config={},
            user_id=1,
            conversation_defaults={
                "allowCreate": index == 0,
                "title": "Turn authority",
            },
        )
        attempt_id = result["attempt"]["attemptId"]
        claim_attempt_start(attempt_id, user_id=1)
        bind_task(attempt_id, f"{conv_id}:task:{index}", user_id=1)
        record_task_event(
            {
                "_attemptId": attempt_id,
                "_userId": 1,
                "content": f"answer {index}",
                "_msgId": f"{conv_id}:output:{index}",
                "thinking": "",
                "toolRounds": [],
                "segments": [
                    {
                        "type": "text",
                        "text": f"answer {index}",
                        "deliverable": True,
                        "terminal": True,
                    }
                ],
                "status": "done",
            },
            {"type": "done", "finishReason": "stop"},
        )
        created.append(result)
    return created


@pytest.mark.api
class TestAuthRoutes:
    def test_open_mode_principal_and_session_endpoints(self, flask_client):
        me = flask_client.get("/api/v1/users/me")
        assert me.status_code == 200
        assert me.get_json()["authenticated"] is True
        assert me.get_json()["ownerId"] == 1

        login = flask_client.post(
            "/api/v1/users/login",
            json={"email": "nobody@example.com", "password": "wrong"},
        )
        assert login.status_code == 401
        assert flask_client.post("/api/v1/users/logout").status_code == 200


@pytest.mark.api
class TestConversations:
    def test_first_turn_derives_title_when_new_shell_sends_no_real_title(
        self, flask_client, monkeypatch
    ):
        """The browser's ``New Chat`` label is never durable metadata."""
        import lib.conversation_sync.task_start as task_start_runtime
        from tests._seed import delete_conversation

        conv_id = f"derived-title-{time.time_ns()}"

        def fake_start(*args, **kwargs):
            kwargs["on_task_registered"]("title-task")
            return "title-task", None

        monkeypatch.setattr(
            task_start_runtime,
            "start_conversation_attempt_executor",
            fake_start,
        )
        try:
            response = flask_client.post(
                f"/api/v3/conversations/{conv_id}/turns",
                json={
                    "commandId": f"{conv_id}:first",
                    "message": {
                        "text": "<notranslate>Repair sidebar titles</notranslate>",
                    },
                    "config": {"model": "gpt-4o"},
                    "conversation": {
                        "allowCreate": True,
                        "title": "",
                        "createdAt": int(time.time() * 1000),
                        "settings": {},
                    },
                },
            )
            assert response.status_code == 200
            detail = flask_client.get(
                f"/api/v1/conversations/{conv_id}"
            )
            assert detail.status_code == 200
            assert detail.get_json()["title"] == "Repair sidebar titles"
        finally:
            delete_conversation(conv_id, user_id=1)

    def test_turn_projection_list_detail_window_metadata_and_delete(
        self, flask_client, monkeypatch
    ):
        conv_id = f"authority-{time.time_ns()}"
        _create_turn_conversation(monkeypatch, conv_id, pairs=4)

        listed = flask_client.get("/api/v1/conversations?meta=1")
        assert listed.status_code == 200
        item = next(row for row in listed.get_json()["items"] if row["id"] == conv_id)
        assert item["msgCount"] == 8
        assert "messages" not in item

        detail = flask_client.get(
            f"/api/v1/conversations/{conv_id}?window=3"
        )
        assert detail.status_code == 200
        body = detail.get_json()
        assert body["windowed"] is True
        assert body["totalCount"] == 8
        assert body["firstLoadedSeq"] == 5
        assert body["lastLoadedSeq"] == 7
        assert body["hasMore"] is True
        assert len(body["messages"]) == 3
        assert body["messages"][-1]["segments"][0]["terminal"] is True

        title = flask_client.patch(
            f"/api/v1/conversations/{conv_id}/title",
            json={"title": "Renamed without transcript replay"},
            headers={"Idempotency-Key": f"rename:{conv_id}"},
        )
        assert title.status_code == 200
        settings = flask_client.patch(
            f"/api/v1/conversations/{conv_id}/settings",
            json={"folderId": "folder-a", "autoTranslate": True},
            headers={"Idempotency-Key": f"settings:{conv_id}"},
        )
        assert settings.status_code == 200
        refreshed = flask_client.get(f"/api/v1/conversations/{conv_id}").get_json()
        assert refreshed["title"] == "Renamed without transcript replay"
        assert refreshed["settings"]["folderId"] == "folder-a"
        assert len(refreshed["messages"]) == 8

        clone_id = f"{conv_id}-copy"
        cloned = flask_client.post(
            f"/api/v1/conversations/{conv_id}/clone",
            json={"conversationId": clone_id, "title": "Atomic copy"},
            headers={"Idempotency-Key": f"clone:{conv_id}"},
        )
        assert cloned.status_code == 200
        clone = flask_client.get(f"/api/v1/conversations/{clone_id}").get_json()
        assert clone["title"] == "Atomic copy"
        assert len(clone["messages"]) == 8
        assert clone["messages"][0]["_turnId"] != refreshed["messages"][0]["_turnId"]

        assert flask_client.put(
            f"/api/v1/conversations/{conv_id}", json={"messages": []}
        ).status_code == 405
        assert flask_client.delete(
            f"/api/v1/conversations/{conv_id}",
            headers={"Idempotency-Key": f"delete:{conv_id}"},
        ).status_code == 200
        assert flask_client.get(f"/api/v1/conversations/{conv_id}").status_code == 404
        restored = flask_client.post(
            f"/api/v1/conversations/{conv_id}/restore",
            headers={"Idempotency-Key": f"restore:{conv_id}"},
        )
        assert restored.status_code == 200
        assert restored.get_json()["turnCount"] == 8
        assert len(flask_client.get(
            f"/api/v1/conversations/{conv_id}"
        ).get_json()["messages"]) == 8

        from tests._seed import delete_conversation

        delete_conversation(conv_id)
        delete_conversation(clone_id)

    def test_full_list_and_recency_touch_are_rejected(self, flask_client):
        assert flask_client.get("/api/v1/conversations?full=1").status_code == 400
        missing = flask_client.patch(
            "/api/v1/conversations/missing/settings",
            json={"touchUpdatedAt": True},
        )
        assert missing.status_code == 400


@pytest.mark.api
class TestRetainedAPIs:
    def test_task_and_swarm_shapes(self, flask_client):
        tasks = flask_client.get("/api/v1/tasks")
        assert tasks.status_code == 200
        assert isinstance(tasks.get_json()["tasks"], list)

        swarm = flask_client.get("/api/v1/swarm/config")
        assert swarm.status_code == 200
        assert isinstance(swarm.get_json()["roles"], list)
        missing = flask_client.get("/api/v1/swarm/status/nonexistent-task")
        assert missing.status_code == 200
        assert missing.get_json().get("known") is False
        assert missing.get_json().get("active") is None

    def test_invalid_task_starts_fail_closed(self, flask_client):
        chat = flask_client.post(
            "/api/v1/chat/start", json={"convId": "missing", "config": {}}
        )
        assert chat.status_code in {400, 404}

    def test_misc_read_surfaces(self, flask_client):
        assert flask_client.get("/api/v1/memory").status_code == 200
        assert flask_client.get("/api/v1/scheduler/tasks").status_code == 200
        # The browser bridge is a single POST poll transport.  The retired
        # split GET command endpoint must stay absent instead of quietly
        # reviving a second delivery contract.
        from lib.api_keys import create_key
        _row, bridge_token = create_key(
            owner_user_id=1,
            name='api-integration-browser',
            scopes=['agents:bridge'],
        )

        bridge = flask_client.get(
            "/api/browser/commands",
            headers={"X-Bridge-Secret": bridge_token},
        )
        assert bridge.status_code == 404


@pytest.mark.api
def test_static_shell_uses_built_frontend(flask_client):
    index = flask_client.get("/")
    assert index.status_code == 200
    html = index.data.decode("utf-8")
    assert '<!-- TOFU_APP_ASSETS -->' not in html
    assert 'id="tofu-boot-config"' in html
    match = re.search(r'src="(static/vite/assets/[^"?]+\.js)', html)
    assert match
    asset = flask_client.get("/" + match.group(1))
    assert asset.status_code == 200
    assert len(asset.data) > 10_000
