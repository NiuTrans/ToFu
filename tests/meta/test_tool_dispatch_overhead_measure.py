"""Self-check for benchmarks/tool_dispatch_overhead.py (component observation).

Runs the documented CLI once in a fresh process with its own disposable
storage sidecar and project tree, then checks the report contract. Clocks are
not asserted: the deterministic overlap guarantee is pinned by
``tests/tools/test_parallel_dispatch_overlap.py``.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_cli_reports_every_requested_tool_and_the_parallel_probes(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / 'benchmarks' / 'tool_dispatch_overhead.py'),
         '--iterations', '1', '--warmup', '0', '--tools', 'read_files,run_command',
         '--workdir', str(tmp_path), '--json'],
        cwd=ROOT, capture_output=True, text=True, timeout=240, check=False)
    assert completed.returncode == 0, completed.stderr[-2000:]
    report = json.loads(completed.stdout[completed.stdout.index('{'):])

    assert report['failures'] == {}
    assert set(report['serial']) == {'read_files', 'run_command'}
    for stats in report['serial'].values():
        assert {'wall', 'pre', 'handler', 'post', 'storage_ops'} <= set(stats)
        assert stats['wall']['n'] == 1
    for probe in ('parallel_read_tool', 'parallel_run_command'):
        assert report['parallel'][probe]['calls'] == 4
        assert report['parallel'][probe]['start_spread_ms'] is not None
    # The disposable project tree is removed after the run.
    assert list(tmp_path.iterdir()) == []
