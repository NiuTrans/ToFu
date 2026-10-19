"""Measure exact point/window reads across the embedded SDK / raw SQLite boundary.

Owns a fresh disposable authority and validates every logical value. Optional
caller wrappers observe conversion/FFI costs; --window analysis requires native
TOFU_DB_REQUEST_PROFILE_EVERY=1. Run fresh processes off/on/on/off to quantify
observer cost. This is an attribution experiment, not the full performance gate.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import resource
from pathlib import Path
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tofudb_perf_compare import RawSqliteEngine, TofuDbEngine, _MINIMUM_TAIL_SAMPLES, _percentile, _record_value, _workloads, _SCALES, _SEARCH_TS
from scripts.tofudb_profile_report import analyze_window


@contextmanager
def caller_edges(engine, enabled):
    """Delegate unchanged SDK calls; restore hooks even on a failed request."""
    timings = {}
    originals = []

    def observe(label, function):
        samples = timings.setdefault(label, [])

        def wrapped(*args, **kwargs):
            start = time.perf_counter_ns()
            result = function(*args, **kwargs)
            samples.append(time.perf_counter_ns() - start)
            return result

        return wrapped

    try:
        if enabled:
            import tofu_db.embedded as sdk
            for name in ("_dumps", "_loads"):
                originals.append((sdk, name, getattr(sdk, name)))
                setattr(sdk, name, observe(name, getattr(sdk, name)))
            native = engine._embedded._lib

            class NativeProxy:
                tofudb_embed_execute = staticmethod(observe("native_ffi", native.tofudb_embed_execute))

                def __getattr__(self, name):
                    return getattr(native, name)

            originals.append((engine._embedded, "_lib", native))
            engine._embedded._lib = NativeProxy()
        yield timings
    finally:
        for obj, name, original in reversed(originals):
            setattr(obj, name, original)


def _window_fixture(history_turns, native):
    if not 1 <= history_turns <= 100000:
        raise ValueError("history_turns must be between 1 and 100000")
    namespace = "matched_window_v1"
    _, setup, operation, _ = next(row for row in _workloads(
        dict(_SCALES["medium"], history_turns=history_turns), 11, namespace=namespace)
        if row[0] == "open_windowed_long_history")
    operation_name, payload, _ = operation(0)
    expected = [(f"{namespace}_hist_turn_{i:06d}", f"history round {i} " + "lorem ipsum " * 8,
                 "human", _SEARCH_TS + 3_600_000 + i)
                for i in range(max(0, history_turns - 20), history_turns)]

    def check(result, _index):
        try:
            actual = [(turn["turnId"], turn["projection"]["content"], turn["actor"], turn["createdAt"])
                      if native else (turn["turn_id"], turn["content"], turn["actor"], turn["created_at"])
                      for turn in result["turns"]]
            valid = actual == expected
            if native:
                window = result["turnWindow"]
                valid = valid and window["totalTurns"] == history_turns and window["hasMore"] == (history_turns > 20)
                valid = valid and window["nextBeforeOrdinal"] == (history_turns - 20 if history_turns > 20 else None)
                valid = valid and [turn["ordinal"] for turn in result["turns"]] == list(range(max(0, history_turns - 20), history_turns))
        except (KeyError, TypeError):
            valid = False
        if not valid:
            raise ValueError("window read differs from its fixture or continuation metadata")

    return setup, operation_name, [payload], expected, check


def measure(engine, *, count, native, edges=False, workload="point", history_turns=1500):
    if workload == "window":
        setup, operation, payloads, values, check = _window_fixture(history_turns, native)
        scope = "fresh authority; exact ordered ID/content/actor/time and native total/continuation metadata; native full public snapshot versus raw SQL row floor, not equivalent complete response work; no concurrent/write/crash qualification"
    elif workload == "point":
        values = [_record_value(i) for i in range(128)]
        payloads = [{"namespace": "matched_point_v1", "key": f"k/{i:06d}"} for i in range(len(values))]
        operation = "record.get"

        def setup(engine):
            for payload, value in zip(payloads, values):
                engine.execute("record.put", {**payload, "value": value}, True)

        def check(result, index):
            actual = result.get("value") if native else result
            if actual != values[index]:
                raise ValueError(f"point read differs from its fixture at index {index}")
        scope = "fresh local authority; 128 warm records; exact logical values; native metadata/admission and worker boundaries exceed raw SQLite's direct SQL boundary; no cold/concurrent/write/crash qualification"
    else:
        raise ValueError("unsupported read workload")

    engine.start()
    try:
        identity = engine._embedded.build_identity() if native else None
        setup(engine)
        for i in range(256):
            index = i % len(payloads)
            last_result = engine.execute(operation, payloads[index], False)
            check(last_result, index)
        before = engine.execute("system.metrics", {}, False) if native else None
        samples = []
        with caller_edges(engine, edges) as edge_samples:
            process_start = time.process_time_ns()
            window_start = time.perf_counter_ns()
            for i in range(count):
                index = i % len(payloads)
                payload = payloads[index]
                start = time.perf_counter_ns()
                result = engine.execute(operation, payload, False)
                samples.append(time.perf_counter_ns() - start)
                check(result, index)
            window_wall = time.perf_counter_ns() - window_start
            process_cpu = time.process_time_ns() - process_start
        after = engine.execute("system.metrics", {}, False) if native else None
        ordered = sorted(samples)
        document = {
            "contract": "tofudb.matched-point/v1" if workload == "point" else "tofudb.matched-window/v1", "operation": operation,
            "workload": workload, "warmup_reads": 256,
            "engine": "tofudb" if native else "rawsqlite", "pid": os.getpid(),
            "count": count, "exact_values_checked": count, "samples_ns": samples,
            "mean_ns": statistics.mean(samples), "p50_ns": statistics.median(samples),
            "p99_ns": _percentile(ordered, .99),
            "p99_sample_count_sufficient": count >= _MINIMUM_TAIL_SAMPLES,
            "window_wall_ns": window_wall, "window_process_cpu_ns": process_cpu,
            "cpu_scope": "whole process including native workers, Python correctness checks and clock collection; not dispatch CPU",
            "fixture_sha256": hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest(),
            "metrics_before": before, "metrics_after": after, "build_identity": identity,
            "measurement_scope": scope,
            "whole_process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
            "rss_scope": "process lifetime peak including fixture setup and native runtime; not summed with separate native RSS",
        }
        if workload == "window":
            document.update({"history_turns": history_turns, "window_turns": len(values),
                             "response_encoded_bytes": len(json.dumps(last_result, separators=(",", ":")).encode()),
                             "logical_fields_checked": ["turn_id", "content", "actor", "created_at"]})
        if edges:
            if any(len(timings) != count for timings in edge_samples.values()):
                raise ValueError("caller edges do not cover exactly one call per request")
            document["caller_edges_ns"] = edge_samples
            document["caller_edges_means_ns"] = {key: statistics.mean(timings) for key, timings in edge_samples.items()}
            document["caller_edge_scope"] = "delegating wrappers add observer cost; native_ffi includes argument marshalling and GIL release/reacquire; means belong only to this instrumented population"
        if native and after["requestProfile"].get("enabled"):
            document["matched_profile_analysis"] = analyze_window(document)
        return document
    finally:
        engine.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--engine", choices=("tofudb", "rawsqlite"), default="tofudb")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=20000)
    parser.add_argument("--edges", action="store_true")
    parser.add_argument("--workload", choices=("point", "window"), default="point")
    parser.add_argument("--history-turns", type=int, default=1500)
    args = parser.parse_args()
    if not 1 <= args.count <= 1_000_000:
        parser.error("count must be between 1 and 1000000")
    if not 1 <= args.history_turns <= 100000:
        parser.error("history-turns must be between 1 and 100000")
    native = args.engine == "tofudb"
    if native and args.binary is None:
        parser.error("tofudb requires --binary")
    if args.edges and not native:
        parser.error("caller edges require tofudb")
    if native and os.environ.get("TOFU_DB_REQUEST_PROFILE_EVERY", "0") not in ("", "0", "1"):
        parser.error("matched attribution requires profile frequency 0 or 1")
    artifacts = {}
    build = None
    entrypoints = ("benchmarks/tofudb_point_boundary_measure.py", "scripts/tofudb_perf_compare.py",
                   "scripts/tofudb_profile_report.py", "tofu_db/embedded.py", "lib/storage/v2_semantics.py")
    source_hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in entrypoints}
    if native:
        args.binary = args.binary.resolve(strict=True)
        for path in (args.binary, args.binary.parent / "libtofu_db.so"):
            artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        build = json.loads(subprocess.run([str(args.binary), "--build-info"],
            capture_output=True, text=True, check=True, timeout=10).stdout)
    engine = TofuDbEngine(args.binary, client_mode="embedded") if native else RawSqliteEngine()
    document = measure(engine, count=args.count, native=native, edges=args.edges,
                       workload=args.workload, history_turns=args.history_turns)
    if native:
        if (document["build_identity"] or {}).get("source_sha256") != build.get("source_sha256") or not build.get("source_sha256"):
            raise ValueError("CLI and loaded library source identities differ or are absent")
        for path in (args.binary, args.binary.parent / "libtofu_db.so"):
            if hashlib.sha256(path.read_bytes()).hexdigest() != artifacts[path.name]:
                raise ValueError("native artifact changed during measurement")
    document["artifacts_sha256"] = artifacts
    document["cli_build_identity"] = build
    if any(hashlib.sha256((ROOT / name).read_bytes()).hexdigest() != digest for name, digest in source_hashes.items()):
        raise ValueError("measurement entrypoint changed during the run")
    document["entrypoints_sha256"] = source_hashes
    document["source_scope"] = "listed entrypoints only; freeze transitive Python/contracts/native inputs separately for comparisons"
    args.output.write_text(json.dumps(document, separators=(",", ":")) + "\n")
    print(json.dumps({key: document[key] for key in ("engine", "count", "mean_ns", "p50_ns", "p99_ns")}))


if __name__ == "__main__":
    main()
