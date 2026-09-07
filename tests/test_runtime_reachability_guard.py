"""Retained-runtime reachability guard follows executable call chains."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts/check_runtime_reachability.mjs"


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_reachability_guard_distinguishes_roots_from_closed_dead_chains():
    program = r"""
const { analyzeRuntimeReachability } = await import(process.argv[1]);
const live = analyzeRuntimeReachability(`
  function root() { leaf(); }
  function leaf() {}
  const arrowRoot = () => leaf();
  runtimeScope.root = root;
  runtimeScope.arrowRoot = arrowRoot;
`);
const dead = analyzeRuntimeReachability(`
  function orphan() { helper(); }
  function helper() {}
  const arrowOrphan = () => orphan();
`);
if (live.unreachable.length !== 0) throw new Error(JSON.stringify(live));
const names = dead.unreachable.map((item) => item.name).sort().join(',');
if (names !== 'arrowOrphan,helper,orphan') throw new Error(names);
console.log('ok');
"""
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", program, CHECKER.as_uri()],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "ok"
