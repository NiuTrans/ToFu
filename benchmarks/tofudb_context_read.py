"""Measure exact model-context reads through disposable production authorities.

Entry: this CLI. Dependencies: the existing comparison lifecycle and context
witness contract. Every timed response is checked outside its request clock.
Run fresh alternating processes for comparisons; this is a storage operation
measurement, never an agent-completion or power-loss certification.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import resource
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.storage.model_continuation import settled_witness
from scripts.tofudb_bench import _mount_type
from scripts.tofudb_perf_compare import LegacyEngine, TofuDbEngine, _percentile


def history_proof(rows):
    digest = hashlib.sha256()
    for row in rows[:-1]:
        digest.update((json.dumps([row["turnId"], row["parentTurnId"], row["status"],
            row["currentAttemptId"], row["modelContextRevision"]],
            ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    return {"version": 2, "count": len(rows) - 1, "digest": digest.hexdigest()}


def fixture(engine, history: int, payload_bytes: int, pattern="escaped"):
    base = {"conversation_id": "context-measure", "user_id": 11}
    timestamp = 1_800_000_000_000
    engine.execute("conversation.create", {
        "conv_id": base["conversation_id"], "user_id": 11,
        "created_at": timestamp, "updated_at": timestamp,
    }, True)
    # Escapes and Unicode exercise the real JSON boundary. Unique prefixes
    # prevent equal rows from substituting for the selected history.
    text = ('evidence \\"\n汉字 ' * (payload_bytes // 10 + 1)).encode()[:payload_bytes]
    text = text.decode("utf-8", errors="ignore")
    if pattern == "plain":
        text = "x" * payload_bytes
    expected = []
    appended = []

    def append(index):
        turn_id = f"context-turn-{index:06d}"
        evidence = (base64.b64encode(random.Random(20261019 + index).randbytes(
            payload_bytes)).decode()[:payload_bytes] if pattern == "unique" else text)
        projection = {"content": f"round-{index}", "toolRounds": [
            {"toolName": "read", "toolContent": f"{index}:" + evidence}]}
        result = engine.execute("turn.append_settled", base | {
            "turn_id": turn_id, "command_id": f"append-{index}",
            "actor": "assistant" if index % 2 else "human",
            "projection": projection, "created_at": timestamp + index,
            "now": timestamp + index,
        }, True)
        if "_storageCommitContract" in result:
            if result["_storageCommitContract"] != "storage.committed-events/v1":
                raise ValueError("unknown committed response contract")
            result = result["value"]
        expected.append((turn_id, projection))
        appended.append(result["turn"])

    for index in range(history):
        append(index)
    prefix = engine.execute("turn.context.read", base | {
        "turn_id": expected[-1][0], "replay": False,
    }, False)
    if (prefix["nextTurnId"] is not None or prefix["turns"] != appended
            or prefix["historyProof"] != history_proof(appended)):
        raise ValueError("fixture prefix exceeds one response page")
    continuation = {"turns": [settled_witness(prefix["turns"][-1])],
        "historyProof": prefix["historyProof"], "policy": "context-measure-v1",
        "messages": [{"role": "assistant", "content": "retained prefix"}]}
    if not engine.execute("model_continuation.put", base | {
        "lane_id": "main", "projection": continuation,
    }, True):
        raise ValueError("continuation was not stored")
    append(history)
    append(history + 1)
    return base | {"turn_id": expected[-1][0]}, expected, continuation, appended


def validate(result, expected, continuation, replay, proof=None, appended=None):
    covered = len(expected) - 2 if replay else 0
    if (result["supported"] is not True or result["covered"] != covered
            or result["nextTurnId"] is not None
            or result["proofEligible"] is not True
            or result["continuationStatus"] != ("available" if replay else "replay_disabled")
            or result["continuation"] != (continuation if replay else None)
            or [(row["turnId"], row["projection"]) for row in result["turns"]] != expected[covered:]
            or result["historyProof"]["count"] != len(expected) - 1
            or (appended is not None and result["turns"] != appended[covered:])
            or (proof is not None and result["historyProof"] != proof)):
        raise ValueError("context response differs from exact fixture/proof")


def measure(args):
    engine = (TofuDbEngine(args.binary, client_mode="embedded",
                          authority_dir_parent=args.directory)
              if args.engine == "tofudb" else
              LegacyEngine("sqlite", authority_dir_parent=args.directory))
    engine.start()
    try:
        identity = engine._embedded.build_identity() if args.engine == "tofudb" else None
        if identity is not None and identity.get("source_sha256") != engine._build_identity.get("source_sha256"):
            raise ValueError("executable/library source identities differ")
        payload, expected, continuation, appended = fixture(
            engine, args.history, args.payload_bytes, args.pattern)
        # Attempt UUIDs are generated by both production ingesters. Verify their
        # exact per-run values; only the cross-run digest substitutes a rank.
        normalized = [row | {"currentAttemptId": f"attempt-{i}" if row["currentAttemptId"] else None}
                      for i, row in enumerate(appended)]
        report = {"schema": "tofu.context-read-measure.v1", "engine": args.engine,
            "build_identity": identity, "history": args.history,
            "payload_bytes": args.payload_bytes, "pattern": args.pattern, "count": args.count,
            "scope": "strict native embedded / SQLite WAL FULL sidecar; exact context operation; no task-speed or power-loss claim",
            "fixture_sha256": hashlib.sha256(json.dumps(expected, ensure_ascii=False,
                separators=(",", ":")).encode()).hexdigest(),
            "logical_turns_sha256": hashlib.sha256(json.dumps(normalized, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "python_version": sys.version, "sqlite_version": sqlite3.sqlite_version,
            "authority_parent": str(args.directory or Path(tempfile.gettempdir())),
            "filesystem": _mount_type(args.directory or Path(tempfile.gettempdir())),
            "cpu_affinity": sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
            "entrypoint_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "workloads": {}}
        if args.engine == "tofudb":
            report["artifacts_sha256"] = {name: hashlib.sha256(
                (args.binary.parent / name).read_bytes()).hexdigest()
                for name in (args.binary.name, "libtofu_db.so")}
        proof = history_proof(appended)
        for replay in (False, True):
            request = payload | {"replay": replay}
            for _ in range(5):
                value = engine.execute("turn.context.read", request, False)
                validate(value, expected, continuation, replay, proof, appended)
            samples = []
            for _ in range(args.count):
                start = time.perf_counter_ns()
                value = engine.execute("turn.context.read", request, False)
                samples.append(time.perf_counter_ns() - start)
                validate(value, expected, continuation, replay, proof, appended)
            ordered = sorted(samples)
            report["workloads"]["continuation" if replay else "rebuild"] = {
                "mean_ns": statistics.mean(samples), "p50_ns": statistics.median(samples),
                "p95_ns": _percentile(ordered, .95), "p99_ns": _percentile(ordered, .99),
                "samples_ns": samples, "verified_responses": len(samples),
                "returned_turns": len(value["turns"]), "history_proof": proof,
            }
        report["caller_peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["rss_scope"] = "caller high-water mark; includes native embedded, excludes SQLite child; not cross-backend comparable"
        if args.engine == "tofudb" and report["artifacts_sha256"] != {
                name: hashlib.sha256((args.binary.parent / name).read_bytes()).hexdigest()
                for name in (args.binary.name, "libtofu_db.so")}:
            raise ValueError("native artifacts changed during measurement")
        return report
    finally:
        engine.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--engine", choices=("tofudb", "sqlite"), required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--history", type=int, default=64)
    parser.add_argument("--payload-bytes", type=int, default=65536)
    parser.add_argument("--pattern", choices=("escaped", "plain", "unique"), default="escaped")
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.engine == "tofudb" and args.binary is None:
        parser.error("--binary is required for tofudb")
    if not 2 <= args.history <= 2000 or args.history % 2:
        parser.error("--history must be even and within 2..2000")
    if not 1 <= args.payload_bytes <= 1048576 or (args.history + 2) * args.payload_bytes > 16 * 1024**2:
        parser.error("payload must be 1..1048576 bytes and total fixture at most 16 MiB")
    if not 1 <= args.count <= 10000:
        parser.error("--count must be within 1..10000")
    report = measure(args)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"engine": args.engine, "history": args.history,
        **{key: {name: value for name, value in row.items() if name != "samples_ns"}
           for key, row in report["workloads"].items()}}))


if __name__ == "__main__":
    main()
