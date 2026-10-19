"""Long-history detail reads preserve source fences and replay dependencies."""

from copy import deepcopy
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from lib.task_replay.task_event_contract import EVENT_READ_GUARD_CONTRACT, EVENT_READ_GUARD_SCOPED_CONTRACT
from lib.tasks_pkg import request_inspector as ri
from lib.tasks_pkg.snapshot_delta import SnapshotProjector, content_hash

pytestmark = pytest.mark.unit


def snapshot(number, text="original", **extra):
    return {
        "type": "messages_snapshot",
        "kind": "request",
        "roundNum": number,
        "messages": [{"role": "user", "content": text}],
        "tools": [],
        **extra,
    }


class EventStorage:
    def __init__(self, payloads):
        self.rows = [
            {"sequence": i, "event": deepcopy(p), "created_at_ms": i + 1}
            for i, p in enumerate(payloads)
        ]
        self.version = 1
        self.pages = []
        self.on_page = None
        self.fail_page = False
        self.guard_contract = EVENT_READ_GUARD_SCOPED_CONTRACT
        self.guard_types = None

    def query(self, operation, payload, **kwargs):
        if operation == "event.bounds":
            rows = [r for r in self.rows if "types" not in payload
                    or r["event"]["type"] in payload["types"]]
            bounds = {
                "retained_count": len(rows),
                "base_cursor": min((r["sequence"] for r in rows), default=0),
                "next_cursor": max((r["sequence"] + 1 for r in rows), default=0),
            }
            if payload.get("include_read_guard"):
                bounds["read_guard"] = {
                    "contract": self.guard_contract,
                    "stamp": hashlib.sha256(str(self.version).encode()).hexdigest(),
                }
                if "types" in payload:
                    bounds["read_guard"]["types"] = (self.guard_types if self.guard_types is not None
                                                       else sorted(set(payload["types"])))
            return bounds
        assert operation == "event.list"
        self.pages.append(deepcopy(payload))
        if self.fail_page:
            raise RuntimeError("Injected source read failure")
        rows = [
            deepcopy(r)
            for r in self.rows
            if r["sequence"] > payload["after_sequence"]
            and (not payload.get("types") or r["event"]["type"] in payload["types"])
        ][: payload["limit"]]
        if self.on_page:
            self.on_page(self)
        return rows


@pytest.fixture
def source(monkeypatch):
    import lib.storage

    ri._EVENTS_CACHE.clear()
    monkeypatch.setattr(ri, "_task_may_append", lambda _: False)
    monkeypatch.setattr(ri, "_child_snapshot_sources", lambda _: ([], True, False))

    def install(payloads, *, capped=True):
        storage = EventStorage(payloads)
        monkeypatch.setattr(lib.storage, "get_storage_client", lambda **_: storage)
        if capped:
            # Small sources isolate continuation from the existing prefix cap.
            rows = ri._InspectorEventRows(
                read_bound={
                    "reason": "event-limit",
                    "afterSequence": 0,
                    "structuralCount": 1,
                    "scannedCount": 1,
                }
            )
            monkeypatch.setattr(ri, "_read_events", lambda *_, **__: (rows, True))
        return storage

    yield install
    ri._EVENTS_CACHE.clear()


def test_actual_over_cap_history_serves_late_round_and_reemitted_early_round(source):
    storage = source(
        [snapshot(i) for i in range(1, 10002)] + [snapshot(1, "later replacement")],
        capped=False,
    )
    assert ri.fold_request_log("long")["coverage"] == "partial"
    assert ri.get_request_payload("long", 10001)["messages"][0]["content"] == "original"
    assert (
        ri.get_request_payload("long", 1)["messages"][0]["content"]
        == "later replacement"
    )
    assert ri.get_request_payload("long", 20000) is None
    assert storage.pages[-1]["types"] == ["messages_snapshot", "tool_wire_projection"]


def test_continuation_preserves_messages_tools_and_fold_across_pages(
    source, monkeypatch
):
    projector = SnapshotProjector()
    tools = [{"type": "function", "function": {"name": "research"}}]
    messages = []
    payloads = []
    for number in range(1, 4):
        messages = messages + [{"role": "user", "content": str(number)}]
        payloads.append(
            projector.project("long", snapshot(number, messages=messages, tools=tools))
        )
    storage = source(payloads)
    monkeypatch.setattr(ri, "_HISTORY_SLICE_EVENTS", 1)
    for _ in range(2):
        with pytest.raises(ri.RequestInspectorReadError):
            ri.get_request_payload("long", 3, fold=True)
    result = ri.get_request_payload("long", 3, fold=True)
    assert result["messages"][-1]["content"] == "3"
    assert result["tools"] == tools and result["foldPrefix"] == 2
    assert not result.get("degraded")
    assert [p["after_sequence"] for p in storage.pages] == [-1, 0, 1]
    scan = next(e[1] for k, e in ri._EVENTS_CACHE.items() if k[1] == "history")
    assert len(scan.index.records) == 2
    assert not scan.index.baselines and not scan.index.tools_by_hash


def test_v2_request_state_dependency_and_missing_baseline_remain_honest(source):
    tools = [{"name": "tool"}]
    full = snapshot(1, tools=tools, toolsHash=content_hash(tools))
    state = {
        "type": "messages_snapshot",
        "kind": "state",
        "roundNum": 2,
        "prefixLen": 1,
        "snapshotDeltaVersion": 2,
        "newMessages": [{"role": "assistant", "content": "state"}],
        "messageCount": 2,
        "toolsHash": content_hash(tools),
    }
    selected = {
        **state,
        "kind": "request",
        "roundNum": 3,
        "prefixLen": 2,
        "newMessages": [{"role": "user", "content": "next"}],
        "messageCount": 3,
    }
    source([full, state, selected])
    result = ri.get_request_payload("long", 3)
    assert len(result["messages"]) == 3 and result["tools"] == tools
    ri._EVENTS_CACHE.clear()
    source([selected])
    degraded = ri.get_request_payload("long", 3)
    assert degraded["degraded"] and "baseline" in degraded["degradedReason"]


def test_same_bounds_aba_discards_mixed_scan_then_retries_fresh(source):
    storage = source([snapshot(1, "before"), snapshot(2)])
    before = storage.query("event.bounds", {"task_id": "long"})

    def replace(current):
        current.rows[0]["event"] = snapshot(1, "after")
        current.version += 1
        current.on_page = None

    storage.on_page = replace
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 1)
    assert not any(k[1] == "history" for k in ri._EVENTS_CACHE)
    assert storage.query("event.bounds", {"task_id": "long"}) == before
    assert ri.get_request_payload("long", 1)["messages"][0]["content"] == "after"


def test_new_source_guard_invalidates_completed_selection(source):
    storage = source([snapshot(1)])
    assert ri.get_request_payload("long", 1)["messages"][0]["content"] == "original"
    storage.version += 1
    storage.rows.append(
        {"sequence": 1, "created_at_ms": 2, "event": snapshot(1, "latest")}
    )
    assert ri.get_request_payload("long", 1)["messages"][0]["content"] == "latest"


def test_wrong_guard_contract_cannot_confirm_absence(source):
    storage = source([snapshot(1)])
    storage.guard_contract = "unknown-contract"
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 100)
    assert not storage.pages


@pytest.mark.parametrize("contract,types", [
    (EVENT_READ_GUARD_CONTRACT, None),
    (EVENT_READ_GUARD_SCOPED_CONTRACT, ["messages_snapshot"]),
    (EVENT_READ_GUARD_SCOPED_CONTRACT, ["tool_wire_projection", "messages_snapshot"]),
])
def test_legacy_or_wrong_scope_cannot_confirm_absence(source, contract, types):
    storage = source([snapshot(1)])
    storage.guard_contract, storage.guard_types = contract, types
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 100)
    assert not storage.pages


def test_noise_writer_does_not_discard_admitted_checkpoint(source, monkeypatch):
    storage = source([snapshot(1, "old"), snapshot(2), snapshot(1, "latest")])
    monkeypatch.setattr(ri, "_HISTORY_SLICE_EVENTS", 1)

    def add_noise(current):
        current.rows.append({"sequence": len(current.rows), "created_at_ms": 1,
                             "event": {"type": "round_usage", "roundNum": 10}})

    storage.on_page = add_noise
    for _ in range(2):
        with pytest.raises(ri.RequestInspectorReadError):
            ri.get_request_payload("long", 1)
    assert ri.get_request_payload("long", 1)["messages"][0]["content"] == "latest"
    assert [page["after_sequence"] for page in storage.pages] == [-1, 0, 1]


def test_byte_boundary_continues_without_losing_rows(source, monkeypatch):
    source([snapshot(1), snapshot(2), snapshot(3)])
    monkeypatch.setattr(ri, "_HISTORY_SLICE_BYTES", 1)
    monkeypatch.setattr(ri, "_HISTORY_SLICE_EVENTS", 1)
    for _ in range(2):
        with pytest.raises(ri.RequestInspectorReadError):
            ri.get_request_payload("long", 3)
    assert ri.get_request_payload("long", 3)["roundNum"] == 3


def test_memory_overflow_discards_state_and_keeps_original_source(source, monkeypatch):
    storage = source([snapshot(1, "完整原文" * 10000)])
    monkeypatch.setattr(ri, "_cache_budget_bytes", lambda: 4096)
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 1)
    assert not ri._EVENTS_CACHE
    monkeypatch.setattr(ri, "_cache_budget_bytes", lambda: 1024 * 1024)
    assert (
        ri.get_request_payload("long", 1)["messages"]
        == storage.rows[0]["event"]["messages"]
    )


def test_read_failure_or_nonadvancing_row_cannot_resume_past_bad_evidence(
    source, monkeypatch
):
    storage = source([snapshot(1), snapshot(2)])
    monkeypatch.setattr(ri, "_HISTORY_SLICE_EVENTS", 1)
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 2)
    storage.fail_page = True
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 2)
    assert not ri._EVENTS_CACHE
    storage.fail_page = False
    original = storage.query

    def query(operation, payload, **kwargs):
        if operation == "event.list":
            return [dict(storage.rows[0], sequence=-1)]
        return original(operation, payload, **kwargs)

    monkeypatch.setattr(storage, "query", query)
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 2)
    assert not ri._EVENTS_CACHE


def test_same_selection_has_one_reader_and_invalidation_prevents_readmission(source):
    storage = source([snapshot(1)])
    entered, release = threading.Event(), threading.Event()

    def blocked(_):
        entered.set()
        assert release.wait(5)

    storage.on_page = blocked
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(ri.get_request_payload, "long", 1)
        assert entered.wait(5)
        try:
            with pytest.raises(ri.RequestInspectorReadError):
                ri.get_request_payload("long", 1)
            ri.invalidate_task_cache("long")
        finally:
            release.set()
        assert first.result()["roundNum"] == 1
    assert not ri._EVENTS_CACHE


def test_latest_wire_state_can_be_read_past_prefix(source):
    source([snapshot(1, "old"), snapshot(1, "revised", kind="state")])
    rows, ok = ri._read_snapshot_events("long", last=True)
    assert (
        ok
        and len(rows) == 1
        and rows[0]["payload"]["messages"][0]["content"] == "revised"
    )


def test_time_boundary_does_not_confirm_absence_and_can_resume(source, monkeypatch):
    storage = source([snapshot(1)])
    monkeypatch.setattr(ri, "_HISTORY_SLICE_SECONDS", 0)
    with pytest.raises(ri.RequestInspectorReadError):
        ri.get_request_payload("long", 100)
    assert not storage.pages
    monkeypatch.setattr(ri, "_HISTORY_SLICE_SECONDS", 2)
    assert ri.get_request_payload("long", 100) is None


def test_adaptive_native_page_limit_retries_the_same_cursor(source, monkeypatch):
    from lib.storage import StorageError

    storage = source([snapshot(1), snapshot(2)])
    original = storage.query
    rejected = []

    def query(operation, payload, **kwargs):
        if operation == "event.list" and payload["limit"] > 1:
            rejected.append((payload["after_sequence"], payload["limit"]))
            raise StorageError("database_unavailable", "event page exceeds 8 MiB")
        return original(operation, payload, **kwargs)

    monkeypatch.setattr(storage, "query", query)
    assert ri.get_request_payload("long", 2)["roundNum"] == 2
    assert rejected and all(cursor == -1 for cursor, _ in rejected)
    assert [page["after_sequence"] for page in storage.pages] == [-1, 0]
