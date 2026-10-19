"""Isolate SQLite transport and JSON costs in exact model-context reads.

Entry: this CLI. Semantics, transactions and durability remain owned by the
production SQLite authority; this disposable measurement adapter exposes its
existing dispatch without creating a second storage implementation. Fixtures
and exact response validation are shared with tofudb_context_read. No timing
here isolates the Rust/Python difference or proves physical-engine superiority.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import orjson
import psutil

from benchmarks.tofudb_context_read import fixture, history_proof, validate
from lib.storage.protocol import PROTOCOL_VERSION, encode_frame
from scripts.tofudb_bench import _mount_type
from scripts.tofudb_perf_compare import BenchError, LegacyEngine, TofuDbEngine, _percentile

ARMS = ("native-embedded", "sqlite-sidecar", "sqlite-object", "sqlite-json")
SCHEMA = "tofu.context-controls.v1"


def frame_round_trip(value):
    """Use the actual SQL wire encoder/JSON decoder, omitting socket copies."""
    return orjson.loads(memoryview(encode_frame(value))[4:])


class InProcessSQLite:
    """Production SQL dispatch/backend with a private disposable authority."""

    def __init__(self, *, json_boundary: bool, directory: Path | None = None):
        self.json_boundary = json_boundary
        self.directory = directory
        self._temporary = None
        self.backend = None
        self._handler = None
        self.config = None
        self._token = uuid.uuid4().hex + uuid.uuid4().hex

    def start(self):
        from lib.storage_authority_sql.config import SidecarConfig
        from runtime_guards import install_process_resource_defaults

        self._temporary = tempfile.TemporaryDirectory(
            prefix="context-sqlite-inprocess-", dir=self.directory)
        env = os.environ.copy()
        install_process_resource_defaults(env)
        env.update(TOFU_PROJECT_PATH=self._temporary.name,
            TOFU_STORAGE_PROJECT_ROOT=self._temporary.name,
            TOFU_STORAGE_ALLOW_PROJECT_OVERRIDE="1",
            TOFU_STORAGE_TEST_BACKEND="sqlite",
            TOFU_STORAGE_TOKEN=self._token, TOFU_STORAGE_FASTPATH="off")
        for key in ("TOFU_STORAGE_CONNECTION_FILE", "TOFU_STORAGE_TEST_POSTGRES_DSN_FILE"):
            env.pop(key, None)
        try:
            with patch.dict(os.environ, env, clear=True):
                self.config = SidecarConfig.from_environment()
                self._open()
        except BaseException:
            self.stop()
            raise

    def _open(self):
        from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend

        self.backend = SQLiteBackend(self.config)
        with patch.dict(os.environ, {"TOFU_STORAGE_FASTPATH": "off"}):
            self.backend.start()
        self._handler = SimpleNamespace(server=SimpleNamespace(
            token=self._token, backend=self.backend, read_only_preview=False,
            logical_outbox=None))

    def request(self, operation, payload, command=False):
        return {"protocol": PROTOCOL_VERSION, "request_id": uuid.uuid4().hex,
            "auth": self._token, "deadline_unix_ms": int(time.time() * 1000) + 30000,
            "kind": "command" if command else "query", "operation": operation,
            "command_id": uuid.uuid4().hex if command else None,
            "priority": "user", "payload": payload}

    def dispatch(self, request):
        from lib.storage_authority_sql.server import _StorageHandler

        if self.backend is None or self._handler is None:
            raise RuntimeError("disposable SQLite authority is not open")
        if self.json_boundary:
            request = frame_round_trip(request)
        response = _StorageHandler._dispatch(
            self._handler, request, request["request_id"])
        if self.json_boundary:
            response = frame_round_trip(response)
        return response["result"]

    def execute(self, operation, payload, command):
        return self.dispatch(self.request(operation, payload, command))

    def restart_cycle(self):
        if self.backend is None:
            raise RuntimeError("disposable SQLite authority is not open")
        began = time.perf_counter_ns()
        self.backend.close()
        self.backend = None
        self._handler = None
        closed_ms = (time.perf_counter_ns() - began) / 1e6
        began = time.perf_counter_ns()
        self._open()
        return closed_ms, (time.perf_counter_ns() - began) / 1e6

    def durability(self):
        def inspect(session):
            return {name: next(iter(session.fetch_one("PRAGMA " + name).values()))
                    for name in ("journal_mode", "synchronous", "foreign_keys")}
        pragmas = self.backend.command("context_measure.durability", "", None,
            "maintenance", inspect, time.monotonic() + 30, receipt_required=False)
        if pragmas != {"journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}:
            raise ValueError("SQLite writer durability differs from WAL/FULL")
        locator = self.backend.diagnostic_locator()
        if locator["fastpath_active"] or Path(locator["authority_path"]) != self.config.sqlite_path:
            raise ValueError("SQLite authority escaped the selected volume")
        return {"policy": "sqlite-wal-full", "writer_pragmas": pragmas,
            "configuration": {name: getattr(self.config, name) for name in (
                "read_pool_size", "write_pool_size", "sqlite_writer_cache_mib",
                "turn_projection_cache_mib", "transaction_timeout_s")}}

    def stop(self):
        if self.backend is not None:
            self.backend.close()
            self.backend = None
            self._handler = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None


def source_snapshot():
    """Pin every tracked dependency in the measured storage/client packages."""
    paths = subprocess.check_output(["git", "ls-files", "-z", "--",
        "lib/storage", "lib/storage_authority_sql", "lib/runtime", "lib/resource_control",
        "tofu_db", "runtime_guards.py", "scripts/tofudb_perf_compare.py",
        "scripts/tofudb_bench.py"], cwd=ROOT).decode().split("\0")
    paths += ["benchmarks/tofudb_context_controls.py", "benchmarks/tofudb_context_read.py"]
    manifest = {path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
                for path in sorted(set(paths)) if path and path.endswith(".py")}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return {"sha256": hashlib.sha256(encoded).hexdigest(), "files": manifest}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def _samples(samples):
    ordered = sorted(samples)
    return {"samples_ns": samples, "count": len(samples),
        "mean_ns": statistics.mean(samples), "p50_ns": statistics.median(samples),
        "p95_ns": _percentile(ordered, .95), "p99_ns": _percentile(ordered, .99)}


def measure(args):
    sources = source_snapshot()
    if args.arm == "native-embedded":
        engine = TofuDbEngine(args.binary, client_mode="embedded",
            authority_dir_parent=args.directory)
    elif args.arm == "sqlite-sidecar":
        engine = LegacyEngine("sqlite", authority_dir_parent=args.directory)
    else:
        engine = InProcessSQLite(json_boundary=args.arm == "sqlite-json", directory=args.directory)
    report = {"schema": SCHEMA, "arm": args.arm, "pid": os.getpid(),
        "started_unix_ns": time.time_ns(), "sources": sources,
        "history": args.history, "payload_bytes": args.payload_bytes,
        "pattern": args.pattern, "count": args.count,
        "python_version": sys.version, "sqlite_version": __import__("sqlite3").sqlite_version,
        "authority_parent": str(args.directory or Path(tempfile.gettempdir())),
        "filesystem": _mount_type(args.directory or Path(tempfile.gettempdir())),
        "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "scope": "exact storage operation; warm reopened authority; no Rust/Python or physical-engine attribution",
        "workloads": {}}
    engine.start()
    authority_process = None
    try:
        if args.arm == "sqlite-sidecar":
            if not engine._child_pid:
                raise ValueError("Sidecar child identity is missing")
            authority_process = psutil.Process(engine._child_pid)
            report["authority_process"] = {"pid": authority_process.pid,
                "created_at": authority_process.create_time(), "placement": "child"}
        else:
            report["authority_process"] = {"pid": os.getpid(), "placement": "inprocess"}
        if args.arm == "native-embedded":
            identity = engine._embedded.build_identity()
            if identity.get("source_sha256") != engine._build_identity.get("source_sha256"):
                raise ValueError("executable/library source identities differ")
            report["build_identity"] = identity
            report["artifacts_sha256"] = {name: hashlib.sha256(
                (args.binary.parent / name).read_bytes()).hexdigest()
                for name in (args.binary.name, "libtofu_db.so")}
        payload, expected, continuation, appended = fixture(
            engine, args.history, args.payload_bytes, args.pattern)
        proof = history_proof(appended)
        report["fixture_sha256"] = _digest(expected)
        report["logical_turns_sha256"] = _digest([row | {
            "currentAttemptId": f"attempt-{i}" if row["currentAttemptId"] else None}
            for i, row in enumerate(appended)])
        report["reopen_ms"] = engine.restart_cycle()
        if args.arm == "sqlite-sidecar":
            if authority_process.is_running():
                raise ValueError("initial SQL child was not joined during reopen")
            report["initial_authority_process"] = report["authority_process"]
            if not engine._child_pid:
                raise ValueError("reopened Sidecar child identity is missing")
            authority_process = psutil.Process(engine._child_pid)
            report["authority_process"] = {"pid": authority_process.pid,
                "created_at": authority_process.create_time(), "placement": "child"}
        report["durability"] = (engine.durability() if isinstance(engine, InProcessSQLite)
            else {"policy": "strict" if args.arm == "native-embedded" else "sqlite-wal-full",
                  "evidence": "production launch configuration; sources and artifacts pinned"})
        try:
            if engine.execute("turn.context.read", payload | {"user_id": 12}, False) is not None:
                raise ValueError("foreign owner observed fixture context")
            report["owner_denial_kind"] = "not_found"
        except BenchError as error:
            if args.arm != "native-embedded" or str(error) != (
                    "turn.context.read: authority_scope_mismatch authority_scope_mismatch"):
                raise
            report["owner_denial_kind"] = "authority_scope_mismatch"
        report["owner_denial_verified"] = True
        for replay in (False, True):
            request = payload | {"replay": replay}
            for _ in range(5):
                result = engine.execute("turn.context.read", request, False)
                validate(result, expected, continuation, replay, proof, appended)
            samples = []
            for _ in range(args.count):
                began = time.perf_counter_ns()
                result = engine.execute("turn.context.read", request, False)
                samples.append(time.perf_counter_ns() - began)
                validate(result, expected, continuation, replay, proof, appended)
            report["workloads"]["continuation" if replay else "rebuild"] = _samples(samples) | {
                "verified_responses": len(samples), "returned_turns": len(result["turns"]),
                "history_proof": proof}
        report["reopen_exact_content_verified"] = True
        if sources != source_snapshot():
            raise ValueError("measured sources changed during run")
        if args.arm == "native-embedded" and report["artifacts_sha256"] != {
                name: hashlib.sha256((args.binary.parent / name).read_bytes()).hexdigest()
                for name in (args.binary.name, "libtofu_db.so")}:
            raise ValueError("native artifacts changed during run")
        report["caller_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["rss_scope"] = "caller HWM; sidecar child excluded; not a memory comparison"
    finally:
        engine.stop()
        if authority_process is not None and authority_process.is_running():
            raise ValueError("SQL child was not joined after stop")
    report["joined_cleanup"] = True
    report["finished_unix_ns"] = time.time_ns()
    return report


def summarize(reports):
    """Refuse unequal work, missing controls and unrepeated process cohorts."""
    if set(row["arm"] for row in reports) != set(ARMS):
        raise ValueError("all four control arms are required")
    comparable = ("schema", "history", "payload_bytes", "pattern", "count",
        "fixture_sha256", "logical_turns_sha256", "python_version", "sqlite_version",
        "filesystem", "cpu_affinity")
    reference = reports[0]
    sqlite_configuration = next(row["durability"].get("configuration")
        for row in reports if row["arm"] == "sqlite-object")
    for row in reports:
        if any(row[key] != reference[key] for key in comparable):
            raise ValueError("cohort fixture or environment differs")
        if row["schema"] != SCHEMA or row["sources"] != reference["sources"]:
            raise ValueError("cohort source witnesses differ")
        if not all(row.get(key) is True for key in (
                "owner_denial_verified", "reopen_exact_content_verified", "joined_cleanup")):
            raise ValueError("cohort lacks exact reopen/owner/cleanup evidence")
        if row["durability"]["policy"] != (
                "strict" if row["arm"] == "native-embedded" else "sqlite-wal-full"):
            raise ValueError("cohort durability differs")
        if row["arm"] in {"sqlite-object", "sqlite-json"} and row["durability"].get("writer_pragmas") != {
                "journal_mode": "wal", "synchronous": 2, "foreign_keys": 1}:
            raise ValueError("cohort lacks actual SQLite writer durability evidence")
        if row["arm"] in {"sqlite-object", "sqlite-json"} and (
                not sqlite_configuration or row["durability"].get("configuration") != sqlite_configuration):
            raise ValueError("in-process SQL resource configurations differ")
        process = row.get("authority_process", {})
        if ((row["arm"] == "sqlite-sidecar" and (process.get("placement") != "child"
                or not process.get("pid") or process["pid"] == row["pid"]))
                or (row["arm"] != "sqlite-sidecar" and (
                    process.get("placement") != "inprocess" or process.get("pid") != row["pid"]))):
            raise ValueError("authority process placement differs from control arm")
        proofs = [row["workloads"][key]["history_proof"] for key in ("rebuild", "continuation")]
        if (proofs[0] != proofs[1] or proofs[0].get("version") != 2
                or proofs[0].get("count") != row["history"] + 1
                or len(proofs[0].get("digest", "")) != 64):
            raise ValueError("cohort lacks matching history proof evidence")
        for key, turns in (("rebuild", row["history"] + 2), ("continuation", 2)):
            work = row["workloads"][key]
            if (work["verified_responses"] != row["count"]
                    or work["count"] != row["count"] or work["returned_turns"] != turns
                    or len(work["samples_ns"]) != row["count"]
                    or any(type(value) is not int or value <= 0 for value in work["samples_ns"])):
                raise ValueError("cohort has incomplete or invalid request evidence")
    if len({row["pid"] for row in reports}) != len(reports):
        raise ValueError("cohort requires fresh processes")
    order = sorted(reports, key=lambda row: row["started_unix_ns"])
    if any(first["finished_unix_ns"] > second["started_unix_ns"]
           for first, second in zip(order, order[1:])):
        raise ValueError("cohort measured processes overlap")
    if len(order) % 8:
        raise ValueError("cohort requires balanced forward/reverse four-arm blocks")
    for offset in range(0, len(order), 8):
        arms = [row["arm"] for row in order[offset:offset + 8]]
        if set(arms[:4]) != set(ARMS) or arms[4:] != arms[:4][::-1]:
            raise ValueError("cohort requires balanced forward/reverse four-arm blocks")
    result = {"schema": SCHEMA + ".summary", **{key: reference[key] for key in comparable if key != "schema"},
        "sources_sha256": reference["sources"]["sha256"], "order": [row["arm"] for row in order],
        "scope": reference["scope"], "arms": {}}
    for arm in ARMS:
        rows = [row for row in reports if row["arm"] == arm]
        if len(rows) < 2:
            raise ValueError("each control arm requires at least two fresh runs")
        if arm == "native-embedded" and any(
                row["build_identity"] != rows[0]["build_identity"]
                or row["artifacts_sha256"] != rows[0]["artifacts_sha256"] for row in rows):
            raise ValueError("native artifact identities differ")
        result["arms"][arm] = {"runs": len(rows), "workloads": {
            key: _samples([value for row in rows for value in row["workloads"][key]["samples_ns"]])
            for key in ("rebuild", "continuation")}}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--history", type=int, default=64)
    parser.add_argument("--payload-bytes", type=int, default=65536)
    parser.add_argument("--pattern", choices=("escaped", "plain", "unique"), default="escaped")
    parser.add_argument("--count", type=int, default=200)
    parser.add_argument("--summarize", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.summarize:
        if args.arm:
            parser.error("--summarize and --arm are mutually exclusive")
        result = summarize([json.loads(path.read_text()) for path in args.summarize])
    else:
        if not args.arm or (args.arm == "native-embedded" and args.binary is None):
            parser.error("--arm is required; native-embedded also requires --binary")
        if not 2 <= args.history <= 2000 or args.history % 2:
            parser.error("--history must be even and within 2..2000")
        if not 1 <= args.payload_bytes <= 1048576 or (args.history + 2) * args.payload_bytes > 16 * 1024**2:
            parser.error("payload must be 1..1048576 bytes and total fixture at most 16 MiB")
        if not 1 <= args.count <= 10000:
            parser.error("--count must be within 1..10000")
        result = measure(args)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"output": str(args.output), "schema": result["schema"]}))


if __name__ == "__main__":
    main()
