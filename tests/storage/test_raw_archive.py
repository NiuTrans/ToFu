"""Durable Request Inspector raw archive budgets and owner isolation."""

from __future__ import annotations

import base64
import hashlib
import uuid
import zlib

import pytest


pytestmark = pytest.mark.unit
pytest_plugins = ("tests._chat_sidecar",)


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def shed_clock(monkeypatch):
    """Each test owns the process-wide pause, its clock and the archive writer."""
    import lib.raw_archive

    clock = _Clock()
    monkeypatch.setattr(
        lib.raw_archive, "_shed_pause", lib.raw_archive._LoadShedPause(clock=clock))
    writer = lib.raw_archive._ArchiveWriter()
    monkeypatch.setattr(lib.raw_archive, "_writer", writer)
    yield clock
    writer.close(5.0)


def _settle():
    import lib.raw_archive

    assert lib.raw_archive.flush_pending_archives(5.0)


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
    assert plan.raw_archive_capture.commit(response_complete=True, status_code=200)
    _settle()

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
    assert plan.raw_archive_capture.commit(
        response_complete=True, status_code=200)
    _settle()

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
    plan.raw_archive_capture.commit(response_complete=True, status_code=200)
    _settle()
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
    plan.raw_archive_capture.commit(response_complete=True, status_code=200)
    _settle()
    assert calls == 1
    # An ordinary failure never pauses capture: the next round still commits.
    plan = _capture_plan(monkeypatch, _IntegrityClient())
    plan.raw_archive_capture.append_response(b"data: {}\n\n")
    plan.raw_archive_capture.commit(response_complete=True, status_code=200)
    _settle()
    assert calls == 2


class _CapacityClient:
    """Refuses raw_archive.put the way storage.v2 surfaces capacity exhaustion."""

    def __init__(self):
        self.calls = 0
        self.stored = 0
        self.refuse = True

    def command(self, operation, payload, command_id, **kwargs):
        from lib.storage.errors import StorageError

        self.calls += 1
        if self.refuse:
            raise StorageError(
                "database_unavailable", "database_resource_exhausted", True, 500)
        self.stored += 1
        return {"archiveId": payload["archive_id"]}


def _commit_once(monkeypatch, client):
    plan = _capture_plan(monkeypatch, client)
    plan.raw_archive_capture.append_response(b"data: {}\n\n")
    plan.raw_archive_capture.commit(response_complete=True, status_code=200)
    _settle()


def test_capacity_refusal_is_not_replayed_and_pauses_capture(monkeypatch, shed_clock):
    """A capacity refusal costs one client call, then later rounds cost none.

    Measured 2026-10-23: 3,251 refused archive commits against 2,443 model
    rounds after a restart, each refusal replayed by the client loop and again
    three times here, on the provider call's thread before its result returned.
    """
    client = _CapacityClient()
    _commit_once(monkeypatch, client)
    assert client.calls == 1  # the client loop already replayed it
    for _ in range(5):
        _commit_once(monkeypatch, client)
    assert client.calls == 1  # paused rounds perform no RPC

    shed_clock.now += 31  # the first window (30 s) has ended: one probe
    _commit_once(monkeypatch, client)
    assert client.calls == 2
    shed_clock.now += 45  # the refused probe doubled the window to 60 s
    _commit_once(monkeypatch, client)
    assert client.calls == 2
    shed_clock.now += 16
    client.refuse = False
    _commit_once(monkeypatch, client)  # probe succeeds
    assert (client.calls, client.stored) == (3, 1)
    _commit_once(monkeypatch, client)  # capture resumed
    assert (client.calls, client.stored) == (4, 2)


def test_deterministic_resource_bound_neither_replays_nor_pauses_capture(
        monkeypatch, shed_clock):
    """Only retryable load shedding pauses capture. A per-request bound
    (non-retryable domain_resource_bound) fails that one archive; the next
    round still commits."""
    from lib.storage.errors import StorageError

    class _BoundClient(_CapacityClient):
        def command(self, operation, payload, command_id, **kwargs):
            self.calls += 1
            if self.refuse:
                raise StorageError(
                    "database_resource_exhausted",
                    "database_resource_exhausted")
            self.stored += 1
            return {"archiveId": payload["archive_id"]}

    client = _BoundClient()
    _commit_once(monkeypatch, client)
    assert client.calls == 1
    client.refuse = False
    _commit_once(monkeypatch, client)
    assert (client.calls, client.stored) == (2, 1)


def test_one_probe_per_window_while_paused(shed_clock):
    import lib.raw_archive
    from lib.storage.errors import StorageError

    pause = lib.raw_archive._shed_pause
    pause.record_refusal(StorageError(
        "database_unavailable", "database_resource_exhausted", True, 500))
    assert not pause.admit()
    shed_clock.now += 30
    assert pause.admit()       # the probe reserves the next window
    assert not pause.admit()   # concurrent rounds keep skipping meanwhile
    pause.record_success()
    assert pause.admit() and pause.admit()


class _BlockedClient:
    """An authority that holds every raw_archive.put until released."""

    def __init__(self):
        import threading

        self.release = threading.Event()
        self.entered = threading.Event()
        self.archive_ids: list[str] = []

    def command(self, operation, payload, command_id, **kwargs):
        self.entered.set()
        assert self.release.wait(10)
        self.archive_ids.append(payload["archive_id"])
        return {"archiveId": payload["archive_id"]}


def test_commit_returns_before_the_authority_answers(monkeypatch):
    """A stalled authority never holds the provider call or the event loop."""
    import time

    client = _BlockedClient()
    plan = _capture_plan(monkeypatch, client)
    plan.raw_archive_capture.append_response(b"data: {}\n\n")
    started = time.monotonic()
    assert plan.raw_archive_capture.commit(response_complete=True, status_code=200)
    assert time.monotonic() - started < 1.0
    assert client.entered.wait(5)  # the writer, not the caller, is waiting
    assert client.archive_ids == []
    client.release.set()
    _settle()
    assert client.archive_ids == [plan.raw_archive_capture.archive_id]


def test_pending_captures_are_bounded_by_resident_bytes(monkeypatch):
    import lib.raw_archive

    writer = lib.raw_archive._ArchiveWriter(memory_limit_bytes=1)
    monkeypatch.setattr(lib.raw_archive, "_writer", writer)
    client = _BlockedClient()
    plans = [_capture_plan(monkeypatch, client) for _ in range(3)]
    for plan in plans:
        plan.raw_archive_capture.append_response(b"data: {}\n\n")
    assert plans[0].raw_archive_capture.commit(response_complete=True)
    assert client.entered.wait(5)  # the first capture left the queue
    # An empty queue admits one capture even above the bound; the next waits
    # behind it and exceeds the bound, so it is dropped instead of retained.
    assert plans[1].raw_archive_capture.commit(response_complete=True)
    assert not plans[2].raw_archive_capture.commit(response_complete=True)
    assert plans[2].raw_archive_capture._response_file.closed
    client.release.set()
    assert writer.flush(5.0)
    assert client.archive_ids == [
        plans[0].raw_archive_capture.archive_id,
        plans[1].raw_archive_capture.archive_id,
    ]
    writer.close(5.0)


def test_closed_writer_releases_pending_and_later_captures(monkeypatch):
    import lib.raw_archive

    client = _BlockedClient()
    plans = [_capture_plan(monkeypatch, client) for _ in range(3)]
    assert plans[0].raw_archive_capture.commit(response_complete=True)
    assert client.entered.wait(5)
    assert plans[1].raw_archive_capture.commit(response_complete=True)
    assert not lib.raw_archive._writer.close(0.05)  # the authority is stalled
    assert plans[1].raw_archive_capture._response_file.closed
    assert not plans[2].raw_archive_capture.commit(response_complete=True)
    assert plans[2].raw_archive_capture._response_file.closed
    client.release.set()


def test_pause_window_is_bounded(shed_clock):
    import lib.raw_archive
    from lib.storage.errors import StorageError

    pause = lib.raw_archive._shed_pause
    refusal = StorageError(
        "database_unavailable", "database_resource_exhausted", True, 500)
    for _ in range(12):
        pause.record_refusal(refusal)
    shed_clock.now += lib.raw_archive._SHED_PAUSE_MAX_S
    assert pause.admit()


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

        # Conversation deletion removes its archives with it.
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


class _ReadClient:
    def __init__(self, result):
        self.result = result
        self.reads = []

    def query(self, operation, payload, **_kwargs):
        self.reads.append((operation, payload))
        return self.result


def test_reclaimed_archive_reference_reads_as_explicit_expired_state(monkeypatch):
    """Retention reclaims the oldest archives while references to them live on
    in error envelopes and earlier payloads; reading one is a state, not 404."""
    import lib.storage
    from lib.tasks_pkg.request_inspector import get_raw_archive_chunk

    client = _ReadClient(None)
    monkeypatch.setattr(lib.storage, "get_storage_client", lambda **_kw: client)
    chunk = get_raw_archive_chunk(
        "task-1", "raw-reclaimed", "response", user_id=1, offset=4096)
    assert chunk == {
        "archive": None,
        "archiveId": "raw-reclaimed",
        "part": "response",
        "state": "expired",
        "offset": 4096,
        "nextOffset": 4096,
        "hasMore": False,
        "availableBytes": 0,
        "dataBase64": "",
    }
    assert client.reads == [("raw_archive.read", {
        "user_id": 1, "task_id": "task-1", "archive_id": "raw-reclaimed",
        "part": "response", "offset": 4096, "limit": 256 * 1024,
    })]


def test_retained_archive_chunk_is_marked_retained(monkeypatch):
    import lib.storage
    from lib.tasks_pkg.request_inspector import get_raw_archive_chunk

    stored = {"archive": {"archiveId": "raw-kept"}, "part": "request",
              "offset": 0, "nextOffset": 2, "hasMore": False,
              "availableBytes": 2, "dataBase64": "aGk="}
    monkeypatch.setattr(
        lib.storage, "get_storage_client", lambda **_kw: _ReadClient(stored))
    assert get_raw_archive_chunk("task-1", "raw-kept", "request", user_id=1) == {
        **stored, "archiveId": "raw-kept", "state": "retained"}


def test_raw_archive_put_skips_blob_readback_and_global_scan():
    """Writer-thread put stays byte-light (2026-09-05 starvation incident)."""
    from lib.storage_authority_sql.operations_pkg import _raw_archives

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
    from lib.storage_authority_sql.operations_pkg import _raw_archives

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
    from lib.storage_authority_sql.operations_pkg import _raw_archives

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
