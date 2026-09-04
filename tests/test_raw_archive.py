"""Durable Request Inspector raw archive budgets and owner isolation."""

from __future__ import annotations

import base64
import hashlib
import uuid
import zlib

import pytest


pytestmark = pytest.mark.unit
pytest_plugins = ("tests._chat_sidecar",)


def test_transport_capture_uses_final_body_and_scrubs_secrets(monkeypatch):
    from lib.llm._sse_core import prepare_request
    from lib.raw_archive_contract import RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES
    import lib.storage
    import lib.raw_archive

    captured: dict = {}

    class _Client:
        def command(self, operation, payload, command_id, **_kwargs):
            captured.update({
                "operation": operation,
                "payload": payload,
                "command_id": command_id,
            })
            return {"archiveId": payload["archive_id"]}

    monkeypatch.setattr(lib.storage, "get_storage_client", lambda **_kwargs: _Client())
    monkeypatch.setattr(
        lib.raw_archive.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {
            "free": RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES * 8,
        })(),
    )
    monkeypatch.setenv("TOFU_RAW_ARCHIVE_BUDGET_MIB", "256")
    plan = prepare_request({
        "model": "gpt-test",
        "messages": [{
            "role": "user",
            "content": "credential sk-supersecret-value",
        }],
        "stream": True,
        "_raw_archive_context": {
            "userId": 1,
            "conversationId": "conv-raw",
            "turnId": "turn-raw",
            "attemptId": "attempt-raw",
            "taskId": "task-raw",
            "roundNum": 2,
            "model": "gpt-test",
        },
    }, api_key="sk-header-secret-value", base_url="https://example.invalid")
    assert plan.raw_archive_capture is not None
    assert "_raw_archive_context" not in plan.body
    plan.raw_archive_capture.append_response(
        b'data: {"authorization":"Bearer abcdefghijklmnop"}\n\n')
    plan.raw_archive_capture.commit(response_complete=True, status_code=200)

    payload = captured["payload"]
    request = zlib.decompress(base64.b64decode(payload["request_blob_b64"]))
    response = zlib.decompress(base64.b64decode(payload["response_blob_b64"]))
    assert captured["operation"] == "raw_archive.put"
    assert b"_raw_archive_context" not in request
    assert b"supersecret" not in request
    assert b"abcdefghijklmnop" not in response
    assert b"redacted" in request and b"redacted" in response
    assert payload["integrity"] == "partial"
    assert payload["truncation_reason"] == "secret_scrubbed"
    assert payload["available_free_bytes"] == \
        RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES


def _archive_payload(created, archive_id: str, *, budget_bytes: int) -> dict:
    request = b'{"messages":[{"role":"user","content":"hello"}]}'
    response = b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
    return {
        "archive_id": archive_id,
        "user_id": 1,
        "conversation_id": created["turn"]["conversationId"],
        "turn_id": created["turn"]["turnId"],
        "attempt_id": created["attempt"]["attemptId"],
        "task_id": "raw-task",
        "round_num": 1,
        "transport_attempt": 0,
        "request_blob_b64": base64.b64encode(
            zlib.compress(request, level=1)).decode("ascii"),
        "response_blob_b64": base64.b64encode(
            zlib.compress(response, level=1)).decode("ascii"),
        "request_bytes": len(request),
        "response_bytes": len(response),
        "request_sha256": hashlib.sha256(request).hexdigest(),
        "response_sha256": hashlib.sha256(response).hexdigest(),
        "integrity": "complete",
        "truncation_reason": "",
        "summary": {
            "text": "Provider request/response",
            "combinedSha256": hashlib.sha256(request + response).hexdigest(),
        },
        "budget_bytes": budget_bytes,
        "min_free_bytes": 0,
        "available_free_bytes": 0,
    }


def test_raw_archive_is_owner_scoped_lazy_and_quota_explicit(chat_sidecar):
    from lib.storage import get_storage_client
    from lib.turn_lifecycle import bind_task, create_turn_pair
    from tests._seed import delete_conversation, seed_conversation

    conversation_id = "raw-archive-" + uuid.uuid4().hex
    seed_conversation(conversation_id, messages=[])
    try:
        created = create_turn_pair(
            conversation_id,
            command_id="raw-create-" + uuid.uuid4().hex,
            input_projection={"content": "hello"},
            config={"model": "gpt-4o"},
            user_id=1,
        )
        bind_task(created["attempt"]["attemptId"], "raw-task", user_id=1)
        client = get_storage_client(write=True)
        archive_id = "raw-" + uuid.uuid4().hex
        stored = client.command(
            "raw_archive.put",
            _archive_payload(created, archive_id, budget_bytes=1024 * 1024),
            "put:" + archive_id,
        )
        assert stored["integrity"] == "complete"
        assert stored["requestAvailable"] is True

        listed = client.query("raw_archive.list", {
            "user_id": 1, "task_id": "raw-task", "round_num": 1,
        })
        assert [row["archiveId"] for row in listed["archives"]] == [archive_id]
        assert client.query("raw_archive.list", {
            "user_id": 2, "task_id": "raw-task", "round_num": 1,
        })["archives"] == []
        chunk = client.query("raw_archive.read", {
            "user_id": 1,
            "task_id": "raw-task",
            "archive_id": archive_id,
            "part": "response",
            "offset": 0,
            "limit": 16,
        })
        assert base64.b64decode(chunk["dataBase64"]).startswith(b"data:")
        assert chunk["hasMore"] is True
        assert client.query("raw_archive.read", {
            "user_id": 2,
            "task_id": "raw-task",
            "archive_id": archive_id,
            "part": "response",
        }) is None

        quota_archive_id = "raw-" + uuid.uuid4().hex
        quota = client.command(
            "raw_archive.put",
            _archive_payload(created, quota_archive_id, budget_bytes=1),
            "put:" + quota_archive_id,
        )
        assert quota["integrity"] == "partial"
        assert quota["truncationReason"] == "quota_exhausted"
        assert quota["storedBytes"] == 0
        assert quota["requestAvailable"] is False

        # User deletion owns archive deletion; there is no TTL or silent
        # maintenance eviction path.
        client.command("conversation.delete", {
            "conv_id": conversation_id, "user_id": 1,
        }, "delete:" + conversation_id)
        assert client.query("raw_archive.list", {
            "user_id": 1, "task_id": "raw-task",
        })["archives"] == []
    finally:
        delete_conversation(conversation_id)
