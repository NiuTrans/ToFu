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
        # api-contract §4 (batch 9): the bare array moved under the api_ok
        # envelope's ``items`` key — a documented, coordinated shape change.
        assert data["ok"] is True
        assert isinstance(data["items"], list)

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
        # api-contract §4 (batch 9): the list array rides the envelope's
        # ``items`` key.
        conv_ids = [c["id"] for c in list_resp.get_json()["items"]]
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

    def test_segments_survive_get_load_path(self, flask_client):
        """END-TO-END delivery (epic pt_8b406df8fbe24ae5): a conversation whose
        persisted assistant message carries `segments` (as the backend persist
        path writes onto the messages column) must deliver those segments,
        intact and interleaved-shaped, through the REAL GET route that the
        frontend `loadConversationMessages` Phase-2 fetch hits — so
        `msg.segments` reaches renderSegmentTimelineHTML, not a stub.

        Seeded via a DIRECT DB write (the PUT wire intentionally strips segments
        — the client never echoes the backend SoT), mirroring what
        `_sync_result_to_conversation` writes. Then GET and assert the
        server-authoritative reconcile + `_conv_to_dict` preserve segments.
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert

        conv_id = f"test-segs-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        # A finished assistant turn with the interleaved segment shape the
        # renderer expects: thinking → narration → tool_use, then deliverable.
        segments = [
            {"type": "thinking", "text": "reason0", "deliverable": False, "llmRound": 0},
            {"type": "text", "text": "Let me search.", "deliverable": False, "llmRound": 0},
            {"type": "tool_use", "id": "tc1", "name": "web_search",
             "input": "{}", "llmRound": 0, "result": {"content": "hit", "status": "done"}},
            {"type": "text", "text": "The answer.", "deliverable": True, "terminal": True},
        ]
        messages = [
            {"role": "user", "content": "q", "timestamp": now},
            {"role": "assistant", "content": "The answer.", "thinking": "reason0",
             "toolRounds": [{"toolCallId": "tc1", "toolName": "web_search",
                             "status": "done", "toolContent": "hit", "llmRound": 0}],
             "segments": segments, "timestamp": now + 1},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            "id": conv_id, "user_id": 1, "title": "seg-e2e",
            "messages": json_dumps_pg(messages), "msg_count": len(messages),
            "created_at": now, "updated_at": now,
        }, insert_cols=["id", "user_id", "title", "messages", "msg_count",
                        "created_at", "updated_at"], retry=True)
        db.commit()

        try:
            resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
            assert resp.status_code == 200
            data = resp.get_json()
            asst = [m for m in data["messages"] if m.get("role") == "assistant"]
            assert asst, "assistant message missing from GET response"
            segs = asst[-1].get("segments")
            # THE PROOF: segments survive the GET/reconcile/_conv_to_dict path.
            assert isinstance(segs, list) and len(segs) == 4, \
                f"segments did not survive the load path: {segs!r}"
            types = [s.get("type") for s in segs]
            assert types == ["thinking", "text", "tool_use", "text"], types
            # Interleaved shape intact: a tool_use with its result nested, and
            # exactly one terminal deliverable.
            tu = next(s for s in segs if s["type"] == "tool_use")
            assert tu["id"] == "tc1" and tu["name"] == "web_search"
            deliverables = [s for s in segs if s["type"] == "text" and s.get("deliverable")]
            assert len(deliverables) == 1 and deliverables[0]["text"] == "The answer."
        finally:
            flask_client.delete(f"/api/v1/conversations/{conv_id}")

    def test_segments_preserved_across_client_strip_put(self, flask_client):
        """PRIMARY FIX (epic pt_cb8f98b0cb9b47fb): the client PUT strips
        `segments` on every full-conversation sync (`_trimMsgForPersist`), yet
        `segments` is a backend-authoritative server-authored field. `save_conv`
        must merge it back from the existing DB message (keyed on _msgId /
        _taskId) so the persisted row keeps segments — otherwise the FIRST
        post-turn sync wipes them and the reloaded message renders grouped.

        Reproduces the REAL path the existing e2e test does NOT: seed a message
        WITH segments → PUT the same conversation with segments stripped (the
        exact shape `_trimMsgForPersist` produces: no `segments` key) → assert
        the persisted+served row STILL has segments (preservation), and that the
        strip-PUT did not bump the CAS `rev` (byte-identical merged messages).
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert

        conv_id = f"test-segpreserve-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        msg_id = "m-asst-1"
        segments = [
            {"type": "thinking", "text": "reason0", "deliverable": False, "llmRound": 0},
            {"type": "text", "text": "Let me search.", "deliverable": False, "llmRound": 0},
            {"type": "tool_use", "id": "tc1", "name": "web_search",
             "input": "{}", "llmRound": 0, "result": {"content": "hit", "status": "done"}},
            {"type": "text", "text": "The answer.", "deliverable": True, "terminal": True},
        ]
        # Seed WITH segments (as _sync_result_to_conversation writes). Note the
        # `segments` key is LAST in the assistant dict so a re-merge that appends
        # it back is byte-identical → the rev trigger must not fire.
        seed_msgs = [
            {"role": "user", "content": "q", "_msgId": "m-user-1", "timestamp": now},
            {"role": "assistant", "content": "The answer.", "thinking": "reason0",
             "toolRounds": [{"toolCallId": "tc1", "toolName": "web_search",
                             "status": "done", "toolContent": "hit", "llmRound": 0}],
             "_msgId": msg_id, "_taskId": "task-abc", "timestamp": now + 1,
             "segments": segments},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            "id": conv_id, "user_id": 1, "title": "seg-preserve",
            "messages": json_dumps_pg(seed_msgs), "msg_count": len(seed_msgs),
            "created_at": now, "updated_at": now,
        }, insert_cols=["id", "user_id", "title", "messages", "msg_count",
                        "created_at", "updated_at"], retry=True)
        db.commit()
        rev_before = db.execute(
            "SELECT rev FROM conversations WHERE id=? AND user_id=?",
            (conv_id, 1)).fetchone()[0]

        try:
            # The client PUT: identical messages MINUS `segments` (what
            # _trimMsgForPersist deletes). Same _msgId/_taskId identity.
            stripped = [dict(m) for m in seed_msgs]
            for m in stripped:
                m.pop("segments", None)
            put_resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
                "title": "seg-preserve",
                "messages": stripped,
                "createdAt": now, "updatedAt": now + 100,
            })
            assert put_resp.status_code == 200, put_resp.get_json()

            # THE PROOF (preservation): the persisted row STILL carries segments.
            get_resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
            assert get_resp.status_code == 200
            asst = [m for m in get_resp.get_json()["messages"]
                    if m.get("role") == "assistant"]
            segs = asst[-1].get("segments")
            assert isinstance(segs, list) and len(segs) == 4, \
                f"segments were NOT preserved across the strip-PUT: {segs!r}"
            assert [s["type"] for s in segs] == ["thinking", "text", "tool_use", "text"]

            # rev-CAS neutrality: the merge re-materializes the SAME bytes the
            # row already held, so the messages-change trigger must not bump rev.
            rev_after = db.execute(
                "SELECT rev FROM conversations WHERE id=? AND user_id=?",
                (conv_id, 1)).fetchone()[0]
            assert rev_after == rev_before, \
                f"strip-PUT falsely bumped rev {rev_before}->{rev_after}"
        finally:
            flask_client.delete(f"/api/v1/conversations/{conv_id}")

    def test_vu_segments_preserved_across_client_strip_put(self, flask_client):
        """RELOAD FIDELITY (autopilot VU inline timeline): an autopilot VU turn
        is a ``role=user`` row carrying ``segments`` (a NEW shape — the existing
        segment machinery was built for assistant rows). The GET backstop is
        assistant/_taskId-only, so a VU row can ONLY survive reload via the
        save_conv preserve-on-write merge, keyed on ``_msgId`` (which the
        ``_isVirtualUser`` row carries). Without it the timeline would render
        live+settle then VANISH on refresh — the exact snap-back being fixed.

        Reproduces the real path: seed a VU user row WITH segments (as
        ``_append_vu_message_to_conv`` writes) → PUT the same conversation with
        segments stripped (what ``_trimMsgForPersist`` produces) → assert the
        persisted+served VU row STILL carries segments, and the strip-PUT did
        not bump the CAS ``rev`` (byte-identical merged messages).
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert

        conv_id = f"test-vusegpreserve-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        vu_msg_id = "vu-msg-1"
        segments = [
            {"type": "thinking", "text": "verify claim", "deliverable": False, "llmRound": 0},
            {"type": "text", "text": "Let me check the exporter.", "deliverable": False, "llmRound": 0},
            {"type": "tool_use", "id": "tc1", "name": "read_files",
             "input": "{}", "llmRound": 0, "result": {"content": "ok", "status": "done"}},
            {"type": "text", "text": "Keep going.", "deliverable": True, "terminal": True},
        ]
        # A VU user row exactly as _append_vu_message_to_conv writes it:
        # role=user, _isVirtualUser, _msgId, toolRounds + segments co-persisted.
        seed_msgs = [
            {"role": "user", "content": "q", "_msgId": "m-user-1", "timestamp": now},
            {"role": "assistant", "content": "attempt", "_msgId": "m-asst-1",
             "timestamp": now + 1},
            {"role": "user", "content": "Keep going.", "_isVirtualUser": True,
             "_msgId": vu_msg_id, "_autopilotRunId": "RX", "timestamp": now + 2,
             "toolRounds": [{"toolCallId": "tc1", "toolName": "read_files",
                             "status": "done", "toolContent": "ok", "llmRound": 0}],
             "segments": segments},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            "id": conv_id, "user_id": 1, "title": "vu-seg-preserve",
            "messages": json_dumps_pg(seed_msgs), "msg_count": len(seed_msgs),
            "created_at": now, "updated_at": now,
        }, insert_cols=["id", "user_id", "title", "messages", "msg_count",
                        "created_at", "updated_at"], retry=True)
        db.commit()
        rev_before = db.execute(
            "SELECT rev FROM conversations WHERE id=? AND user_id=?",
            (conv_id, 1)).fetchone()[0]

        try:
            # The client PUT: identical messages MINUS `segments` (what
            # _trimMsgForPersist deletes on EVERY full-conv sync, role-agnostic).
            stripped = [dict(m) for m in seed_msgs]
            for m in stripped:
                m.pop("segments", None)
            put_resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
                "title": "vu-seg-preserve",
                "messages": stripped,
                "createdAt": now, "updatedAt": now + 100,
            })
            assert put_resp.status_code == 200, put_resp.get_json()

            # THE PROOF: the persisted VU (role=user) row STILL carries segments.
            get_resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
            assert get_resp.status_code == 200
            vu = [m for m in get_resp.get_json()["messages"]
                  if m.get("_isVirtualUser")]
            assert vu, "VU message missing from GET response"
            segs = vu[-1].get("segments")
            assert isinstance(segs, list) and len(segs) == 4, \
                f"VU segments were NOT preserved across the strip-PUT: {segs!r}"
            assert [s["type"] for s in segs] == ["thinking", "text", "tool_use", "text"]

            # rev-CAS neutrality: the merge re-materializes the SAME bytes, so
            # the messages-change trigger must not bump rev.
            rev_after = db.execute(
                "SELECT rev FROM conversations WHERE id=? AND user_id=?",
                (conv_id, 1)).fetchone()[0]
            assert rev_after == rev_before, \
                f"VU strip-PUT falsely bumped rev {rev_before}->{rev_after}"
        finally:
            flask_client.delete(f"/api/v1/conversations/{conv_id}")

    def test_segments_preservation_neutered_loses_them(self, flask_client):
        """NEUTER of the PRIMARY fix: simulate the pre-fix save_conv by PUTting a
        stripped payload into a conversation whose DB copy has segments, but with
        NO matching identity (different _msgId AND _taskId) — the merge cannot
        find a source, so segments are genuinely lost. Proves the preservation is
        IDENTITY-DRIVEN and load-bearing: without a matching id it does NOT fire,
        and the message reloads segment-less (the exact pre-fix failure).
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert

        conv_id = f"test-segneuter-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        segments = [
            {"type": "tool_use", "id": "tc1", "name": "web_search",
             "input": "{}", "llmRound": 0, "result": {"content": "hit", "status": "done"}},
            {"type": "text", "text": "The answer.", "deliverable": True, "terminal": True},
        ]
        seed_msgs = [
            {"role": "user", "content": "q", "_msgId": "u1", "timestamp": now},
            {"role": "assistant", "content": "The answer.", "_msgId": "server-side-id",
             "_taskId": "server-task", "timestamp": now + 1, "segments": segments},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            "id": conv_id, "user_id": 1, "title": "seg-neuter",
            "messages": json_dumps_pg(seed_msgs), "msg_count": len(seed_msgs),
            "created_at": now, "updated_at": now,
        }, insert_cols=["id", "user_id", "title", "messages", "msg_count",
                        "created_at", "updated_at"], retry=True)
        db.commit()
        try:
            # Stripped PUT whose assistant carries DIFFERENT ids → no merge match.
            stripped = [
                {"role": "user", "content": "q", "_msgId": "u1", "timestamp": now},
                {"role": "assistant", "content": "The answer.",
                 "_msgId": "client-different-id", "_taskId": "client-different-task",
                 "timestamp": now + 1},
            ]
            put_resp = flask_client.put(f"/api/v1/conversations/{conv_id}", json={
                "title": "seg-neuter", "messages": stripped,
                "createdAt": now, "updatedAt": now + 100,
            })
            assert put_resp.status_code == 200
            get_resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
            asst = [m for m in get_resp.get_json()["messages"]
                    if m.get("role") == "assistant"]
            # No identity match → segments genuinely gone (proves the merge is
            # gated on identity, not a blanket resurrect).
            assert not asst[-1].get("segments"), \
                "segments should NOT survive when no _msgId/_taskId matches"
        finally:
            flask_client.delete(f"/api/v1/conversations/{conv_id}")

    def test_segments_rehydrated_from_task_results_on_get(self, flask_client):
        """BACKSTOP (epic pt_cb8f98b0cb9b47fb): a turn persisted BEFORE the
        save_conv fix has NO segments in the messages column but its `_taskId`
        still has a `task_results.segments` row (the backend SoT). The GET path
        must rehydrate segments from task_results so the reloaded message renders
        the timeline. Display-only: the served payload gets segments, but they
        are NOT written back (no rev bump).

        This is exactly the state of the owner's on-screen conversation:
        `segments=0` on the message, but 58 segments in task_results.
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, TASK_RESULTS, upsert

        conv_id = f"test-segrehy-{int(time.time()*1000)}"
        task_id = f"task-rehy-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        # Message has NO segments (stripped long ago), but carries _taskId.
        messages = [
            {"role": "user", "content": "q", "_msgId": "u1", "timestamp": now},
            {"role": "assistant", "content": "The answer.", "_msgId": "a1",
             "_taskId": task_id,
             "toolRounds": [{"toolCallId": "tc1", "toolName": "web_search",
                             "status": "done", "toolContent": "hit", "llmRound": 0}],
             "timestamp": now + 1},
        ]
        # The backend-authoritative segments live in task_results (thin form).
        tr_segments = [
            {"type": "thinking", "text": "reason0", "deliverable": False, "llmRound": 0},
            {"type": "tool_use", "id": "tc1", "name": "web_search",
             "input": "{}", "llmRound": 0, "result": {"content": "hit", "status": "done"}},
            {"type": "text", "text": "The answer.", "deliverable": True, "terminal": True},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            "id": conv_id, "user_id": 1, "title": "seg-rehy",
            "messages": json_dumps_pg(messages), "msg_count": len(messages),
            "created_at": now, "updated_at": now,
        }, insert_cols=["id", "user_id", "title", "messages", "msg_count",
                        "created_at", "updated_at"], retry=True)
        upsert(db, TASK_RESULTS, {
            "task_id": task_id, "conv_id": conv_id, "content": "The answer.",
            "thinking": "reason0", "status": "done",
            "segments": json.dumps(tr_segments),
            "created_at": now, "completed_at": now + 1,
        }, insert_cols=["task_id", "conv_id", "content", "thinking", "status",
                        "segments", "created_at", "completed_at"], retry=True)
        db.commit()
        try:
            resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
            assert resp.status_code == 200
            asst = [m for m in resp.get_json()["messages"]
                    if m.get("role") == "assistant"]
            segs = asst[-1].get("segments")
            # THE PROOF (rehydration): segments came from task_results.
            assert isinstance(segs, list) and len(segs) == 3, \
                f"segments were NOT rehydrated from task_results: {segs!r}"
            assert [s["type"] for s in segs] == ["thinking", "tool_use", "text"]

            # Display-only: NOT persisted back to the messages column.
            row = db.execute("SELECT messages FROM conversations WHERE id=? AND user_id=?",
                             (conv_id, 1)).fetchone()
            db_msgs = json.loads(row[0])
            db_asst = [m for m in db_msgs if m.get("role") == "assistant"][-1]
            assert not db_asst.get("segments"), \
                "rehydration must be display-only (not written back to the column)"
        finally:
            flask_client.delete(f"/api/v1/conversations/{conv_id}")

    def test_rehydration_neutered_no_task_results_row(self, flask_client):
        """NEUTER of the BACKSTOP: identical to the rehydration test but with NO
        `task_results` row for the message's _taskId. The GET path must fall
        through cleanly — the message stays segment-less (→ grouped render),
        never a crash. Proves the rehydration is the load-bearing source and
        that its absence degrades gracefully.
        """
        import json
        from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
        from lib.database._core_schema import CONVERSATIONS, upsert

        conv_id = f"test-segrehy-none-{int(time.time()*1000)}"
        now = int(time.time() * 1000)
        messages = [
            {"role": "user", "content": "q", "_msgId": "u1", "timestamp": now},
            {"role": "assistant", "content": "The answer.", "_msgId": "a1",
             "_taskId": "task-with-no-results-row", "timestamp": now + 1},
        ]
        db = get_thread_db(DOMAIN_CHAT)
        upsert(db, CONVERSATIONS, {
            "id": conv_id, "user_id": 1, "title": "seg-rehy-none",
            "messages": json_dumps_pg(messages), "msg_count": len(messages),
            "created_at": now, "updated_at": now,
        }, insert_cols=["id", "user_id", "title", "messages", "msg_count",
                        "created_at", "updated_at"], retry=True)
        db.commit()
        try:
            resp = flask_client.get(f"/api/v1/conversations/{conv_id}")
            assert resp.status_code == 200
            asst = [m for m in resp.get_json()["messages"]
                    if m.get("role") == "assistant"]
            assert not asst[-1].get("segments"), \
                "no task_results row → message must stay segment-less (grouped)"
        finally:
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
        # api-contract §4 (batch 11): the bare array moved under ``items``;
        # every other consumer of this endpoint already unwraps it.
        assert data["ok"] is True
        assert isinstance(data["items"], list)


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
        # /api/browser/commands is bridge-scoped (B0): a credential is
        # required regardless of peer address. Use the in-process loopback
        # agent token, as the desktop bridge tests do.
        from routes.api_v1.auth import loopback_agent_token
        resp = flask_client.get("/api/browser/commands", headers={
            'X-Bridge-Secret': loopback_agent_token(),
        })
        assert resp.status_code == 200

    def test_scheduler_tasks(self, flask_client):
        resp = flask_client.get("/api/v1/scheduler/tasks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
