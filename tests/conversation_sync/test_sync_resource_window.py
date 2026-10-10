"""Oversized reads must make progress without losing authoritative cursors."""
from __future__ import annotations

import pytest

from lib.conversation_sync.repository import SidecarConversationSyncRepository
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class WindowClient:
    def __init__(self, result, *, capacity=24, error=None):
        self.result = result
        self.capacity = capacity
        self.error = error
        self.calls = []

    def query(self, operation, payload):
        self.calls.append((operation, dict(payload)))
        if self.error is not None:
            raise self.error
        count = payload.get("turn_limit", payload.get("limit"))
        if count is None or count > self.capacity:
            raise StorageError("database_unavailable", "database_resource_exhausted", True)
        return self.result


def repository(client):
    return SidecarConversationSyncRepository(client_factory=lambda write=False: client)


def test_oversized_snapshot_keeps_storage_cursor_attempts_and_revision():
    result = {"turns": [{"ordinal": 59}], "attempts": {"current": {}},
              "syncSequence": 78308, "conversationRevision": 123,
              "turnWindow": {"hasMore": True, "nextBeforeOrdinal": 59, "totalTurns": 83}}
    client = WindowClient(result)
    assert repository(client).snapshot("conversation", 7, turn_limit=96,
                                        include_artifact_hint=True) is result
    assert [payload["turn_limit"] for _, payload in client.calls] == [96, 48, 24]
    for operation, payload in client.calls:
        assert operation == "turn.sync.snapshot"
        assert payload["conversation_id"] == "conversation"
        assert payload["user_id"] == 7
        assert payload["include_artifact_hint"] is True


def test_oversized_history_page_preserves_exclusive_cursor_and_replay_fence():
    result = {"hasMore": True, "nextBeforeOrdinal": 35, "syncSequence": 78308}
    client = WindowClient(result, capacity=16)
    assert repository(client).turn_page("conversation", 7, lane_id="main",
        expected_sync_sequence=78308, before_ordinal=59, limit=64) is result
    assert [payload["limit"] for _, payload in client.calls] == [64, 32, 16]
    for operation, payload in client.calls:
        assert operation == "turn.sync.page"
        assert payload["conversation_id"] == "conversation"
        assert payload["user_id"] == 7
        assert payload["lane_id"] == "main"
        assert payload["before_ordinal"] == 59
        assert payload["sync_sequence"] == 78308


@pytest.mark.parametrize("code,message", [
    ("database_unavailable", "authority unavailable"),
    ("database_protocol_error", "database_resource_exhausted"),
    ("database_integrity", "damaged stored turn"),
    ("deadline_elapsed", "deadline elapsed"),
])
def test_other_failures_are_propagated_without_page_retries(code, message):
    error = StorageError(code, message)
    client = WindowClient(None, error=error)
    with pytest.raises(StorageError) as raised:
        repository(client).snapshot("conversation", 7, turn_limit=96)
    assert raised.value is error
    assert len(client.calls) == 1


@pytest.mark.parametrize("limit", [None, 0, 1, 96])
def test_unbounded_or_irreducible_exhaustion_stays_loud_and_finite(limit):
    error = StorageError("database_unavailable", "database_resource_exhausted", True)
    client = WindowClient(None, error=error)
    with pytest.raises(StorageError) as raised:
        repository(client).snapshot("conversation", 7, turn_limit=limit)
    assert raised.value is error
    counts = [payload.get("turn_limit") for _, payload in client.calls]
    assert counts == ([96, 48, 24, 12, 6, 3, 1] if limit == 96 else [limit])


def test_normal_reads_keep_full_requested_capacity():
    result = {"turnWindow": {"hasMore": False}, "turns": []}
    client = WindowClient(result, capacity=256)
    assert repository(client).snapshot("conversation", 7, turn_limit=96) is result
    assert len(client.calls) == 1


def test_reduced_page_can_report_stale_head_without_merging_old_results():
    result = {"stale": True, "syncSequence": 78309}
    client = WindowClient(result, capacity=16)
    assert repository(client).turn_page("conversation", 7, lane_id="main",
        expected_sync_sequence=78308, before_ordinal=None, limit=64) is result
    assert all("before_ordinal" not in payload for _, payload in client.calls)


def test_invalid_reduced_snapshot_still_fails_protocol_validation():
    client = WindowClient([], capacity=24)
    with pytest.raises(StorageError, match="Invalid conversation sync snapshot result"):
        repository(client).snapshot("conversation", 7, turn_limit=96)
