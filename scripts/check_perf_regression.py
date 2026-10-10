#!/usr/bin/env python3
"""Performance regression gate: benchmark ratios against a committed baseline.

Runs the deterministic, provider-free benchmark suite
(``benchmarks/task_replay_bench.py``) and compares each workload's
``median_ms``/``p90_ms``/``peak_kib`` against
``benchmarks/perf_baseline.json``. A workload fails when time grows beyond
``TIME_RATIO`` × baseline or retained memory beyond ``MEM_RATIO`` × baseline.
Ratio gates (not wall-clock gates) are what stays reliable on shared CI
runners; the baseline itself is re-measured on representative hardware and
regenerated deliberately with ``--write-baseline``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
BENCH = os.path.join(ROOT, "benchmarks", "task_replay_bench.py")
BASELINE = os.path.join(ROOT, "benchmarks", "perf_baseline.json")
# Time gates target algorithmic regressions (quadratic folds, unbounded
# retention), which present as multi-x signals; 1.5x stays sensitive to those
# while tolerating noisy-neighbour jitter on shared hosts. Memory peaks are
# allocation-deterministic, so that gate can stay tight.
TIME_RATIO = 1.50
MEM_RATIO = 1.25


def run_benchmark() -> dict:
    out_path = os.path.join(
        os.environ.get("TMPDIR", "/tmp"),
        f"tofu_replay_bench_{os.getpid()}.json",
    )
    try:
        result = subprocess.run(
            [sys.executable, BENCH, "--iterations", "9",
             "--output", out_path],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
        if result.returncode != 0:
            print(
                f"task_replay_bench failed (rc={result.returncode}):\n"
                f"{(result.stderr or '')[-2000:]}",
                file=sys.stderr,
            )
            raise SystemExit(2)
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def _by_name(report: dict) -> dict[str, dict]:
    return {w["name"]: w for w in report["workloads"]}


def compare(current: dict, baseline: dict) -> list[str]:
    problems: list[str] = []
    base = _by_name(baseline)
    now = _by_name(current)
    for name, base_workload in sorted(base.items()):
        if name not in now:
            problems.append(f"[{name}] workload missing from current run")
            continue
        for field, ratio in (
            ("median_ms", TIME_RATIO),
            ("p90_ms", TIME_RATIO),
            ("peak_kib", MEM_RATIO),
        ):
            base_value = float(base_workload[field])
            now_value = float(now[name][field])
            if now_value > base_value * ratio:
                problems.append(
                    f"[{name}] {field} regressed {base_value} -> "
                    f"{now_value} (> {ratio}x baseline)"
                )
    extra = set(now) - set(base)
    if extra:
        problems.append(
            f"new workloads {sorted(extra)} are not in the baseline; "
            "regenerate deliberately with --write-baseline"
        )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-baseline", action="store_true")
    args = parser.parse_args()

    report = run_benchmark()
    if args.write_baseline:
        report["policy"] = (
            "Committed performance baseline for scripts/"
            "check_perf_regression.py. Regenerate deliberately on "
            "representative hardware with --write-baseline and name the "
            "change that justified the new numbers."
        )
        with open(BASELINE, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
            f.write("\n")
        print(f"wrote {os.path.relpath(BASELINE, ROOT)}")
        return 0

    with open(BASELINE, encoding="utf-8") as f:
        baseline = json.load(f)
    problems = compare(report, baseline)
    if problems:
        print("perf-regression: FAILED")
        print("\n".join(f"- {problem}" for problem in problems))
        return 1
    print("perf-regression: OK "
          f"({len(report['workloads'])} workloads within ratio gates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
