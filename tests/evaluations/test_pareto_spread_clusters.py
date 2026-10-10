#!/usr/bin/env python3
"""Same-coordinate markers spread sideways only and report one dashed frame.

``spreadMarkers`` (frontend/src/features/model-catalog/pareto-chart.ts) used
to spiral stacked markers around the anchor, which moved models sharing the
SAME price and SAME AA score onto different score rows (gpt-6-astra vs its
stack rendered at different heights). Same-coordinate clusters now spread
purely horizontally — the score row never changes — and report one cluster
record so the chart draws a dashed group frame; lone near-miss markers still
spiral.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/evaluations/test_pareto_spread_clusters.py -v
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess

import pytest

from tests._runtime_sections import ROOT

pytestmark = pytest.mark.unit

NODE = shutil.which('node')
CLEARANCE = 20  # MARKER_CLEARANCE_PX in pareto-chart.ts
BOUNDS = {'minX': 72, 'maxX': 1196, 'minY': 32, 'maxY': 668}


def _collides(left, right):
    return math.hypot(left['x'] - right['x'], left['y'] - right['y']) \
        < CLEARANCE - 1e-6


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_same_coordinate_cluster_spreads_horizontally_only(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'spread.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/pareto-chart.ts'
    entry.write_text(f'''
import {{ spreadMarkers }} from {json.dumps(module.as_posix())};
const bounds = {json.dumps(BOUNDS)};
const at = (x: number, y: number, tag: string) => ({{ x, y, tag }});
const cluster = spreadMarkers([
  at(400, 300, 'a'), at(400, 300, 'b'), at(400, 300, 'c'),
], bounds);
const mixed = spreadMarkers([
  at(400, 300, 'a'), at(400, 300, 'b'), at(400, 300, 'c'),
  at(425, 300, 'lone'),
  at(700, 500, 'far'),
], bounds);
const edge = spreadMarkers([
  at(1190, 100, 'x'), at(1190, 100, 'y'), at(1190, 100, 'z'),
], bounds);
console.log(JSON.stringify({{ cluster, mixed, edge }}));
''', encoding='utf-8')
    bundled = subprocess.run(
        [NODE, str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
         '--bundle', '--format=cjs', '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert bundled.returncode == 0, bundled.stderr
    result = subprocess.run(
        [NODE, str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    values = json.loads(result.stdout.strip().splitlines()[-1])

    # Cluster: the score row never changes; the fan is centred; one frame.
    cluster = values['cluster']
    assert [item['y'] for item in cluster['items']] == [300, 300, 300]
    assert sorted(item['x'] for item in cluster['items']) == [380, 400, 420]
    assert len(cluster['clusters']) == 1
    frame = cluster['clusters'][0]
    assert frame['count'] == 3
    assert frame['left'] == 380
    assert frame['right'] == 420
    assert frame['y'] == 300

    # Mixed: the cluster row is untouched; the colliding lone marker spirals
    # away; the far marker keeps its exact position. Nothing overlaps.
    mixed_items = values['mixed']['items']
    row = sorted([item['x'], item['y']] for item in mixed_items
                 if item['tag'] in {'a', 'b', 'c'})
    assert row == [[380, 300], [400, 300], [420, 300]]
    by_tag = {item['tag']: item for item in mixed_items}
    assert by_tag['far']['x'] == 700 and by_tag['far']['y'] == 500
    assert (by_tag['lone']['x'], by_tag['lone']['y']) != (425, 300)
    for index, left in enumerate(mixed_items):
        for right in mixed_items[index + 1:]:
            assert not _collides(left, right), (left, right)
    assert len(values['mixed']['clusters']) == 1

    # Edge: the row clamps inside the right bound without changing y.
    edge_items = values['edge']['items']
    assert [item['y'] for item in edge_items] == [100, 100, 100]
    edge_xs = sorted(item['x'] for item in edge_items)
    assert edge_xs[0] >= BOUNDS['minX'] - 1e-6
    assert edge_xs[-1] <= BOUNDS['maxX'] + 1e-6
    assert edge_xs[1] - edge_xs[0] == CLEARANCE
    assert edge_xs[2] - edge_xs[1] == CLEARANCE
