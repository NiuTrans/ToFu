"""tests/test_frontend_fable_brand_detection.py — Fable 5 must render with the
real Anthropic Claude brand logo, not the unbranded gray "generic" box.

WHY
---
Anthropic's Fable line (fable-5) shares the Claude Messages API family — the
backend already treats it identically (lib/model_info/_family.is_claude includes
'fable'; discovery infers vision+thinking; slot table gives it the Claude caps).
But the frontend brand-detection regex in static/js/settings/branding.js was
never updated: ``_BRAND_PATTERNS`` had

    [/claude|anthropic|opus|sonnet|haiku/i, 'claude']

which does NOT match ``fable-5`` → ``_detectBrand('fable-5')`` fell through to
``'generic'`` → every place that renders a brand icon (model picker preset,
provider header, model card, finish-info route tag, matrix row) showed the
gray placeholder box instead of the Claude logo.

This harness evals the REAL shipped ``static/js/settings/branding.js`` and
asserts:
  * ``_detectBrand('fable-5') === 'claude'`` (post-fix behaviour)
  * ``_detectBrand('us.anthropic.fable-5-v1:0') === 'bedrock'`` (Bedrock prefix
    still wins — it's ordered BEFORE the fable/claude clause)
  * ``_brandSvg('claude', 20)`` returns markup carrying the Claude amber color
    ``#D97706``, so a fable card in Providers list uses the real Claude logo
  * No regression: existing Claude / Kimi / OpenAI paths keep their brands

SOURCE-LEVEL NEUTER (mutated copy; shipped file untouched): drop 'fable' out of
the claude pattern → ``_detectBrand('fable-5')`` regresses to 'generic', proving
the fable clause is the load-bearing fix.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_section_path

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
BRANDING = runtime_section_path('settings/branding.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;

const out = [];
function check(name, cond, extra) {
  out.push((cond ? 'PASS ' : 'FAIL ') + name + (extra ? ' :: ' + extra : ''));
}

// Loading branding.js pulls in _BRAND_ICONS / _BRAND_COLORS / _BRAND_PATTERNS /
// _detectBrand / _brandSvg. It also references BASE_PATH + BASE_PATH-derived
// _ICON_BASE for the tofu-avatar <img> tags — those are irrelevant here, we
// just define BASE_PATH so eval doesn't ReferenceError.
global.BASE_PATH = '';

function loadBranding(src) {
  (0, eval)(src);
}

const SRC = fs.readFileSync(process.argv[2], 'utf8');
loadBranding(SRC);

// ── Positive: fable now maps to Claude ──
check('fable_5_is_claude', _detectBrand('fable-5') === 'claude',
      "_detectBrand('fable-5') = " + _detectBrand('fable-5'));
check('fable_5_uppercase_is_claude', _detectBrand('Fable-5') === 'claude');
// A gateway prefix that isn't Bedrock (i.e. no 'us.anthropic.' or 'bedrock')
// but names fable stays on Claude.
check('gateway_fable_is_claude', _detectBrand('proxy/fable-5-preview') === 'claude');

// ── Bedrock still wins over fable (its regex is ordered first) ──
check('bedrock_us_anthropic_fable',
      _detectBrand('us.anthropic.fable-5-v1:0') === 'bedrock');

// ── The Claude _brandSvg carries the Anthropic amber #D97706 ──
const svg = _brandSvg('claude', 20);
check('claude_brand_svg_has_amber', /#D97706/.test(svg),
      'markup=' + svg.slice(0, 120));
// And it's an actual <svg>, not the unbranded generic box.
check('claude_brand_svg_is_svg', /<svg /.test(svg));
// Sanity: generic falls back to gray (#888) — proves the color plumbing runs.
const gsvg = _brandSvg('generic', 20);
check('generic_brand_svg_has_gray', /#888/.test(gsvg));

// ── Cross-brand regressions ──
check('kimi_k3_stays_kimi', _detectBrand('kimi-k3') === 'kimi');
check('kimi_k2_6_stays_kimi', _detectBrand('kimi-k2.6') === 'kimi');
check('claude_opus_stays_claude', _detectBrand('claude-opus-4-8') === 'claude');
check('gpt_stays_openai', _detectBrand('gpt-5.6') === 'openai');
check('gemini_stays_gemini', _detectBrand('gemini-3.5-flash') === 'gemini');

// ── Codex line maps to OpenAI (bare wire ids, no gpt/chatgpt substring) ──
// ChatGPT-subscription providers stamp brand='oauth'; surfaces that only
// have the model_id (info-rail, finish-info, provider model cards) detect
// from the id alone — 'codex-*' must not fall through to the grey box.
check('codex_auto_review_is_openai', _detectBrand('codex-auto-review') === 'openai');
check('codex_mini_latest_is_openai', _detectBrand('codex-mini-latest') === 'openai');
check('gpt_codex_spark_is_openai', _detectBrand('gpt-5.3-codex-spark') === 'openai');

// ── NEUTER: drop 'fable' out of the claude pattern → regresses to generic ──
{
  const neutered = SRC.replace(
    "[/claude|anthropic|opus|sonnet|haiku|fable/i, 'claude']",
    "[/claude|anthropic|opus|sonnet|haiku/i, 'claude']");
  check('neuter_applied', neutered !== SRC);
  loadBranding(neutered);
  const nb = _detectBrand('fable-5');
  check('neuter_regresses_to_generic', nb === 'generic',
        'neutered _detectBrand(fable-5) = ' + nb);
}

// ── NEUTER 2: drop 'codex' out of the openai pattern → regresses to generic ──
{
  const neutered = SRC.replace(
    "[/gpt|openai|o[134]-|chatgpt|codex|dall/i, 'openai']",
    "[/gpt|openai|o[134]-|chatgpt|dall/i,   'openai']");
  check('neuter2_applied', neutered !== SRC);
  loadBranding(neutered);
  const nb = _detectBrand('codex-auto-review');
  check('neuter2_regresses_to_generic', nb === 'generic',
        'neutered _detectBrand(codex-auto-review) = ' + nb);
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_fable_detects_as_claude_brand():
    src = open(BRANDING, encoding='utf-8').read()
    # Tripwire — if the pattern is reworded, the NEUTER string must be updated
    # too. Fail fast rather than silently pass a stale test.
    assert "|haiku|fable/i, 'claude'" in src, \
        'branding.js Claude pattern signature drifted — update this test'
    assert "|chatgpt|codex|dall/i, 'openai'" in src, \
        'branding.js OpenAI pattern signature drifted — update this test'

    harness = os.path.join(HERE, '_fable_brand_detection_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, BRANDING],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'fable-brand-detection failures:\n' + output
    # 14 positive + 2 neuter pairs = 18 checks total.
    assert output.count('PASS') >= 18, f'expected >=18 PASS, got:\n{output}'
