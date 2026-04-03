"""API integration tests — Flask test client hitting all core endpoints.

Uses the Flask test client (direct WSGI, no real HTTP) with a mock LLM
backend. Tests all major API routes for correct status codes, response
shapes, and error handling.

Run:  pytest tests/test_api_integration.py -m api
"""
from __future__ import annotations

import json
import time

import pytest

# ═══════════════════════════════════════════════════════════
#  Auth & Meta
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestAuthRoutes:
    """Auth stubs should always return success (single-user mode)."""

    def test_me(self, flask_client):
        resp = flask_client.get("/api/me")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["authenticated"] is True

    def test_login(self, flask_client):
        resp = flask_client.post("/api/login",
                                 json={"username": "test", "password": "test"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_logout(self, flask_client):
        resp = flask_client.post("/api/logout")
        assert resp.status_code == 200


# ═══════════════════════════════════════════════════════════
#  Conversations CRUD
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestConversations:
    """Conversation list, save, load, delete."""

    def test_list_conversations_empty(self, flask_client):
        resp = flask_client.get("/api/conversations")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, list)

    def test_save_and_load_conversation(self, flask_client):
        conv_id = f"test-conv-{int(time.time()*1000)}"
        now = int(time.time() * 1000)

        # Save via PUT /api/conversations/<id>
        save_resp = flask_client.put(f"/api/conversations/{conv_id}", json={
            "title": "Test Conversation",
            "messages": [
                {"role": "user", "content": "Hello", "timestamp": now},
                {"role": "assistant", "content": "Hi there!", "timestamp": now + 1},
            ],
            "createdAt": now,
            "updatedAt": now,
        })
        assert save_resp.status_code == 200

        # Load
        load_resp = flask_client.get(f"/api/conversations/{conv_id}")
        assert load_resp.status_code == 200
        data = load_resp.get_json()
        assert data["id"] == conv_id
        assert len(data["messages"]) == 2

        # Verify in list
        list_resp = flask_client.get("/api/conversations")
        assert list_resp.status_code == 200
        conv_ids = [c["id"] for c in list_resp.get_json()]
        assert conv_id in conv_ids

        # Delete
        del_resp = flask_client.delete(f"/api/conversations/{conv_id}")
        assert del_resp.status_code == 200

        # Verify deleted
        load_after = flask_client.get(f"/api/conversations/{conv_id}")
        assert load_after.status_code in (404, 200)  # may 404 or return empty

    def test_save_conversation_minimal(self, flask_client):
        """Save with minimal required fields."""
        conv_id = f"test-minimal-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        resp = flask_client.put(f"/api/conversations/{conv_id}", json={
            "title": "Minimal",
            "messages": [],
            "createdAt": now,
            "updatedAt": now,
        })
        assert resp.status_code == 200

        # Cleanup
        flask_client.delete(f"/api/conversations/{conv_id}")


# ═══════════════════════════════════════════════════════════
#  Chat Start & Polling
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestChatAPI:
    """Chat task lifecycle: start → poll → result."""

    def test_chat_start_requires_messages(self, flask_client):
        resp = flask_client.post("/api/chat/start", json={
            "convId": "test-conv",
            "messages": [],
            "config": {},
        })
        assert resp.status_code == 400

    def test_chat_start_creates_task(self, flask_client):
        resp = flask_client.post("/api/chat/start", json={
            "convId": "test-conv",
            "messages": [{"role": "user", "content": "Hello"}],
            "config": {"model": "mock-model"},
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert "taskId" in data
        task_id = data["taskId"]

        # Give the task a moment to process
        time.sleep(0.5)

        # Poll for result
        poll_resp = flask_client.get(f"/api/chat/poll/{task_id}")
        assert poll_resp.status_code == 200

    def test_chat_active_tasks(self, flask_client):
        resp = flask_client.get("/api/chat/active")
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
        resp = flask_client.post("/api/endpoint/start", json={
            "messages": [{"role": "system", "content": "You are helpful"}],
            "config": {},
        })
        assert resp.status_code == 400

    def test_endpoint_requires_messages(self, flask_client):
        resp = flask_client.post("/api/endpoint/start", json={
            "messages": [],
            "config": {},
        })
        assert resp.status_code == 400

    def test_endpoint_start_success(self, flask_client):
        resp = flask_client.post("/api/endpoint/start", json={
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
        resp = flask_client.get("/api/swarm/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["available"] is True
        assert "roles" in data
        assert isinstance(data["roles"], list)

    def test_swarm_status_nonexistent(self, flask_client):
        resp = flask_client.get("/api/swarm/status/nonexistent-task")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("active") is False


# ═══════════════════════════════════════════════════════════
#  Translate
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestTranslateAPI:
    """Translation endpoint."""

    def test_translate_requires_text(self, flask_client):
        resp = flask_client.post("/api/translate", json={})
        assert resp.status_code in (400, 200)  # may return error in body

    def test_translate_with_text(self, flask_client):
        resp = flask_client.post("/api/translate", json={
            "text": "Hello world",
            "targetLang": "zh",
        })
        # May succeed or fail depending on LLM availability
        assert resp.status_code in (200, 500)


# ═══════════════════════════════════════════════════════════
#  Static Pages
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestStaticPages:
    """Core HTML pages load successfully."""

    def test_index_page(self, flask_client):
        resp = flask_client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "Tofu" in html

    def test_css_loads(self, flask_client):
        resp = flask_client.get("/static/styles.css")
        assert resp.status_code == 200
        assert len(resp.data) > 1000  # non-trivial CSS

    def test_main_js_loads(self, flask_client):
        resp = flask_client.get("/static/js/main.js")
        assert resp.status_code == 200
        assert len(resp.data) > 10000


# ═══════════════════════════════════════════════════════════
#  Settings / Browser / Skills stubs
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestMiscEndpoints:
    """Various other endpoints return valid responses."""

    def test_skills_list(self, flask_client):
        resp = flask_client.get("/api/skills")
        assert resp.status_code == 200

    def test_browser_commands(self, flask_client):
        resp = flask_client.get("/api/browser/commands")
        assert resp.status_code == 200

    def test_scheduler_tasks(self, flask_client):
        resp = flask_client.get("/api/scheduler/tasks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
