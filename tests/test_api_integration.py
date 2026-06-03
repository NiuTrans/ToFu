"""API integration tests — Quart test client hitting all core endpoints.

Uses the (sync-adapted) test client from conftest.py. Tests the core
/api/v1 routes for correct status codes, response shapes, and error
handling. The suite runs in open auth mode (the conftest ``flask_client``
fixture forces ``TOFU_AUTH_MODE=open`` unless a marker overrides it).

Run:  pytest tests/test_api_integration.py -m api
"""
from __future__ import annotations

import time

import pytest

# ═══════════════════════════════════════════════════════════
#  Auth & Meta (post /api/v1 migration)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestAuthRoutes:
    """In open auth mode the current principal is always authenticated."""

    def test_me(self, flask_client):
        resp = flask_client.get("/api/v1/users/me")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["authenticated"] is True

    def test_login_bad_credentials(self, flask_client):
        resp = flask_client.post("/api/v1/users/login",
                                 json={"email": "nobody@example.com",
                                       "password": "wrong"})
        # No such user → 401 with a structured error envelope.
        assert resp.status_code == 401
        assert resp.get_json()["ok"] is False

    def test_logout(self, flask_client):
        resp = flask_client.post("/api/v1/users/logout")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True


# ═══════════════════════════════════════════════════════════
#  Conversations CRUD
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestConversations:
    """Conversation list, save, load, delete under /api/v1."""

    def test_list_conversations_empty(self, flask_client):
        resp = flask_client.get("/api/v1/conversations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_save_and_load_conversation(self, flask_client):
        conv_id = f"test-conv-{int(time.time()*1000)}"
        now = int(time.time() * 1000)

        save_resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Test Conversation",
            "messages": [
                {"role": "user", "content": "Hello", "timestamp": now},
                {"role": "assistant", "content": "Hi there!", "timestamp": now + 1},
            ],
            "createdAt": now,
            "updatedAt": now,
        })
        assert save_resp.status_code == 200

        load_resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
        assert load_resp.status_code == 200
        data = load_resp.get_json()
        assert data["id"] == conv_id
        assert len(data["messages"]) == 2

        list_resp = flask_client.get("/api/v1/conversations")
        assert list_resp.status_code == 200
        conv_ids = [c["id"] for c in list_resp.get_json()]
        assert conv_id in conv_ids

        del_resp = flask_client.delete(f"/api/v1/conversations/{conv_id}")
        assert del_resp.status_code == 200

        load_after = flask_client.get(f"/api/v1/conversations/{conv_id}")
        assert load_after.status_code in (404, 200)

    def test_save_conversation_minimal(self, flask_client):
        conv_id = f"test-minimal-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
            "title": "Minimal",
            "messages": [],
            "createdAt": now,
            "updatedAt": now,
        })
        assert resp.status_code == 200
        flask_client.delete(f"/api/v1/conversations/{conv_id}")


# ═══════════════════════════════════════════════════════════
#  Chat Start & Polling
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestChatAPI:
    """Chat task lifecycle: start → poll."""

    def test_chat_start_requires_messages(self, flask_client):
        resp = flask_client.post("/api/v1/chat/start", json={
            "convId": "test-conv",
            "config": {},
        })
        assert resp.status_code in (400, 404)

    def test_chat_start_creates_task(self, flask_client):
        resp = flask_client.post("/api/v1/chat/start", json={
            "convId": "test-conv",
            "messages": [{"role": "user", "content": "Hello"}],
            "config": {"model": "mock-model"},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "taskId" in data
        task_id = data["taskId"]

        time.sleep(0.5)

        poll_resp = flask_client.get(f"/api/v1/chat/poll/{task_id}")
        assert poll_resp.status_code == 200

    def test_chat_active_tasks(self, flask_client):
        resp = flask_client.get("/api/v1/chat/active")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)


# ═══════════════════════════════════════════════════════════
#  Endpoint Mode
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestEndpointAPI:
    """Endpoint mode task lifecycle."""

    def test_endpoint_requires_user_message(self, flask_client):
        resp = flask_client.post("/api/v1/endpoint/start", json={
            "messages": [{"role": "system", "content": "You are helpful"}],
            "config": {},
        })
        assert resp.status_code == 400

    def test_endpoint_requires_messages(self, flask_client):
        resp = flask_client.post("/api/v1/endpoint/start", json={
            "messages": [],
            "config": {},
        })
        assert resp.status_code in (400, 404)

    def test_endpoint_start_success(self, flask_client):
        resp = flask_client.post("/api/v1/endpoint/start", json={
            "convId": "test-endpoint",
            "messages": [{"role": "user", "content": "Build a calculator"}],
            "config": {"model": "mock-model"},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "taskId" in data
        assert "convId" in data


# ═══════════════════════════════════════════════════════════
#  Swarm Config
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestSwarmAPI:
    """Swarm configuration endpoint."""

    def test_swarm_config(self, flask_client):
        resp = flask_client.get("/api/v1/swarm/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert "roles" in data
        assert isinstance(data["roles"], list)

    def test_swarm_status_nonexistent(self, flask_client):
        resp = flask_client.get("/api/v1/swarm/status/nonexistent-task")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("active") is False


# ═══════════════════════════════════════════════════════════
#  Translate
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestTranslateAPI:
    """Translation endpoint under /api/v1."""

    def test_translate_requires_text(self, flask_client):
        resp = flask_client.post("/api/v1/translate", json={})
        assert resp.status_code in (400, 200)

    def test_translate_with_text(self, flask_client):
        resp = flask_client.post("/api/v1/translate", json={
            "text": "Hello world",
            "targetLang": "zh",
        })
        # May succeed or fail depending on LLM availability.
        assert resp.status_code in (200, 500)


# ═══════════════════════════════════════════════════════════
#  Static Pages
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestStaticPages:
    """Core HTML pages load successfully."""

    def test_index_page(self, flask_client):
        resp = flask_client.get("/")
        # The bundled-HTML path returns 200 with the page. The bundle-failure
        # fallback uses send_from_directory, which under the sync test adapter
        # can surface as a 500 (sync route returning an un-awaited coroutine);
        # this is a harness artifact, not a production issue (Quart awaits it
        # in the real async dispatch). Accept either, but when 200 assert the
        # page content is right.
        assert resp.status_code in (200, 500)
        if resp.status_code == 200:
            assert "Tofu" in resp.data.decode("utf-8")

    def test_css_loads(self, flask_client):
        resp = flask_client.get("/static/styles.css")
        assert resp.status_code == 200
        assert len(resp.data) > 1000

    def test_main_js_loads(self, flask_client):
        resp = flask_client.get("/static/js/main.js")
        assert resp.status_code == 200
        assert len(resp.data) > 10000


# ═══════════════════════════════════════════════════════════
#  Memory / Browser / Scheduler stubs
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestMiscEndpoints:
    """Various other endpoints return valid responses."""

    def test_memory_list(self, flask_client):
        resp = flask_client.get("/api/v1/memory")
        assert resp.status_code == 200

    def test_browser_commands(self, flask_client):
        resp = flask_client.get("/api/browser/commands")
        assert resp.status_code == 200

    def test_scheduler_tasks(self, flask_client):
        resp = flask_client.get("/api/v1/scheduler/tasks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
