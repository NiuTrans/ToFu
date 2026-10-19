"""Same-process SQL controls retain real authority semantics and evidence fences."""
import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.tofudb_context_controls import ARMS, SCHEMA, InProcessSQLite, measure, summarize
from benchmarks.tofudb_context_read import fixture, history_proof, validate
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("json_boundary", [False, True])
def test_inprocess_control_retains_owner_receipts_durability_and_reopen(tmp_path, json_boundary):
    engine = InProcessSQLite(json_boundary=json_boundary, directory=tmp_path)
    engine.start()
    private_root = Path(engine._temporary.name)
    try:
        payload, expected, continuation, appended = fixture(engine, 4, 1024)
        request = engine.request("turn.context.read", payload)
        request["auth"] = "wrong-token"
        with pytest.raises(StorageError, match="authentication"):
            engine.dispatch(request)
        assert engine.execute("turn.context.read", payload | {"user_id": 12}, False) is None
        request = engine.request("record.put", {
            "namespace": "context-control", "key": "receipt", "value": {"exact": "内容"}}, True)
        first = engine.dispatch(request)
        assert engine.dispatch(request) == first
        assert engine.durability()["writer_pragmas"]["synchronous"] == 2
        engine.restart_cycle()
        assert engine.dispatch(request | {"deadline_unix_ms": engine.request("unused", {})["deadline_unix_ms"]}) == first
        for replay in (False, True):
            result = engine.execute("turn.context.read", payload | {"replay": replay}, False)
            validate(result, expected, continuation, replay, history_proof(appended), appended)
    finally:
        engine.stop()
    assert not private_root.exists()
    with pytest.raises(RuntimeError, match="not open"):
        engine.execute("turn.context.read", payload, False)


def test_sidecar_measure_tracks_reopened_child_and_joins_both_generations(tmp_path):
    report = measure(SimpleNamespace(arm="sqlite-sidecar", directory=tmp_path,
        history=4, payload_bytes=128, pattern="escaped", count=1))
    assert report["initial_authority_process"]["pid"] != report["authority_process"]["pid"]
    assert report["authority_process"]["pid"] != report["pid"]
    assert report["initial_authority_process"]["created_at"] <= report["authority_process"]["created_at"]
    assert report["joined_cleanup"] is True


def reports():
    result = []
    for index, arm in enumerate((*ARMS, *reversed(ARMS))):
        proof = {"version": 2, "count": 5, "digest": str(index) * 64}
        report = {"schema": SCHEMA, "arm": arm, "pid": 100 + index,
            "started_unix_ns": index * 10, "finished_unix_ns": index * 10 + 9,
            "history": 4, "payload_bytes": 1024, "pattern": "escaped", "count": 2,
            "fixture_sha256": "fixture", "logical_turns_sha256": "exact-turns",
            "python_version": "python", "sqlite_version": "sqlite", "filesystem": "xfs",
            "cpu_affinity": [0, 1], "sources": {"sha256": "source", "files": {}},
            "owner_denial_verified": True, "reopen_exact_content_verified": True,
            "joined_cleanup": True, "scope": "storage operation",
            "authority_process": {"pid": 200 + index if arm == "sqlite-sidecar" else 100 + index,
                "placement": "child" if arm == "sqlite-sidecar" else "inprocess"},
            "durability": {"policy": "strict" if arm == "native-embedded" else "sqlite-wal-full",
                "configuration": {"read_pool_size": 12, "turn_projection_cache_mib": 128},
                "writer_pragmas": {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}},
            "build_identity": {"source_sha256": "native-source"},
            "artifacts_sha256": {"tofu-db": "binary", "libtofu_db.so": "library"},
            "workloads": {key: {"samples_ns": [100, 200], "count": 2,
                "verified_responses": 2, "returned_turns": turns, "history_proof": proof}
                for key, turns in (("rebuild", 6), ("continuation", 2))}}
        result.append(report)
    return result


def test_control_summary_refuses_unequal_work_and_missing_evidence():
    cohort = reports()
    result = summarize(cohort)
    assert result["arms"]["sqlite-object"]["workloads"]["rebuild"]["p50_ns"] == 150
    for field, replacement in (
        ("fixture_sha256", "wrong"), ("logical_turns_sha256", "truncated"),
        ("sources", {"sha256": "changed", "files": {}}),
        ("owner_denial_verified", False), ("reopen_exact_content_verified", False),
        ("joined_cleanup", False), ("pid", 100), ("started_unix_ns", 0),
        ("durability", {"policy": "normal"}),
        ("authority_process", {"pid": 101, "placement": "inprocess"}),
    ):
        changed = copy.deepcopy(cohort)
        changed[1][field] = replacement
        with pytest.raises(ValueError):
            summarize(changed)
    for field, replacement in (("verified_responses", 1), ("returned_turns", 5),
            ("samples_ns", [100]), ("samples_ns", [100, -1]),
            ("history_proof", {"version": 2, "count": 5, "digest": "bad"})):
        changed = copy.deepcopy(cohort)
        changed[1]["workloads"]["rebuild"][field] = replacement
        with pytest.raises(ValueError):
            summarize(changed)
    changed = copy.deepcopy(cohort)
    changed[2]["durability"]["writer_pragmas"]["synchronous"] = 1
    with pytest.raises(ValueError, match="durability"):
        summarize(changed)
    changed = copy.deepcopy(cohort)
    changed[2]["durability"]["configuration"]["turn_projection_cache_mib"] = 1
    with pytest.raises(ValueError, match="configurations"):
        summarize(changed)
    changed = copy.deepcopy(cohort)
    changed[-1]["artifacts_sha256"]["tofu-db"] = "different"
    with pytest.raises(ValueError, match="artifact"):
        summarize(changed)
    with pytest.raises(ValueError):
        summarize(cohort[:-1])
