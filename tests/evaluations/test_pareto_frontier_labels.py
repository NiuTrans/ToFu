#!/usr/bin/env python3
"""Frontier callout chips never stack: blocked rows climb and slide sideways.

``layoutFrontierLabels`` (frontend/src/features/model-catalog/pareto-chart.ts)
centered each chip above its point and tried 8 rows; when every row was
blocked (a near-vertical frontier fills them fast) it pinned the chip by
formula with no collision check, so two chips rendered on top of each other
(qwen3.5-9b / DeepSeek R1 Distill Qwen 7B overlap). The layout now keeps
climbing and nudges sideways until the chip clears every obstacle.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import ROOT

pytestmark = pytest.mark.unit

NODE = shutil.which('node')

# Mirror of CHIP_MIN_GAP_X in pareto-chart.ts: chips must keep this horizontal gap.
CHIP_MIN_GAP_X = 10
BOUNDS = {'minX': 72, 'maxX': 1196, 'minY': 4}


def _overlap(left, right):
    return (
        left['x0'] - CHIP_MIN_GAP_X < right['x1']
        and right['x0'] - CHIP_MIN_GAP_X < left['x1']
        and left['y0'] < right['y1']
        and right['y0'] < left['y1']
    )


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_frontier_labels_never_overlap_when_centered_rows_blocked(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'labels.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/pareto-chart.ts'
    entry.write_text(f'''
import {{ layoutFrontierLabels }} from {json.dumps(module.as_posix())};

// A vertical obstacle wall through both anchors blocks every centered row,
// reproducing the near-vertical frontier that stacked the two chips.
const anchors = [
  {{ x: 500, y: 600, width: 90 }},
  {{ x: 520, y: 605, width: 90 }},
];
const wall = [{{ x1: 500, y1: 0, x2: 500, y2: 660 }}];
const markers = anchors.map((anchor) => ({{ x: anchor.x, y: anchor.y, r: 10 }}));
const bounds = {json.dumps(BOUNDS)};
const blocked = layoutFrontierLabels(anchors, {{
  segments: wall, markers, ...bounds,
}});
const spaced = layoutFrontierLabels([
  {{ x: 300, y: 600, width: 90 }},
  {{ x: 900, y: 300, width: 90 }},
], {{ segments: [], markers: [], ...bounds }});
console.log(JSON.stringify({{ blocked, spaced }}));
''', encoding='utf-8')
    bundled = subprocess.run(
        [NODE, str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
         '--bundle', '--format=cjs', '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert bundled.returncode == 0, bundled.stderr
    result = subprocess.run(
        [NODE, str(output)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout.strip().splitlines()[-1])

    blocked = values['blocked']
    assert len(blocked) == 2
    assert not _overlap(blocked[0]['chip'], blocked[1]['chip'])
    for placement in blocked:
        chip = placement['chip']
        assert chip['x0'] >= BOUNDS['minX'] - 1e-6
        assert chip['x1'] <= BOUNDS['maxX'] + 1e-6
        assert chip['y0'] >= BOUNDS['minY'] - 1e-6
        # Displaced chips draw a leader line back to the marker.
        assert placement['leader'] is True

    # Unblocked anchors keep the exact centered row-0 placement.
    assert [placement['row'] for placement in values['spaced']] == [0, 0]
    assert [placement['leader'] for placement in values['spaced']] == [False, False]
    assert [placement['cx'] for placement in values['spaced']] == [300, 900]
