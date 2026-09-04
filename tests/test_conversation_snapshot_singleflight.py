"""Executable cost, isolation, and freshness contract for snapshot bursts."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import orjson
import pytest

from lib.conversation_sync.service import ConversationSyncService
from lib.conversation_sync.snapshot_query import ConversationSnapshotQuery
from runtime_guards import resolve_resource_budget


pytestmark = pytest.mark.unit


def _eventually(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition was not reached before the bounded deadline")
        time.sleep(0.001)


def _stored_snapshot(marker: str = "authority") -> dict:
    return {
        "conversationRevision": 7,
        "syncSequence": 11,
        "settings": {"marker": marker},
        "turns": [],
        "attempts": [],
        "queueItems": [],
    }


class _RecordingRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[tuple[str, int]] = []

    def snapshot(self, conversation_id: str, user_id: int):
        with self._lock:
            self.calls.append((conversation_id, user_id))
        return _stored_snapshot(f"{user_id}:{conversation_id}")

    def changes(self, *args, **kwargs):
        raise AssertionError("snapshot tests must not read replay changes")


def test_service_shares_four_arrivals_but_isolates_hints_and_envelopes():
    repository = _RecordingRepository()
    release_gather = threading.Event()
    query_box: list[ConversationSnapshotQuery] = []

    def create_query(loader):
        query = ConversationSnapshotQuery(
            loader,
            max_active_gathers=4,
            wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
        )
        query_box.append(query)
        return query

    service = ConversationSyncService(
        repository,
        snapshot_query_factory=create_query,
    )
    hints = [True, False, True, False]

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                service.snapshot,
                "conv-a",
                7,
                push_withheld=hint,
            )
            for hint in hints
        ]
        _eventually(lambda: query_box[0].snapshot()["joined"] == 3)
        release_gather.set()
        results = [future.result(timeout=2.0) for future in futures]

    assert repository.calls == [("conv-a", 7)]
    assert [result["pushWithheld"] for result in results] == hints
    assert query_box[0].snapshot() == {
        "capacity": 4,
        "gatherMilliseconds": 8,
        "active": 0,
        "peakActive": 1,
        "joined": 3,
        "bypassed": 0,
        "backingSnapshots": 1,
    }
    results[0]["settings"] = {"marker": "mutated"}
    assert results[1]["settings"]["marker"] == "7:conv-a"


def test_reference_arrivals_share_one_flight_lifetime_projection(monkeypatch):
    import lib.conversation_sync.service as service_module

    repository = _RecordingRepository()
    release_authority = threading.Event()
    projection_started = threading.Event()
    release_projection = threading.Event()
    projection_count = 0
    projection_lock = threading.Lock()
    query_box: list[ConversationSnapshotQuery] = []
    original_projection = service_module.snapshot_with_reference_tool_segments

    projection_scopes = []

    def counted_projection(snapshot, *, owner_cache_scope):
        nonlocal projection_count
        with projection_lock:
            projection_count += 1
            projection_scopes.append(owner_cache_scope)
        projection_started.set()
        assert release_projection.wait(2.0)
        return original_projection(
            snapshot, owner_cache_scope=owner_cache_scope,
        )

    monkeypatch.setattr(
        service_module,
        "snapshot_with_reference_tool_segments",
        counted_projection,
    )

    def create_query(loader):
        query = ConversationSnapshotQuery(
            loader,
            max_active_gathers=4,
            wait_for_arrivals=lambda _seconds: release_authority.wait(2.0),
        )
        query_box.append(query)
        return query

    service = ConversationSyncService(
        repository,
        snapshot_query_factory=create_query,
    )
    hints = [True, False, True, False]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(
                service.snapshot,
                "conv-a",
                7,
                push_withheld=hint,
                segment_payload="refs",
            )
            for hint in hints
        ]
        _eventually(lambda: query_box[0].snapshot()["joined"] == 3)
        release_authority.set()
        assert projection_started.wait(2.0)
        assert projection_count == 1
        release_projection.set()
        results = [future.result(timeout=2.0) for future in futures]

    assert repository.calls == [("conv-a", 7)]
    assert projection_count == 1
    from lib.conversation_sync.turn_images import turn_image_owner_scope
    assert projection_scopes == [turn_image_owner_scope(7, "conv-a")]
    assert [result["pushWithheld"] for result in results] == hints
    assert len({id(result) for result in results}) == 4
    assert all(result["turns"] is results[0]["turns"] for result in results)


def test_full_and_reference_callers_share_authority_without_nested_mutation():
    from lib.conversation_sync.validation import decode

    release_gather = threading.Event()
    query_box: list[ConversationSnapshotQuery] = []
    large_result = "result" * 10_000

    class ToolRepository(_RecordingRepository):
        def snapshot(self, conversation_id: str, user_id: int):
            stored = super().snapshot(conversation_id, user_id)
            stored["turns"] = [{
                "turnId": "turn-a",
                "conversationId": conversation_id,
                "laneId": "main",
                "ordinal": 1,
                "actor": "assistant",
                "kind": "reply",
                "runId": "run-a",
                "status": "completed",
                "projection": {
                    "content": "done",
                    "toolRounds": [{
                        "toolCallId": "call-a",
                        "toolName": "research",
                        "toolArgs": {"query": "shared"},
                        "toolContent": large_result,
                        "status": "done",
                        "_responsesItems": [{
                            "type": "reasoning",
                            "encrypted_content": "private" * 1_000,
                        }],
                    }],
                    "segments": [{
                        "type": "tool_use",
                        "blockId": "tool:call-a",
                        "id": "call-a",
                        "name": "research",
                        "input": {"query": "shared"},
                        "result": {"content": large_result, "status": "done"},
                    }],
                },
                "projectionRevision": 1,
                "settlement": {},
                "createdAt": 1,
                "updatedAt": 1,
            }]
            return stored

    def create_query(loader):
        query = ConversationSnapshotQuery(
            loader,
            max_active_gathers=2,
            wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
        )
        query_box.append(query)
        return query

    repository = ToolRepository()
    service = ConversationSyncService(
        repository,
        snapshot_query_factory=create_query,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        full_future = pool.submit(
            service.snapshot, "conv-a", 7, segment_payload="full",
        )
        refs_future = pool.submit(
            service.snapshot, "conv-a", 7, segment_payload="refs",
        )
        _eventually(lambda: query_box[0].snapshot()["joined"] == 1)
        release_gather.set()
        full = full_future.result(timeout=2.0)
        referenced = refs_future.result(timeout=2.0)

    assert repository.calls == [("conv-a", 7)]
    full_projection = full["turns"][0]["projection"]
    reference_projection = referenced["turns"][0]["projection"]
    assert reference_projection["toolRounds"] is not full_projection["toolRounds"]
    assert reference_projection["toolRounds"][0]["toolContent"] is (
        full_projection["toolRounds"][0]["toolContent"]
    )
    assert "_responsesItems" in full_projection["toolRounds"][0]
    assert "_responsesItems" not in reference_projection["toolRounds"][0]
    assert full_projection["segments"][0]["input"] == {"query": "shared"}
    assert full_projection["segments"][0]["result"]["content"] == large_result
    assert "roundRef" not in full_projection["segments"][0]
    assert reference_projection["segments"][0] == {
        "type": "tool_use",
        "blockId": "tool:call-a",
        "id": "call-a",
        "name": "research",
        "result": {},
        "roundRef": "call-a",
    }
    assert decode("ConversationSyncSnapshot", referenced) == referenced


def test_reference_snapshot_content_addresses_repeated_large_tool_documents():
    from lib.conversation_sync.validation import decode
    from lib.turn_projection_segments import snapshot_with_reference_tool_segments

    large_content = "shared research result\n" * 3_000
    large_results = [{"title": "shared source", "body": "evidence" * 5_000}]

    class RepeatedToolRepository(_RecordingRepository):
        def snapshot(self, conversation_id: str, user_id: int):
            stored = super().snapshot(conversation_id, user_id)
            turns = []
            for ordinal in (1, 2):
                turns.append({
                    "turnId": f"turn-{ordinal}",
                    "conversationId": conversation_id,
                    "laneId": "main",
                    "ordinal": ordinal,
                    "actor": "assistant",
                    "kind": "reply",
                    "runId": f"run-{ordinal}",
                    "status": "completed",
                    "projection": {
                        "content": "done",
                        "toolRounds": [{
                            "toolCallId": f"call-{ordinal}",
                            "toolName": "search_tools",
                            "toolArgs": "",
                            "toolContent": large_content,
                            "results": large_results,
                            "status": "done",
                        }],
                        "segments": [],
                    },
                    "projectionRevision": 1,
                    "settlement": {},
                    "createdAt": ordinal,
                    "updatedAt": ordinal,
                })
            stored["turns"] = turns
            return stored

    service = ConversationSyncService(RepeatedToolRepository())
    full = service.snapshot("conv-a", 7, segment_payload="full")
    referenced = service.snapshot("conv-a", 7, segment_payload="refs")

    shared = referenced["sharedToolDocuments"]
    assert len(shared) == 2
    reference_rounds = [
        turn["projection"]["toolRounds"][0]
        for turn in referenced["turns"]
    ]
    assert reference_rounds[0]["_snapshotDocumentRefs"] == (
        reference_rounds[1]["_snapshotDocumentRefs"]
    )
    for round_record in reference_rounds:
        refs = round_record["_snapshotDocumentRefs"]
        assert "toolContent" not in round_record
        assert "results" not in round_record
        assert shared[refs["toolContent"]] == large_content
        assert shared[refs["results"]] == large_results
    assert all(
        turn["projection"]["segments"][0]["roundRef"]
        == f"call-{turn['ordinal']}"
        for turn in referenced["turns"]
    )

    full_rounds = [turn["projection"]["toolRounds"][0] for turn in full["turns"]]
    assert "sharedToolDocuments" not in full
    assert [round_record["toolContent"] for round_record in full_rounds] == [
        large_content,
        large_content,
    ]
    assert decode("ConversationSyncSnapshot", referenced) == referenced
    assert len(orjson.dumps(referenced)) < len(orjson.dumps(full)) * 0.6

    running = {
        **full,
        "turns": [{**turn, "status": "running"} for turn in full["turns"]],
    }
    running_reference = snapshot_with_reference_tool_segments(running)
    assert "sharedToolDocuments" not in running_reference
    assert all(
        "toolContent" in turn["projection"]["toolRounds"][0]
        for turn in running_reference["turns"]
    )

    interrupted = {
        **full,
        "turns": [
            {**turn, "status": "interrupted"}
            for turn in full["turns"]
        ],
    }
    interrupted_reference = snapshot_with_reference_tool_segments(interrupted)
    assert len(interrupted_reference["sharedToolDocuments"]) == 2


def test_reference_snapshot_reuses_large_unique_projection_content():
    from lib.conversation_sync.validation import decode
    from lib.turn_projection_segments import snapshot_with_reference_tool_segments

    large_content = "verified long-form answer\n" * 4_000

    class ContentRepository(_RecordingRepository):
        def snapshot(self, conversation_id: str, user_id: int):
            stored = super().snapshot(conversation_id, user_id)
            stored["turns"] = [{
                "turnId": "turn-a",
                "conversationId": conversation_id,
                "laneId": "main",
                "ordinal": 1,
                "actor": "assistant",
                "kind": "reply",
                "runId": "run-a",
                "status": "completed",
                "projection": {"content": large_content, "segments": []},
                "projectionRevision": 1,
                "settlement": {},
                "createdAt": 1,
                "updatedAt": 1,
            }]
            return stored

    service = ConversationSyncService(ContentRepository())
    full = service.snapshot("conv-a", 7, segment_payload="full")
    referenced = service.snapshot("conv-a", 7, segment_payload="refs")

    full_projection = full["turns"][0]["projection"]
    referenced_projection = referenced["turns"][0]["projection"]
    source_segment = referenced_projection["segments"][0]
    assert referenced["snapshotProjectionRefs"] == {
        "turn-a": {"content": source_segment["blockId"]},
    }
    assert "content" not in referenced_projection
    assert source_segment["text"] == large_content
    assert full_projection["content"] == large_content
    assert "snapshotProjectionRefs" not in full
    assert decode("ConversationSyncSnapshot", referenced) is referenced
    assert len(orjson.dumps(referenced)) < len(orjson.dumps(full)) * 0.7

    running = {
        **full,
        "turns": [{**full["turns"][0], "status": "running"}],
    }
    running_reference = snapshot_with_reference_tool_segments(running)
    assert "snapshotProjectionRefs" not in running_reference
    assert running_reference["turns"][0]["projection"]["content"] == large_content

    ambiguous_projection = {
        **full_projection,
        "segments": [
            *full_projection["segments"],
            dict(full_projection["segments"][0]),
        ],
    }
    ambiguous = {
        **full,
        "turns": [{**full["turns"][0], "projection": ambiguous_projection}],
    }
    ambiguous_reference = snapshot_with_reference_tool_segments(ambiguous)
    assert "snapshotProjectionRefs" not in ambiguous_reference
    assert ambiguous_reference["turns"][0]["projection"]["content"] == large_content

    reused_block_id = {
        "type": "thinking",
        "blockId": full_projection["segments"][0]["blockId"],
        "text": "different segment authority",
    }
    colliding = {
        **full,
        "turns": [{
            **full["turns"][0],
            "projection": {
                **full_projection,
                "segments": [*full_projection["segments"], reused_block_id],
            },
        }],
    }
    colliding_reference = snapshot_with_reference_tool_segments(colliding)
    assert "snapshotProjectionRefs" not in colliding_reference
    assert (
        colliding_reference["turns"][0]["projection"]["content"]
        == large_content
    )


def test_reference_snapshot_reuses_unique_round_thinking_segment():
    from lib.conversation_sync.validation import decode
    from lib.turn_projection_segments import snapshot_with_reference_tool_segments

    large_thinking = "careful private reasoning\n" * 2_000
    tool_args = {"query": "evidence"}

    class ThinkingRepository(_RecordingRepository):
        def snapshot(self, conversation_id: str, user_id: int):
            stored = super().snapshot(conversation_id, user_id)
            stored["turns"] = [{
                "turnId": "turn-a",
                "conversationId": conversation_id,
                "laneId": "main",
                "ordinal": 1,
                "actor": "assistant",
                "kind": "reply",
                "runId": "run-a",
                "status": "completed",
                "projection": {
                    "content": "done",
                    "toolRounds": [{
                        "toolCallId": "call-a",
                        "toolName": "search",
                        "toolArgs": tool_args,
                        "toolContent": "found",
                        "thinking": large_thinking,
                        "status": "done",
                    }],
                    "segments": [{
                        "type": "thinking",
                        "blockId": "thinking:round-a",
                        "text": large_thinking,
                    }, {
                        "type": "tool_use",
                        "blockId": "tool:call-a",
                        "id": "call-a",
                        "name": "search",
                        "input": tool_args,
                        "result": {"content": "found", "status": "done"},
                    }, {
                        "type": "text",
                        "blockId": "text:terminal",
                        "text": "done",
                    }],
                },
                "projectionRevision": 1,
                "settlement": {},
                "createdAt": 1,
                "updatedAt": 1,
            }]
            return stored

    service = ConversationSyncService(ThinkingRepository())
    full = service.snapshot("conv-a", 7, segment_payload="full")
    referenced = service.snapshot("conv-a", 7, segment_payload="refs")
    full_projection = full["turns"][0]["projection"]
    referenced_projection = referenced["turns"][0]["projection"]

    assert referenced["snapshotProjectionRefs"] == {
        "turn-a": {
            "roundThinking": {"call-a": "thinking:round-a"},
        },
    }
    assert "thinking" not in referenced_projection["toolRounds"][0]
    assert referenced_projection["segments"][0]["text"] == large_thinking
    assert full_projection["toolRounds"][0]["thinking"] == large_thinking
    assert decode("ConversationSyncSnapshot", referenced) is referenced
    assert len(orjson.dumps(referenced)) < len(orjson.dumps(full)) * 0.7

    running = {
        **full,
        "turns": [{**full["turns"][0], "status": "running"}],
    }
    running_reference = snapshot_with_reference_tool_segments(running)
    assert "snapshotProjectionRefs" not in running_reference
    assert (
        running_reference["turns"][0]["projection"]["toolRounds"][0][
            "thinking"
        ]
        == large_thinking
    )

    duplicate_thinking = dict(full_projection["segments"][0])
    ambiguous = {
        **full,
        "turns": [{
            **full["turns"][0],
            "projection": {
                **full_projection,
                "segments": [*full_projection["segments"], duplicate_thinking],
            },
        }],
    }
    ambiguous_reference = snapshot_with_reference_tool_segments(ambiguous)
    assert "snapshotProjectionRefs" not in ambiguous_reference
    assert (
        ambiguous_reference["turns"][0]["projection"]["toolRounds"][0][
            "thinking"
        ]
        == large_thinking
    )


def test_owner_and_conversation_identity_never_share_a_snapshot():
    release_gathers = threading.Event()
    calls: list[tuple[str, int]] = []
    call_lock = threading.Lock()

    def load(conversation_id: str, user_id: int) -> dict:
        with call_lock:
            calls.append((conversation_id, user_id))
        return {"pushWithheld": False, "nested": {"owner": user_id}}

    query = ConversationSnapshotQuery(
        load,
        max_active_gathers=4,
        wait_for_arrivals=lambda _seconds: release_gathers.wait(2.0),
    )
    keys = [("conv-a", 7), ("conv-a", 8), ("conv-b", 7)]

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(query.read, conv_id, user_id, push_withheld=False)
            for conv_id, user_id in keys
        ]
        _eventually(lambda: query.snapshot()["active"] == 3)
        release_gathers.set()
        [future.result(timeout=2.0) for future in futures]

    assert set(calls) == set(keys)
    assert len(calls) == 3


def test_full_and_bounded_windows_never_share_snapshot_authority():
    release_gathers = threading.Event()
    calls: list[int | None] = []
    call_lock = threading.Lock()

    def load(
        _conversation_id: str,
        _user_id: int,
        turn_limit: int | None = None,
    ) -> dict:
        with call_lock:
            calls.append(turn_limit)
        return {"pushWithheld": False, "turnLimit": turn_limit}

    query = ConversationSnapshotQuery(
        load,
        max_active_gathers=2,
        wait_for_arrivals=lambda _seconds: release_gathers.wait(2.0),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        full = pool.submit(
            query.read, "conv-a", 7, push_withheld=False
        )
        bounded = pool.submit(
            query.read,
            "conv-a",
            7,
            push_withheld=False,
            turn_limit=96,
        )
        _eventually(lambda: query.snapshot()["active"] == 2)
        release_gathers.set()
        assert full.result(timeout=2.0)["turnLimit"] is None
        assert bounded.result(timeout=2.0)["turnLimit"] == 96

    assert sorted(calls, key=lambda value: -1 if value is None else value) == [
        None, 96
    ]
    assert query.snapshot()["backingSnapshots"] == 2


def test_artifact_hint_opt_in_never_changes_an_older_client_flight():
    release_gathers = threading.Event()
    calls: list[bool] = []
    call_lock = threading.Lock()

    def load(
        _conversation_id: str,
        _user_id: int,
        _turn_limit: int | None = None,
        *,
        include_artifact_hint: bool = False,
    ) -> dict:
        with call_lock:
            calls.append(include_artifact_hint)
        return {
            "pushWithheld": False,
            **({"hasArtifacts": False} if include_artifact_hint else {}),
        }

    query = ConversationSnapshotQuery(
        load,
        max_active_gathers=2,
        wait_for_arrivals=lambda _seconds: release_gathers.wait(2.0),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        legacy = pool.submit(
            query.read, "conv-a", 7, push_withheld=False,
        )
        hinted = pool.submit(
            query.read,
            "conv-a",
            7,
            push_withheld=False,
            include_artifact_hint=True,
        )
        _eventually(lambda: query.snapshot()["active"] == 2)
        release_gathers.set()
        assert "hasArtifacts" not in legacy.result(timeout=2.0)
        assert hinted.result(timeout=2.0)["hasArtifacts"] is False

    assert sorted(calls) == [False, True]
    assert query.snapshot()["backingSnapshots"] == 2


def test_failure_is_shared_reclaimed_and_retried():
    release_gather = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def load(_conversation_id: str, _user_id: int) -> dict:
        nonlocal call_count
        with call_lock:
            call_count += 1
            current = call_count
        if current == 1:
            raise RuntimeError("snapshot authority unavailable")
        return {"pushWithheld": False}

    query = ConversationSnapshotQuery(
        load,
        max_active_gathers=2,
        wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(query.read, "conv-a", 7, push_withheld=False)
        second = pool.submit(query.read, "conv-a", 7, push_withheld=True)
        _eventually(lambda: query.snapshot()["joined"] == 1)
        release_gather.set()
        for future in (first, second):
            with pytest.raises(RuntimeError, match="snapshot authority unavailable"):
                future.result(timeout=2.0)

    assert query.snapshot()["active"] == 0
    assert query.read("conv-a", 7, push_withheld=True)["pushWithheld"] is True
    assert call_count == 2


def test_request_after_read_start_gets_a_newer_authority_read():
    first_read_started = threading.Event()
    release_first_read = threading.Event()
    call_count = 0
    call_lock = threading.Lock()

    def load(_conversation_id: str, _user_id: int) -> dict:
        nonlocal call_count
        with call_lock:
            call_count += 1
            current = call_count
        if current == 1:
            first_read_started.set()
            assert release_first_read.wait(2.0)
        return {"pushWithheld": False, "revision": current}

    query = ConversationSnapshotQuery(
        load,
        gather_seconds=0,
        wait_for_arrivals=lambda _seconds: None,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(query.read, "conv-a", 7, push_withheld=False)
        assert first_read_started.wait(2.0)
        newer = pool.submit(query.read, "conv-a", 7, push_withheld=False)
        assert newer.result(timeout=2.0)["revision"] == 2
        release_first_read.set()
        assert older.result(timeout=2.0)["revision"] == 1

    assert call_count == 2


def test_sequential_reference_reads_rebuild_without_a_completed_value_cache():
    authority_reads = 0
    projections = 0

    def load(_conversation_id: str, _user_id: int) -> dict:
        nonlocal authority_reads
        authority_reads += 1
        return {"pushWithheld": False, "revision": authority_reads}

    def project(authority: dict) -> dict:
        nonlocal projections
        projections += 1
        return {**authority, "representation": "refs"}

    query = ConversationSnapshotQuery(
        load,
        gather_seconds=0,
        wait_for_arrivals=lambda _seconds: None,
    )
    first = query.read(
        "conv-a",
        7,
        push_withheld=False,
        representation="refs",
        project_representation=project,
    )
    second = query.read(
        "conv-a",
        7,
        push_withheld=False,
        representation="refs",
        project_representation=project,
    )

    assert [first["revision"], second["revision"]] == [1, 2]
    assert authority_reads == projections == 2


def test_registry_uses_launch_budget_and_fails_open_at_capacity():
    release_gather = threading.Event()
    calls: list[tuple[str, int]] = []
    query = ConversationSnapshotQuery(
        lambda conversation_id, user_id: (
            calls.append((conversation_id, user_id))
            or {"pushWithheld": False}
        ),
        max_active_gathers=1,
        wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(query.read, "conv-a", 7, push_withheld=False)
        _eventually(lambda: query.snapshot()["active"] == 1)
        bypassed = pool.submit(query.read, "conv-b", 7, push_withheld=False)
        assert bypassed.result(timeout=2.0)["pushWithheld"] is False
        assert query.snapshot()["bypassed"] == 1
        release_gather.set()
        held.result(timeout=2.0)

    assert len(calls) == 2
    defaults = ConversationSnapshotQuery(
        lambda _conversation_id, _user_id: {"pushWithheld": False}
    ).snapshot()
    assert defaults["capacity"] == resolve_resource_budget(
        "TOFU_STORAGE_RPC_CAPACITY",
        maximum=256,
    )
    assert defaults["gatherMilliseconds"] == 8
