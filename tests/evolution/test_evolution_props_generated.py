"""The evolution office prop atlas (props.png) and its measured PROPS bounds
must match the deterministic generator — no hand-edited raster copies, and the
frontend registry must consume exactly the generator's printed output."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parent.parent.parent
GENERATOR = 'scripts/generate_evolution_props.py'
REGISTRY = ROOT / 'frontend/src/features/evolution/office-objects.ts'


def test_prop_atlas_matches_regeneration():
    result = subprocess.run(
        [sys.executable, GENERATOR, '--check'], cwd=ROOT,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, (
        f'props.png drifted from regeneration:\n{result.stdout}{result.stderr}')


def test_prop_bounds_match_frontend_registry():
    result = subprocess.run(
        [sys.executable, GENERATOR, '--frames-only'], cwd=ROOT,
        capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    generated = re.findall(r'\[(\d+),(\d+),(\d+),(\d+)\]', result.stdout)
    source = REGISTRY.read_text()
    block = re.search(r'export const PROPS = \[(.*?)\];', source, re.S)
    assert block, 'office-objects.ts lost its PROPS constant'
    committed = re.findall(r'\[(\d+), (\d+), (\d+), (\d+)\]', block.group(1))
    assert generated and committed == generated, (
        f'PROPS in office-objects.ts drifted from the generator output: '
        f'{committed} != {generated}')
