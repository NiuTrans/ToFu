"""Executable cost, isolation, and freshness contract for snapshot bursts."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

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
