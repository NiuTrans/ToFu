"""Executable cost and freshness contract for metadata burst coalescing."""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.conversations.catalog import ConversationMetadataQuery
from lib.conversations.repository import ConversationSnapshot
from runtime_guards import resolve_resource_budget


pytestmark = pytest.mark.unit


def _snapshot(user_id: int, setting: str = "projectPath") -> ConversationSnapshot:
    return ConversationSnapshot(
        metadata={
            "id": f"c-{user_id}-{setting}",
            "user_id": user_id,
            "title": "Title",
            "settings": {setting: [f"/owner/{user_id}"]},
            "created_at": 1,
            "updated_at": 2,
            "msg_count": 0,
            "rev": 3,
        },
        messages=[],
    )


def _eventually(predicate, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("condition was not reached before the bounded deadline")
        time.sleep(0.001)


class _RecordingRepository:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[dict] = []

    def list(self, **payload):
        with self._lock:
            self.calls.append(dict(payload))
        setting = (payload.get("settings_keys") or ["projectPath"])[0]
        return [_snapshot(payload["user_id"], setting)]


def test_four_arrivals_share_one_backing_query_and_receive_independent_copies():
    repository = _RecordingRepository()
    release_gather = threading.Event()
    query = ConversationMetadataQuery(
        lambda: repository,
        max_active_gathers=4,
        wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
    )

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(query.list_metadata, user_id=7) for _ in range(4)]
        _eventually(lambda: query.snapshot()["joined"] == 3)
        release_gather.set()
        results = [future.result(timeout=2.0) for future in futures]

    assert len(repository.calls) == 1
    assert query.snapshot() == {
        "capacity": 4,
        "gatherMilliseconds": 8,
        "active": 0,
        "peakActive": 1,
        "joined": 3,
        "bypassed": 0,
        "backingQueries": 1,
    }
    results[0][0]["settings"]["projectPath"].append("/mutated")
    assert results[1][0]["settings"]["projectPath"] == ["/owner/7"]


def test_owner_and_projection_shape_are_part_of_the_coalescing_key():
    repository = _RecordingRepository()
    release_gathers = threading.Event()
    query = ConversationMetadataQuery(
        lambda: repository,
        max_active_gathers=4,
        wait_for_arrivals=lambda _seconds: release_gathers.wait(2.0),
    )

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(query.list_metadata, user_id=7, settings_keys=["folderId"]),
            pool.submit(query.list_metadata, user_id=8, settings_keys=["folderId"]),
            pool.submit(query.list_metadata, user_id=7, settings_keys=["projectPath"]),
        ]
        _eventually(lambda: query.snapshot()["active"] == 3)
        release_gathers.set()
        [future.result(timeout=2.0) for future in futures]

    assert len(repository.calls) == 3
    observed_keys = {
        (call["user_id"], tuple(call["settings_keys"] or ()))
        for call in repository.calls
    }
    assert observed_keys == {
        (7, ("folderId",)),
        (8, ("folderId",)),
        (7, ("projectPath",)),
    }


def test_shared_failure_is_reclaimed_and_the_next_call_retries():
    release_gather = threading.Event()

    class _FlakyRepository(_RecordingRepository):
        def list(self, **payload):
            with self._lock:
                self.calls.append(dict(payload))
                call_count = len(self.calls)
            if call_count == 1:
                raise RuntimeError("authority unavailable")
            return [_snapshot(payload["user_id"])]

    repository = _FlakyRepository()
    query = ConversationMetadataQuery(
        lambda: repository,
        max_active_gathers=2,
        wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(query.list_metadata, user_id=7)
        second = pool.submit(query.list_metadata, user_id=7)
        _eventually(lambda: query.snapshot()["joined"] == 1)
        release_gather.set()
        with pytest.raises(RuntimeError, match="authority unavailable"):
            first.result(timeout=2.0)
        with pytest.raises(RuntimeError, match="authority unavailable"):
            second.result(timeout=2.0)

    assert query.list_metadata(user_id=7)[0]["id"].startswith("c-7-")
    assert len(repository.calls) == 2
    assert query.snapshot()["active"] == 0


def test_gather_interruption_wakes_joiners_without_running_the_repository():
    allow_failure = threading.Event()
    repository = _RecordingRepository()

    def interrupted_wait(_seconds):
        assert allow_failure.wait(2.0)
        raise RuntimeError("gather interrupted")

    query = ConversationMetadataQuery(
        lambda: repository,
        max_active_gathers=2,
        wait_for_arrivals=interrupted_wait,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(query.list_metadata, user_id=7)
        joiner = pool.submit(query.list_metadata, user_id=7)
        _eventually(lambda: query.snapshot()["joined"] == 1)
        allow_failure.set()
        for future in (leader, joiner):
            with pytest.raises(RuntimeError, match="gather interrupted"):
                future.result(timeout=2.0)

    assert repository.calls == []
    assert query.snapshot()["active"] == 0


def test_request_after_read_start_does_not_join_the_older_snapshot():
    first_read_started = threading.Event()
    release_first_read = threading.Event()

    class _BlockingRepository(_RecordingRepository):
        def list(self, **payload):
            with self._lock:
                self.calls.append(dict(payload))
                call_count = len(self.calls)
            if call_count == 1:
                first_read_started.set()
                assert release_first_read.wait(2.0)
            return [_snapshot(payload["user_id"])]

    repository = _BlockingRepository()
    query = ConversationMetadataQuery(
        lambda: repository,
        gather_seconds=0,
        wait_for_arrivals=lambda _seconds: None,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        older = pool.submit(query.list_metadata, user_id=7)
        assert first_read_started.wait(2.0)
        newer = pool.submit(query.list_metadata, user_id=7)
        assert newer.result(timeout=2.0)[0]["id"].startswith("c-7-")
        assert len(repository.calls) == 2
        release_first_read.set()
        older.result(timeout=2.0)


def test_gather_registry_is_launch_budgeted_and_capacity_fails_open():
    release_gather = threading.Event()
    repository = _RecordingRepository()
    query = ConversationMetadataQuery(
        lambda: repository,
        max_active_gathers=1,
        wait_for_arrivals=lambda _seconds: release_gather.wait(2.0),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        held = pool.submit(query.list_metadata, user_id=7)
        _eventually(lambda: query.snapshot()["active"] == 1)
        bypassed = pool.submit(query.list_metadata, user_id=8)
        assert bypassed.result(timeout=2.0)[0]["id"].startswith("c-8-")
        assert query.snapshot()["bypassed"] == 1
        assert query.snapshot()["peakActive"] == 1
        release_gather.set()
        held.result(timeout=2.0)

    assert len(repository.calls) == 2
    assert query.snapshot()["active"] == 0
    defaults = ConversationMetadataQuery().snapshot()
    assert defaults["capacity"] == resolve_resource_budget(
        "TOFU_STORAGE_RPC_CAPACITY",
        maximum=256,
    )
    assert defaults["gatherMilliseconds"] == 8
