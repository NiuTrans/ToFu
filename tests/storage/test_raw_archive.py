"""Durable Request Inspector raw archive budgets and owner isolation."""

from __future__ import annotations

import base64
import hashlib
import uuid
import zlib

import pytest


pytestmark = pytest.mark.unit
pytest_plugins = ("tests._chat_sidecar",)


def test_transport_capture_uses_final_body_and_preserves_original_values(monkeypatch):
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
    assert plan.raw_dumper.raw_archive_ref == {
        'archiveId': plan.raw_archive_capture.archive_id,
        'traceId': plan.trace_id,
        'taskId': 'task-raw', 'turnId': 'turn-raw',
        'attemptId': 'attempt-raw', 'roundNum': 2,
    }
    assert "_raw_archive_context" not in plan.body
    plan.raw_archive_capture.append_response(
        b'data: {"authorization":"Bearer abcdefghijklmnop"}\n\n')
    plan.raw_archive_capture.commit(response_complete=True, status_code=200)

    payload = captured["payload"]
    request = zlib.decompress(base64.b64decode(payload["request_blob_b64"]))
    response = zlib.decompress(base64.b64decode(payload["response_blob_b64"]))
    assert captured["operation"] == "raw_archive.put"
    assert b"_raw_archive_context" not in request
    assert b"supersecret" in request
    assert b"abcdefghijklmnop" in response
    assert b"redacted" not in request and b"redacted" not in response
    assert payload["integrity"] == "complete"
    assert payload["truncation_reason"] == ""
    assert payload["available_free_bytes"] == \
        RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES


def _capture_plan(monkeypatch, client):
    from lib.llm._sse_core import prepare_request
    from lib.raw_archive_contract import RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES
    import lib.storage
    import lib.raw_archive

    monkeypatch.setattr(
        lib.storage, "get_storage_client", lambda **_kwargs: client)
    monkeypatch.setattr(
        lib.raw_archive.shutil,
        "disk_usage",
        lambda _path: type("Usage", (), {
            "free": RAW_ARCHIVE_FREE_SPACE_WIRE_MAX_BYTES * 8,
        })(),
    )
    monkeypatch.setattr(lib.raw_archive.time, "sleep", lambda _s: None)
    plan = prepare_request({
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "_raw_archive_context": {
            "userId": 1,
            "conversationId": "conv-retry",
            "turnId": "turn-retry",
            "attemptId": "attempt-retry",
            "taskId": "task-retry",
            "roundNum": 1,
            "model": "gpt-test",
        },
    }, api_key="sk-header-secret-value", base_url="https://example.invalid")
    return plan


def test_commit_retries_classified_transient_errors(monkeypatch):
    from lib.storage.errors import StorageError

    calls: list[dict] = []

    class _FlakyClient:
        def command(self, operation, payload, command_id, **kwargs):
            calls.append({
                "operation": operation,
                "command_id": command_id,
                **kwargs,
            })
            if len(calls) < 3:
                raise StorageError(
                    "database_timeout",
                    "Storage writer acquisition timed out", True, 25)
            return {"archiveId": payload["archive_id"]}

    plan = _capture_plan(monkeypatch, _FlakyClient())
    plan.raw_archive_capture.append_response(b"data: {}\n\n")
    result = plan.raw_archive_capture.commit(
        response_complete=True, status_code=200)

    assert result is not None
    assert len(calls) == 3
    assert all(call["operation"] == "raw_archive.put" for call in calls)
    assert all(call["priority"] == "event" for call in calls)
    assert len({call["command_id"] for call in calls}) == 1


def test_commit_gives_up_after_bounded_attempts(monkeypatch):
    from lib.storage.errors import StorageError

    calls = 0

    class _DownClient:
        def command(self, operation, payload, command_id, **kwargs):
            nonlocal calls
            calls += 1
            raise StorageError(
                "database_unavailable", "SQLite storage is unavailable",
                True, 100)

    plan = _capture_plan(monkeypatch, _DownClient())
    plan.raw_archive_capture.append_response(b"data: {}\n\n")
    assert plan.raw_archive_capture.commit(
        response_complete=True, status_code=200) is None
    assert calls == 3


def test_commit_never_retries_non_retryable_errors(monkeypatch):
    from lib.storage.errors import StorageError

    calls = 0

    class _IntegrityClient:
        def command(self, operation, payload, command_id, **kwargs):
            nonlocal calls
            calls += 1
            raise StorageError(
                "database_integrity", "SQLite integrity constraint failed")

    plan = _capture_plan(monkeypatch, _IntegrityClient())
    plan.raw_archive_capture.append_response(b"data: {}\n\n")
    assert plan.raw_archive_capture.commit(
        response_complete=True, status_code=200) is None
    assert calls == 1


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


class _RecordingSession:
    backend = "sqlite"

    def __init__(self, responses, *, meta_value=None):
        self.responses = list(responses)
        self.meta_value = meta_value
        self.statements = []
        self.executed = []

    def lock_key(self, namespace, key):
        pass

    def fetch_one(self, sql, params=()):
        self.statements.append(sql)
        if "storage_meta" in sql:
            return (
                {"meta_value": self.meta_value}
                if self.meta_value is not None else None
            )
        for marker, row in self.responses:
            if marker in sql:
                return row
        return None

    def execute(self, sql, params=()):
        self.statements.append(sql)
        self.executed.append((sql, params))
        if "INSERT INTO storage_meta" in sql or "UPDATE storage_meta" in sql:
            self.meta_value = params[-1] if "INSERT" in sql else params[0]
        return 1


def _unit_payload() -> dict:
    request = b'{"messages":[{"role":"user","content":"hi"}]}'
    response = b'data: {"choices":[]}\n\n'
    return {
        "archive_id": "raw-unit-" + uuid.uuid4().hex,
        "user_id": 1,
        "conversation_id": "conv",
        "turn_id": "turn",
        "attempt_id": "attempt",
        "task_id": "task",
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
        "summary": {"text": "Provider request/response"},
        "budget_bytes": 1024 * 1024,
        "min_free_bytes": 0,
        "available_free_bytes": 0,
    }


def test_raw_archive_put_skips_blob_readback_and_global_scan():
    """Writer-thread put stays byte-light (2026-09-05 starvation incident)."""
    from lib.storage_sidecar.operations_pkg import _raw_archives

    session = _RecordingSession(
        [
            ("storage_generation_attempts", {"attempt_id": "attempt"}),
            ("SUM(stored_bytes)", {"total": 0}),
        ],
        meta_value="0",
    )
    stored = _raw_archives._raw_archive_put(session, _unit_payload())
    assert stored["idempotentReplay"] is False
    assert stored["requestAvailable"] is True
    assert stored["storedBytes"] > 0
    for sql in session.statements:
        assert "SELECT *" not in sql
        if "SUM(stored_bytes)" in sql:
            assert "WHERE" in sql  # attempt-scoped only, never a global scan
    assert any("UPDATE storage_meta" in sql for sql in session.statements)


def test_raw_archive_usage_counter_bootstraps_from_table_once():
    """Authorities predating the counter seed it from one SUM, then reuse."""
    from lib.storage_sidecar.operations_pkg import _raw_archives

    session = _RecordingSession(
        [
            ("storage_generation_attempts", {"attempt_id": "attempt"}),
            ("SUM(stored_bytes)", {"total": 42}),
        ],
    )
    stored = _raw_archives._raw_archive_put(session, _unit_payload())
    inserts = [
        params for sql, params in session.executed
        if "INSERT INTO storage_meta" in sql
    ]
    assert inserts == [("raw_archive_stored_bytes", "42")]
    updates = [
        params for sql, params in session.executed
        if "UPDATE storage_meta" in sql
    ]
    assert updates == [
        (str(42 + stored["storedBytes"]), "raw_archive_stored_bytes"),
    ]


def test_raw_archive_delete_debits_usage_counter():
    from lib.storage_sidecar.operations_pkg import _raw_archives

    session = _RecordingSession(
        [("SUM(stored_bytes)", {"total": 40})],
        meta_value="100",
    )
    _raw_archives._debit_stored_bytes_for_delete(
        session, "attempt_id=?", ("attempt",))
    scans = [sql for sql in session.statements if "SUM(stored_bytes)" in sql]
    assert scans and all("WHERE attempt_id=?" in sql for sql in scans)
    updates = [
        params for sql, params in session.executed
        if "UPDATE storage_meta" in sql
    ]
    assert updates == [("60", "raw_archive_stored_bytes")]


def test_raw_archive_budget_stays_exact_across_replay_and_delete(chat_sidecar):
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
        stored_bytes = stored["storedBytes"]
        assert stored_bytes > 1

        replay = client.command(
            "raw_archive.put",
            _archive_payload(created, archive_id, budget_bytes=1024 * 1024),
            "put-replay:" + archive_id,
        )
        assert replay["idempotentReplay"] is True

        budget = stored_bytes + 1
        follower = client.command(
            "raw_archive.put",
            _archive_payload(
                created, "raw-" + uuid.uuid4().hex, budget_bytes=budget),
            "put:follower-" + uuid.uuid4().hex,
        )
        assert follower["truncationReason"] == "quota_exhausted"

        # A second live conversation owns the post-delete put: deleting the
        # first conversation removes its attempt rows, and put validates
        # attempt ownership.
        conversation_id2 = "raw-archive-" + uuid.uuid4().hex
        seed_conversation(conversation_id2, messages=[])
        try:
            created2 = create_turn_pair(
                conversation_id2,
                command_id="raw-create-" + uuid.uuid4().hex,
                input_projection={"content": "hello"},
                config={"model": "gpt-4o"},
                user_id=1,
            )
            bind_task(created2["attempt"]["attemptId"], "raw-task", user_id=1)
            client.command("conversation.delete", {
                "conv_id": conversation_id, "user_id": 1,
            }, "delete:" + conversation_id)
            # The delete must debit exactly the rows it removed: neither the
            # replay's double-charge nor a missing debit may linger.
            after = client.command(
                "raw_archive.put",
                _archive_payload(
                    created2, "raw-" + uuid.uuid4().hex, budget_bytes=budget),
                "put:after-" + uuid.uuid4().hex,
            )
            assert after["integrity"] == "complete"
        finally:
            delete_conversation(conversation_id2)
    finally:
        delete_conversation(conversation_id)
