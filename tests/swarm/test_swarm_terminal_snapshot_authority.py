"""Exercise the real swarm presenter, retained panel, and push overlay together."""
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_swarm_panel_launch_and_snapshot_authority():
    # The Node suite loads the authored TypeScript owners and retained reducers
    # at their public entry points. It covers live/reload DOM output, immutable
    # Turn authority, launch identity, and bounded occurrence-safe tool evidence.
    run = subprocess.run(
        ['node', '--test', 'tests/swarm_panel_authority.test.cjs'],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
