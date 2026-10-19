"""Measure complete embedded record reads on disposable native authorities.

Entry: CLI. Owners: scripts.tofudb_perf_compare for lifecycle and production
adapters. Fresh-process alternating builds are required for a comparison.
The exact write receipt plus independently constructed value is the read oracle;
no successful first read can become its own oracle. Caller clocks exclude
validation, but process CPU includes it. This is no raw SQLite or Agent SLO claim.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.tofudb_perf_compare import TofuDbEngine, _percentile


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_read(actual, expected):
    if actual != expected:
        raise ValueError("complete record response differs from committed fixture")


def memory():
    fields = {}
    for line in Path("/proc/self/status").read_text().splitlines():
        name, _, rest = line.partition(":")
        if name in ("VmRSS", "VmHWM", "Threads"):
            fields[name] = int(rest.split()[0])
    return fields


def record_value(index, payload_bytes):
    pattern = 'evidence \\"\n汉字 '
    body = (pattern * (payload_bytes // 10 + 1)).encode()[:payload_bytes]
    return {"index": index, "content": body.decode("utf-8", errors="ignore"),
            "float": 0.125, "nested": [True, None, {"sequence": index}]}


def seed(engine, name, count, payload_bytes):
    requests, expected = [], []
    for index in range(count):
        request = {"namespace": name, "key": f"key-{index:06d}"}
        value = record_value(index, payload_bytes)
        receipt = engine.execute("record.put", request | {"value": value}, True)
        if "_storageCommitContract" in receipt:
            if receipt["_storageCommitContract"] != "storage.committed-events/v1":
                raise ValueError("unrecognized committed response")
            receipt = receipt["value"]
        if receipt["key"] != request["key"] or receipt["version"] != 1:
            raise ValueError("fixture did not create a fresh record")
        expected.append({"version": 1, "updated_at_ms": receipt["updated_at_ms"], "value": value})
        requests.append(request)
    return requests, expected


def measure(engine, name, count, payload_bytes, iterations, warm, readers):
    requests, expected = seed(engine, name, count, payload_bytes)
    if warm:
        for _ in range(3):
            for request, answer in zip(requests, expected):
                validate_read(engine.execute("record.get", request, False), answer)
    cache_before = engine.execute("system.metrics", {}, False).get("publishedReadView", {}).get("queryResults")
    if not isinstance(cache_before, dict):
        raise ValueError("query-result cache observation is unavailable")
    before_memory = memory()
    before_cpu = time.process_time_ns()

    def run(worker):
        samples = []
        for index in range(iterations):
            selected = (index + worker) % count
            began = time.perf_counter_ns()
            actual = engine.execute("record.get", requests[selected], False)
            samples.append(time.perf_counter_ns() - began)
            validate_read(actual, expected[selected])
        return samples

    began = time.perf_counter_ns()
    if readers == 1:
        batches = [run(0)]
    else:
        with ThreadPoolExecutor(max_workers=readers) as pool:
            batches = list(pool.map(run, range(readers)))
    elapsed = time.perf_counter_ns() - began
    cpu = time.process_time_ns() - before_cpu
    cache_after = engine.execute("system.metrics", {}, False).get("publishedReadView", {}).get("queryResults")
    if not isinstance(cache_after, dict):
        raise ValueError("query-result cache observation disappeared")
    samples = [sample for batch in batches for sample in batch]
    ordered = sorted(samples)
    encoded_sizes = [len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode())
                     for row in expected]
    return {"reads": len(samples), "keys": count, "payload_bytes": payload_bytes,
        "response_json_bytes": {"min": min(encoded_sizes), "max": max(encoded_sizes)},
        "warm_passes": 3 if warm else 0, "readers": readers,
        "samples_ns": samples, "p50_ns": _percentile(ordered, .5),
        "p95_ns": _percentile(ordered, .95), "p99_ns": _percentile(ordered, .99),
        "mean_ns": statistics.mean(samples), "phase_wall_ns": elapsed,
        "phase_process_cpu_ns": cpu, "memory_before": before_memory, "memory_after": memory(),
        "fixture_sha256": hashlib.sha256(json.dumps([x["value"] for x in expected],
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest(),
        "exact_readbacks": len(samples), "cache_before": cache_before, "cache_after": cache_after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    if os.environ.get("TOFU_DB_REQUEST_PROFILE_EVERY", "0") not in ("", "0"):
        raise ValueError("timing requires request profiling disabled")
    engine = TofuDbEngine(args.binary, client_mode="embedded")
    report = {"format": "tofudb-cached-read/v1", "pid": os.getpid(),
        "python_version": sys.version, "python_executable": sys.executable,
        "cache_overrides": {name: os.environ.get(name) for name in
            ("TOFU_DB_BLOCK_CACHE_MIB", "TOFU_DB_PAGE_CACHE_MIB")},
        "affinity": sorted(os.sched_getaffinity(0)), "benchmark_sha256": digest(__file__),
        "binary_sha256": digest(args.binary), "library_sha256": digest(args.binary.parent / "libtofu_db.so"),
        "durability": "strict", "request_retries": 0, "workloads": {}}
    try:
        engine.start()
        report["build_identity"] = engine._embedded.build_identity()
        report["binary_build_identity"] = engine._build_identity
        report["checkout_source_sha256"] = engine._checkout_source_sha256
        if report["build_identity"]["source_sha256"] != engine._build_identity["source_sha256"]:
            raise ValueError("CLI and loaded library are not the same build")
        shapes = [
            ("first_point", 512, 128, 512, False, 1),
            ("warm_point", 64, 128, 20_000, True, 1),
            ("warm_64k", 8, 65_536, 2_000, True, 1),
            ("warm_512k", 4, 524_288, 500, True, 1),
            ("concurrent_point", 64, 128, 2_000, True, 8),
        ]
        for shape in shapes:
            if args.quick:
                shape = (shape[0], min(4, shape[1]), shape[2], 8, shape[4], min(2, shape[5]))
            report["workloads"][shape[0]] = measure(engine, *shape)
        report["request_retries"] = engine.client_retries
        if engine.client_retries:
            raise ValueError("unexpected benchmark retry")
    finally:
        engine.stop()
    report["memory_after_close"] = memory()
    report["ru_maxrss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({name: {key: value for key, value in row.items() if key != "samples_ns"}
        for name, row in report["workloads"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()
