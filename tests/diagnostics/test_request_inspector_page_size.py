"""Materialized native page-size rejection must not masquerade as outage."""
import time

import pytest

from lib.tasks_pkg import request_inspector as ri
from lib.tasks_pkg.request_inspector_history import SnapshotHistoryRead

pytestmark = pytest.mark.unit

SIZE_REFUSALS = [
    pytest.param("storage_payload_too_large", id="payload-too-large"),
    # Engines built before the stream point-read bound was classified report
    # it as a protocol refusal whose sanitized message is only its code.
    pytest.param("database_protocol_error", id="unclassified-engine-refusal"),
]


class PageError(Exception):
    def __init__(self, code, message=None):
        super().__init__(code if message is None else message)
        self.code = code


class Pages:
    def __init__(self, code="storage_payload_too_large", message=None, reject_single=False):
        self.code = code
        self.message = message
        self.reject_single = reject_single
        self.calls = []
        self.rows = [
            {"sequence": sequence, "event": {
                "type": "messages_snapshot", "kind": "request",
                "roundNum": sequence + 1, "messages": [
                    {"role": "user", "content": f"exact-{sequence}"}],
            }} for sequence in [0, 7, 19]
        ]

    def query(self, operation, args, **kwargs):
        assert operation == "event.list"
        after, limit = args["after_sequence"], args["limit"]
        self.calls.append((after, limit))
        if limit > 1 or self.reject_single:
            raise PageError(self.code, self.message)
        return [row for row in self.rows if row["sequence"] > after][:limit]


@pytest.mark.parametrize("code", SIZE_REFUSALS)
@pytest.mark.parametrize("reader", ["structural", "history"])
def test_page_size_retries_same_cursor_and_returns_complete_exact_payload(monkeypatch, reader, code):
    pages = Pages(code)
    if reader == "structural":
        import lib.storage
        monkeypatch.setattr(lib.storage, "get_storage_client", lambda: pages)
        rows, ok = ri._read_events_uncached("page-test", rebuild=False)
        assert ok
        assert [r["event_id"] for r in rows] == [0, 7, 19]
        assert [r["payload"]["messages"][0]["content"] for r in rows] == [
            "exact-0", "exact-7", "exact-19"]
    else:
        scan = SnapshotHistoryRead("page-test", {"next_cursor": 20},
            rounds=None, turn="", kind="request", last=False)
        assert scan.advance(pages, kind_of=ri._snapshot_kind, measure=lambda *_: 0,
            work_budget=100000, event_budget=10000, byte_budget=100000,
            deadline=time.monotonic() + 10)
        assert [r["payload"]["messages"][0]["content"] for r in scan.event_rows()] == [
            "exact-0", "exact-7", "exact-19"]
    assert pages.calls[:10] == [(-1, limit) for limit in [
        1000, 500, 250, 125, 62, 31, 15, 7, 3, 1]]


@pytest.mark.parametrize("code,message", [
    ("database_unavailable", None),
    ("database_integrity", None),
    ("database_resource_exhausted", None),
    # A protocol failure that carries client or fence text is not the
    # engine's text-free bound refusal.
    ("database_protocol_error", "Storage response correlation failed"),
])
def test_other_failures_are_not_size_retry_candidates(monkeypatch, code, message):
    import lib.storage
    pages = Pages(code, message)
    monkeypatch.setattr(lib.storage, "get_storage_client", lambda: pages)
    assert ri._read_events_uncached("page-failure", rebuild=False) == ([], False)
    assert pages.calls == [(-1, 1000)]


@pytest.mark.parametrize("code", SIZE_REFUSALS)
def test_single_row_size_rejection_stops_without_partial_success(monkeypatch, code):
    import lib.storage
    pages = Pages(code, reject_single=True)
    monkeypatch.setattr(lib.storage, "get_storage_client", lambda: pages)
    assert ri._read_events_uncached("page-single", rebuild=False) == ([], False)
    assert pages.calls[-1] == (-1, 1)
    assert len(pages.calls) == 10
