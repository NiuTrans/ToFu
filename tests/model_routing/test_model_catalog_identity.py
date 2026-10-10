#!/usr/bin/env python3
"""Settings catalog semantic-identity deduplication.

``buildVendorGroups`` (frontend/src/features/model-catalog/model.ts) used to
render one row per registry entry, so the same model appeared once per
supply path: ``kimi-k2.5`` next to ``kimi/kimi-k2.5``, and Meituan-supplied
re-registrations (``Qwen3_8-27B-Meituan``, ``deepseek-v31-meituan``) next to
the canonical developer rows. The catalog is a read-only model surface, so
provider namespaces and supply variants now collapse into a single row under
the model's developer, with the bare canonical id surviving and missing
facts (pricing, AA score) filled from the hidden twins.

Near-name twins that are genuinely distinct models must survive: OpenAI's
``gpt-3.5-turbo-instruct`` (own pricing, 4096 context) is not
``gpt-3.5-turbo`` and is pinned as never merging.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from tests._runtime_sections import ROOT

pytestmark = pytest.mark.unit

NODE = shutil.which('node')


@pytest.mark.skipif(not NODE, reason='node not installed')
def test_vendor_groups_exclude_pending_model_drafts(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'catalog.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/model.ts'
    entry.write_text(f'''
import {{ buildVendorGroups }} from {json.dumps(module.as_posix())};
const mk = (id: string, state?: string) => ({{
  creator_id: 'xai', model_id: id, display_name: id,
  capabilities: ['text'], context_window: 500000,
  quality_rank: 1, registration_state: state,
}});
const groups = buildVendorGroups({{
  contract_version: 'tofu.model-routing/v2',
  creators: [{{ creator_id: 'xai', name: 'xAI' }}],
  models: [mk('grok-4.7', 'ready'),
    mk('grok-4.20-multi-agent', 'pending_metadata'),
    mk('grok-3', 'disabled'), mk('grok-4.6')],
}}, {{ 'xai::grok-4.20-multi-agent': {{ intelligence: 100 }} }});
console.log(JSON.stringify(groups.flatMap(group => group.models.map(model => model.modelId))));
''', encoding='utf-8')
    bundled = subprocess.run(
        [NODE, str(ROOT / 'scripts/vite_test_bundle.mjs'), str(entry),
         '--bundle', '--format=cjs', '--platform=node', f'--outfile={output}'],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert bundled.returncode == 0, bundled.stderr
    result = subprocess.run([NODE, str(output)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert sorted(json.loads(result.stdout.strip().splitlines()[-1])) == [
        'grok-4.6', 'grok-4.7',
    ]


def test_vendor_groups_collapse_semantic_duplicates(tmp_path):
    entry = tmp_path / 'entry.ts'
    output = tmp_path / 'catalog.cjs'
    module = ROOT / 'frontend/src/features/model-catalog/model.ts'
    entry.write_text(f'''
import {{ buildVendorGroups }} from {json.dumps(module.as_posix())};

const usd = (input: number, output: number) => ({{
  input, output, currency: 'USD', unit: 'per_million_tokens',
}});
const mk = (
  creatorId: string, modelId: string,
  extra: object = {{}},
) => ({{
  creator_id: creatorId,
  model_id: modelId,
  display_name: modelId,
  capabilities: ['text'],
  context_window: 128000,
  quality_rank: 50,
  lifecycle: 'stable',
  ...extra,
}});
const document = {{
  contract_version: 'tofu.model-routing/v2',
  creators: [
    {{ creator_id: 'moonshot', name: 'Moonshot AI' }},
    {{ creator_id: 'alibaba', name: 'Alibaba' }},
    {{ creator_id: 'meta', name: 'Meta' }},
    {{ creator_id: 'openai', name: 'OpenAI' }},
  ],
  models: [
    // Bare + provider-namespaced duplicate: bare survives.
    mk('moonshot', 'kimi-k2.5', {{ list_pricing: usd(0.6, 2.4) }}),
    mk('moonshot', 'kimi/kimi-k2.5', {{ list_pricing: usd(0.6, 2.4) }}),
    // Bare unpriced twin survives, pricing filled from the namespaced twin.
    mk('moonshot', 'kimi-k2.6'),
    mk('moonshot', 'accounts/fireworks/models/kimi-k2.6', {{
      list_pricing: usd(0.95, 4),
    }}),
    // Meituan supply suffix collapses into the canonical developer row.
    mk('alibaba', 'qwen3.8-27b', {{ list_pricing: usd(0.5, 2) }}),
    mk('alibaba', 'Qwen3_8-27B-Meituan'),
    // instruct tag on one twin of an otherwise identical registration is
    // the same snapshot (DashScope bare alias of the instruct id).
    mk('alibaba', 'qwen3-235b-a22b-2507', {{ list_pricing: usd(0.22, 0.88) }}),
    mk('alibaba', 'qwen3-235b-a22b-instruct-2507', {{
      list_pricing: usd(0.22, 0.88),
    }}),
    // Three-way: FP8-Meituan + meta-llama namespace + bare canonical.
    mk('meta', 'Llama-4-Maverick-17B-128E-Instruct-FP8-Meituan', {{
      capabilities: ['text', 'vision'],
    }}),
    mk('meta', 'meta-llama/llama-4-maverick-17b-128e-instruct'),
    mk('meta', 'llama-4-maverick-17b-128e-instruct', {{
      list_pricing: usd(0.2, 0.6),
      capabilities: ['text'],
    }}),
    // Meituan Scout dropped the instruct tag: still the same model.
    mk('meta', 'Llama-4-Scout-17B-16E-Meituan', {{ context_window: 64000 }}),
    mk('meta', 'llama-4-scout-17b-16e-instruct', {{
      list_pricing: usd(0.2, 0.78),
    }}),
    // Genuinely distinct near-name twins must both survive.
    mk('openai', 'gpt-3.5-turbo', {{
      list_pricing: usd(0.5, 1.5), context_window: 16385,
    }}),
    mk('openai', 'gpt-3.5-turbo-instruct', {{
      list_pricing: usd(1.5, 2), context_window: 4096,
    }}),
  ],
}};
const aaScores = {{
  // AA matched only the namespaced registration: the surviving bare row
  // still carries the benchmark evidence.
  'moonshot::kimi/kimi-k2.5': {{
    intelligence: 27.6, coding: null, agentic: null, math: null,
    aa_name: 'Kimi K2.5', aa_slug: 'kimi-k2-5',
  }},
}};
const groups = buildVendorGroups(document, aaScores);
const rows = groups.flatMap((group) => group.models.map((row) => ({{
  vendor: group.vendorId,
  id: row.modelId,
  pricing: row.pricing,
  aa: row.aa?.intelligence ?? null,
  capabilities: row.capabilities,
  contextWindow: row.contextWindow,
}})));
console.log(JSON.stringify(rows));
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
    rows = json.loads(result.stdout.strip().splitlines()[-1])
    by_id = {row['id']: row for row in rows}

    # One row per semantic model: 12 registrations collapse to 8 models.
    assert sorted(by_id) == [
        'gpt-3.5-turbo',
        'gpt-3.5-turbo-instruct',
        'kimi-k2.5',
        'kimi-k2.6',
        'llama-4-maverick-17b-128e-instruct',
        'llama-4-scout-17b-16e-instruct',
        'qwen3-235b-a22b-2507',
        'qwen3.8-27b',
    ]

    # Bare canonical ids survive; namespaced / Meituan twins are gone.
    for gone in (
        'kimi/kimi-k2.5',
        'accounts/fireworks/models/kimi-k2.6',
        'Qwen3_8-27B-Meituan',
        'qwen3-235b-a22b-instruct-2507',
        'Llama-4-Maverick-17B-128E-Instruct-FP8-Meituan',
        'meta-llama/llama-4-maverick-17b-128e-instruct',
        'Llama-4-Scout-17B-16E-Meituan',
    ):
        assert gone not in by_id

    # Missing facts are filled from the hidden twins.
    assert by_id['kimi-k2.6']['pricing']['input'] == 0.95
    assert by_id['kimi-k2.5']['aa'] == 27.6
    assert by_id['llama-4-maverick-17b-128e-instruct']['capabilities'] == [
        'text',
        'vision',
    ]
    # Survivor keeps its own facts when present (no twin overwrite).
    assert by_id['llama-4-scout-17b-16e-instruct']['contextWindow'] == 128000
    assert by_id['qwen3.8-27b']['pricing']['input'] == 0.5

    # The distinct legacy twin keeps its own row and its own pricing.
    assert by_id['gpt-3.5-turbo-instruct']['pricing']['input'] == 1.5
    assert by_id['gpt-3.5-turbo-instruct']['contextWindow'] == 4096
