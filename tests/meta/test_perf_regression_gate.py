"""The performance ratio gate must fire on regressions and stay wired to the
real benchmark surface — a gate that compares nothing is worse than none.
"""

from __future__ import annotations

import json
import os

import pytest

from scripts import check_perf_regression as gate

pytestmark = pytest.mark.unit

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _report(workloads: dict[str, dict[str, float]]) -> dict:
    return {
        "workloads": [
            {"name": name, **metrics} for name, metrics in workloads.items()
        ]
    }


_BASE_WORKLOAD = {"median_ms": 100.0, "p90_ms": 120.0, "peak_kib": 400.0}


def test_compare_passes_within_ratio():
    baseline = _report({"w": _BASE_WORKLOAD})
    current = _report({"w": {"median_ms": 149.0, "p90_ms": 179.0,
                             "peak_kib": 499.0}})
    assert gate.compare(current, baseline) == []


def test_compare_fails_on_time_regression():
    baseline = _report({"w": _BASE_WORKLOAD})
    current = _report({"w": {"median_ms": 200.0, "p90_ms": 120.0,
                             "peak_kib": 400.0}})
    problems = gate.compare(current, baseline)
    assert len(problems) == 1
    assert "median_ms regressed 100.0 -> 200.0" in problems[0]


def test_compare_fails_on_memory_regression():
    baseline = _report({"w": _BASE_WORKLOAD})
    current = _report({"w": {"median_ms": 100.0, "p90_ms": 120.0,
                             "peak_kib": 600.0}})
    problems = gate.compare(current, baseline)
    assert len(problems) == 1
    assert "peak_kib regressed" in problems[0]


def test_compare_fails_on_missing_and_new_workloads():
    baseline = _report({"w": _BASE_WORKLOAD, "z": _BASE_WORKLOAD})
    current = _report({"w": _BASE_WORKLOAD, "novel": _BASE_WORKLOAD})
    problems = gate.compare(current, baseline)
    assert any("missing from current run" in problem for problem in problems)
    assert any("not in the baseline" in problem for problem in problems)


def test_committed_baseline_covers_the_real_benchmark_surface():
    path = os.path.join(ROOT, "benchmarks", "perf_baseline.json")
    with open(path, encoding="utf-8") as f:
        baseline = json.load(f)
    workloads = baseline.get("workloads") or []
    assert len(workloads) >= 4, (
        f"perf baseline covers only {len(workloads)} workloads — it has "
        "drifted off the benchmark surface"
    )
    for workload in workloads:
        for field in ("median_ms", "p90_ms", "peak_kib"):
            assert workload[field] > 0, (workload["name"], field)
