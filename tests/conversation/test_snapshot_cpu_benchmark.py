"""Offline benchmark smoke contract: paired evidence is not production admission."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_snapshot_cpu_probe_compares_exact_sources_and_preserves_raw_clocks(tmp_path):
    output = tmp_path / "clocks.json"
    subprocess.run([
        sys.executable, "-m", "benchmarks.conversation_snapshot_cpu",
        "--reference-ref", "HEAD", "--samples", "2", "--pairs", "2",
        "--output", str(output),
    ], cwd=ROOT, check=True, capture_output=True, text=True, timeout=60)
    report = json.loads(output.read_text())
    assert len(report["pairs"]) == 4
    assert [row["arm"] for row in report["pairs"]] == [
        "reference", "candidate", "candidate", "reference",
    ]
    assert report["full_wire_bytes"] > 4_000_000
    assert report["refs_wire_bytes"] < report["full_wire_bytes"]
    for key in ("fixture_sha256", "output_sha256"):
        assert len(report[key]) == 64
    assert report["reference_hashes"].keys() == report["candidate_hashes"].keys()
    for row in report["pairs"]:
        assert set(row["stages"]) == {"validation", "refs_projection", "snapshot_encode"}
        for clocks in row["stages"].values():
            assert len(clocks["wall_ns"]) == len(clocks["thread_cpu_ns"]) == 2
            assert all(value > 0 for value in clocks["wall_ns"])
    assert any("no network" in limitation for limitation in report["limitations"])
