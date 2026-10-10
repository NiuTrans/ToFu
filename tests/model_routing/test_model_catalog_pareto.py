#!/usr/bin/env python3
"""Pareto chart pricing and identity semantics.

``blendedModelCost`` (frontend/src/features/model-catalog/model.ts) feeds
``buildParetoPoints`` / ``buildParetoExclusions``. A catalog scan that had no
price card materialized explicit ``{input: 0, output: 0}`` rows, and the
chart plotted them as an official free tier — a fake $0 frontier point
(DeepSeek V4 Pro 0813 regression). The chart now treats an all-zero card as
unplottable: the Model lands in the missing-price exclusion note instead.

The chart also plots one point per AA-tested model: duplicate registrations
of the same benchmarked model (a bare canonical id next to a
provider-namespaced id matched to the same AA row, e.g. ``kimi-k2.5`` and
``kimi/kimi-k2.5``) collapse into the canonical registration instead of
rendering twice on the same coordinate.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import ROOT

pytestmark = pytest.mark.unit

NODE = shutil.which('node')


def _run_node(harness_path, *args):
    result = subprocess.run(
        [NODE, str(harness_path), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_pareto_all_zero_pricing_is_excluded_not_free(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'pareto.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/pareto-chart.ts'
    entry.write_text(f'''
import {{ buildParetoPoints, buildParetoExclusions }} from {json.dumps(module.as_posix())};

const mk = (modelId: string, intelligence: number, pricing: object | null) => ({{
  creatorId: 'deepseek',
  creatorLabel: 'DeepSeek',
  modelId,
  displayName: modelId,
  brand: 'deepseek',
  capabilities: ['text'],
  contextWindow: 0,
  registeredQualityRank: null,
  releaseDate: null,
  aa: {{ intelligence }},
  pricing,
  lifecycle: 'stable',
}});
const usd = (input: number, output: number) => ({{
  input, output, currency: 'USD', unit: 'per_million_tokens',
}});
const groups = [{{
  vendorId: 'deepseek',
  label: 'DeepSeek',
  icon: 'deepseek',
  models: [
    mk('zeroed-placeholder', 50, usd(0, 0)),
    mk('priced-model', 40, usd(1, 2)),
    mk('free-input-promo', 45, usd(0, 2)),
    mk('never-priced', 60, null),
  ],
}}];
const points = buildParetoPoints(groups);
const exclusions = buildParetoExclusions(groups);
console.log(JSON.stringify({{
  points: points.map((point) => ({{
    id: point.modelId, cost: point.rawCost, frontier: point.onFrontier,
  }})),
  exclusions: exclusions.map((entry) => entry.modelId),
}}));
''', encoding='utf-8')
    bundled = subprocess.run(
        [NODE, str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
         '--bundle', '--format=cjs', '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert bundled.returncode == 0, bundled.stderr
    values = json.loads(_run_node(output).strip().splitlines()[-1])

    points = {point['id']: point for point in values['points']}
    # The placeholder zero does not plot — and nothing plots at $0.
    assert 'deepseek/zeroed-placeholder' not in points
    assert all(point['cost'] > 0 for point in points.values())
    # A genuine partial promo (free input, paid output) still plots.
    assert points['deepseek/free-input-promo']['cost'] == 0.5
    assert points['deepseek/free-input-promo']['frontier'] is True
    assert points['deepseek/priced-model']['cost'] == 1.25
    assert points['deepseek/priced-model']['frontier'] is False
    # Unknown price lands in the exclusion note, best quality first.
    assert values['exclusions'] == [
        'deepseek/never-priced',
        'deepseek/zeroed-placeholder',
    ]


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_pareto_duplicate_registrations_collapse_to_one_point(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'pareto.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/pareto-chart.ts'
    entry.write_text(f'''
import {{ buildParetoPoints, buildParetoExclusions }} from {json.dumps(module.as_posix())};

const mk = (
  creatorId: string, modelId: string, intelligence: number,
  aaSlug: string, pricing: object | null,
) => ({{
  creatorId,
  creatorLabel: creatorId,
  modelId,
  displayName: modelId,
  brand: 'generic',
  capabilities: ['text'],
  contextWindow: 0,
  registeredQualityRank: null,
  releaseDate: null,
  aa: {{ intelligence, aa_slug: aaSlug }},
  pricing,
  lifecycle: 'stable',
}});
const usd = (input: number, output: number) => ({{
  input, output, currency: 'USD', unit: 'per_million_tokens',
}});
const groups = [{{
  vendorId: 'moonshot',
  label: 'Moonshot',
  icon: 'moonshot',
  models: [
    // Same AA row, bare + provider-namespaced duplicate: one point, bare wins.
    mk('moonshot', 'kimi-k2.5', 27.6, 'kimi-k2-5', usd(0.6, 2.4)),
    mk('moonshot', 'kimi/kimi-k2.5', 27.6, 'kimi-k2-5', usd(0.6, 2.4)),
    // Same AA row, priced namespaced twin + unpriced bare id: the priced
    // registration keeps the point, no exclusion noise.
    mk('moonshot', 'kimi-k2.6', 35.8, 'kimi-k2-6', null),
    mk('moonshot', 'accounts/fireworks/models/kimi-k2.6', 35.8, 'kimi-k2-6', usd(0.95, 4)),
    // Same AA row, two bare ids (effort-variant re-registration): the
    // shorter canonical id wins deterministically.
    mk('moonshot', 'o4-mini-high', 19.1, 'o4-mini', usd(1, 5)),
    mk('moonshot', 'o4-mini', 19.1, 'o4-mini', usd(1, 5)),
    // Same AA row, all registrations unpriced: one exclusion, bare wins.
    mk('moonshot', 'llama-4-maverick-17b-128e-instruct', 8.5, 'llama-4-maverick', null),
    mk('moonshot', 'meta-llama/llama-4-maverick-17b-128e-instruct', 8.5, 'llama-4-maverick', null),
  ],
}}];
const points = buildParetoPoints(groups);
const exclusions = buildParetoExclusions(groups);
console.log(JSON.stringify({{
  points: points.map((point) => point.modelId),
  exclusions: exclusions.map((entry) => entry.modelId),
}}));
''', encoding='utf-8')
    bundled = subprocess.run(
        [NODE, str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
         '--bundle', '--format=cjs', '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert bundled.returncode == 0, bundled.stderr
    values = json.loads(_run_node(output).strip().splitlines()[-1])

    assert sorted(values['points']) == [
        'moonshot/accounts/fireworks/models/kimi-k2.6',
        'moonshot/kimi-k2.5',
        'moonshot/o4-mini',
    ]
    assert values['exclusions'] == [
        'moonshot/llama-4-maverick-17b-128e-instruct',
    ]
