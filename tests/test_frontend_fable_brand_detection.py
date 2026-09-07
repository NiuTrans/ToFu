"""Public behavior contract for the typed model-brand presentation owners."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

from tests._runtime_sections import native_module_graph

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
BRAND_PRESENTATION = native_module_graph([
    ('.native/model-brand-detection.js',
     ROOT / 'frontend/src/core/model-brand-detection.ts'),
    ('.native/model-brand-icons.js',
     ROOT / 'frontend/src/core/model-brand-icons.ts'),
])


_HARNESS = r"""
const fs = require('fs');
(0, eval)(fs.readFileSync(process.argv[1], 'utf8'));

const out = [];
function check(name, condition, detail) {
  out.push((condition ? 'PASS ' : 'FAIL ') + name
    + (detail ? ' :: ' + detail : ''));
}

check('fable_5_is_claude', detectModelBrand('fable-5') === 'claude');
check('fable_uppercase_is_claude', detectModelBrand('Fable-5') === 'claude');
check('gateway_fable_is_claude',
  detectModelBrand('proxy/fable-5-preview') === 'claude');
check('bedrock_precedes_fable',
  detectModelBrand('us.anthropic.fable-5-v1:0') === 'bedrock');

const claudeBadge = brandIconHtml('claude', 20);
check('claude_badge_has_amber', /#D97706/.test(claudeBadge));
check('claude_badge_contains_svg', /<svg /.test(claudeBadge));
check('generic_badge_has_gray', /#888/.test(brandIconHtml('generic', 20)));
check('unknown_and_prototype_keys_use_generic_asset',
  ['unknown', '__proto__', 'constructor'].every((brand) =>
    brandIconHtml(brand, 20).includes(MODEL_BRAND_ICONS.generic)));

const cases = [
  ['kimi-k3', 'kimi'],
  ['kimi-k2.6', 'kimi'],
  ['claude-opus-4-8', 'claude'],
  ['gpt-5.6', 'openai'],
  ['gemini-3.5-flash', 'gemini'],
  ['codex-auto-review', 'openai'],
  ['codex-mini-latest', 'openai'],
  ['gpt-5.3-codex-spark', 'openai'],
  // Creator families that previously fell through to the generic badge.
  ['command-a-03-2025', 'cohere'],
  ['cohere-command-a', 'cohere'],
  ['c4ai-aya-expanse-8b', 'cohere'],
  ['north-mini-code-1-0', 'cohere'],
  ['llama-3.3-70b-instruct', 'meta'],
  ['cerebras-llama-4-maverick-17b-128e-instruct', 'meta'],
  ['meta/esmfold', 'meta'],
  ['nvidia/nemotron-3-super-120b-a12b', 'nvidia'],
  ['nemotron-super-3-120b', 'nvidia'],
  ['phi-4-mini', 'microsoft'],
  ['step-3.5-flash', 'stepfun'],
  ['stepfun-ai/step-3.7-flash', 'stepfun'],
  ['sonar-pro', 'perplexity'],
  ['codestral-latest', 'mistral'],
  ['devstral-small-2507', 'mistral'],
  ['magistral-small', 'mistral'],
  ['qvq-max', 'qwen'],
  ['text-embedding-v4', 'qwen'],
  ['text-embedding-3-large', 'openai'],
  ['o3', 'openai'],
  ['lyria-3-pro-preview', 'gemini'],
  ['seed-oss-36b-instruct', 'doubao'],
  ['nova-2-lite-v1', 'bedrock'],
  // meta precedes nvidia: llama-nemotron composites group with Meta,
  // matching features/model-catalog/vendor.ts creator-family grouping.
  ['llama-3.1-nemotron-70b-instruct', 'meta'],
];
for (const [modelId, expected] of cases) {
  check('detect_' + modelId, detectModelBrand(modelId) === expected,
    detectModelBrand(modelId));
}

check('cohere_badge_has_green', /#39594D/.test(brandIconHtml('cohere', 20)));
check('new_vendor_badges_use_own_asset',
  ['cohere', 'meta', 'nvidia', 'microsoft', 'stepfun', 'perplexity']
    .every((brand) =>
      !brandIconHtml(brand, 20).includes(MODEL_BRAND_ICONS.generic)));

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_brand_detection_and_icon_rendering_public_contract():
    proc = subprocess.run(
        ['node', '-e', _HARNESS, BRAND_PRESENTATION],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    failures = [line for line in output.splitlines() if line.startswith('FAIL')]
    assert not failures, 'brand presentation failures:\n' + output
    assert output.count('PASS') == 42, output
