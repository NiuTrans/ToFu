#!/usr/bin/env python3
"""tests/test_meituan_marketplace_models.py — Jul 2026 marketplace batch guard.

Pins the end-to-end registration of the six models added from the Meituan
model marketplace on 2026-07-24:

  =====================  ========  ==================  ======================
  model_id               provider  marketplace price   notes
  =====================  ========  ==================  ======================
  gpt-5.6-sol            OpenAI    ¥36 / ¥216 per 1M   SOTA flagship tier
  gpt-5.6-terra          OpenAI    ¥18 / ¥108 per 1M   balanced daily driver
  gpt-5.6-luna           OpenAI    ¥7.2 / ¥43.2 per 1M high-throughput light
  gemini-3.6-flash       Google    ¥10.8 / ¥54 per 1M  marketplace RPM 20
  gemini-3.5-flash-lite  Google    ¥2.16 / ¥18 per 1M  marketplace RPM 20
  claude-fable-5         Anthropic ¥72 / ¥360 per 1M   Meituan name for Fable 5
  =====================  ========  ==================  ======================

All six cards declare 复杂推理 (thinking) + 图像理解 (vision) + 文本生成
(text) + Function Call, so every entry must carry at least
``{text, vision, thinking}``. The ``cheap`` tag is NOT hand-picked — it is
owned by ``reevaluate_pricing_tags`` from MODEL_PRICING (input < $3/1M AND
output < $15/1M); the expected sets below mirror the USD-converted prices.

Four registration surfaces are audited for consistency:
  1. ``static/provider_templates/meituan.json`` (Settings UI template)
  2. ``lib.llm_dispatch.config._slots.DEFAULT_SLOT_CONFIGS``
  3. ``lib.pricing._tables.MODEL_PRICING`` (USD per 1M, CNY converted at 7.24)
  4. ``lib.llm_dispatch.config._aliases.MODEL_ALIASES`` (claude-fable-5 ↔ fable-5)

plus the wire-shape predicates (is_gpt_56 ultra tier, is_claude family,
build_body reasoning_effort / thinking.type, discovery caps + thinking
format vote).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_marketplace_models.py -v
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.mcp.registry import is_opensource_build

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_DUMMY_MSGS = [{'role': 'user', 'content': 'hi'}]

# Expected capability sets — per the marketplace cards (2026-07-24).
_EXPECTED_CAPS = {
    'gpt-5.6-sol':           {'text', 'vision', 'thinking'},
    'gpt-5.6-terra':         {'text', 'vision', 'thinking', 'cheap'},
    'gpt-5.6-luna':          {'text', 'vision', 'thinking', 'cheap'},
    'gemini-3.6-flash':      {'text', 'vision', 'thinking', 'cheap'},
    'gemini-3.5-flash-lite': {'text', 'vision', 'thinking', 'cheap'},
    'claude-fable-5':        {'text', 'vision', 'thinking'},
}

# Expected USD pricing (per 1M) — CNY list prices converted at 7.24.
_EXPECTED_PRICING = {
    'gpt-5.6-sol':           (4.97, 29.83),    # ¥36/¥216
    'gpt-5.6-terra':         (2.49, 14.92),    # ¥18/¥108
    'gpt-5.6-luna':          (0.99, 5.97),     # ¥7.2/¥43.2
    'gemini-3.6-flash':      (1.49, 7.46),     # ¥10.80/¥54.00
    'gemini-3.5-flash-lite': (0.30, 2.49),     # ¥2.16/¥18
    'claude-fable-5':        (9.94, 49.72),    # ¥72/¥360
}

# cheap = input < $3 AND output < $15 (PRICING_TIERS, Sonnet-4.6 bracket).
_EXPECTED_CHEAP = {'gpt-5.6-terra', 'gpt-5.6-luna',
                   'gemini-3.6-flash', 'gemini-3.5-flash-lite'}
_EXPECTED_NOT_CHEAP = {'gpt-5.6-sol', 'claude-fable-5'}


def _load_meituan_template() -> dict:
    path = os.path.join(_ROOT, 'static', 'provider_templates', 'meituan.json')
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def _template_violations(models: list[dict]) -> list[str]:
    """Audit a template model list against _EXPECTED_CAPS. Reusable so the
    NEUTER face can feed a synthetic broken list through the same predicate."""
    by_id = {m.get('model_id'): m for m in models}
    violations: list[str] = []
    for mid, want_caps in _EXPECTED_CAPS.items():
        entry = by_id.get(mid)
        if entry is None:
            violations.append('%s: missing from template' % mid)
            continue
        got = set(entry.get('capabilities') or [])
        if got != want_caps:
            violations.append('%s: caps %r != expected %r'
                              % (mid, sorted(got), sorted(want_caps)))
    return violations


# ═══════════════════════════════════════════════════════════
#  1. Template / slot / pricing registration
# ═══════════════════════════════════════════════════════════

@pytest.mark.skipif(is_opensource_build(),
                    reason='meituan.json is an internal provider template, '
                           'not shipped in opensource builds')
def test_template_carries_the_six_marketplace_models():
    tpl = _load_meituan_template()
    violations = _template_violations(tpl.get('models') or [])
    assert not violations, 'meituan.json template violations:\n' + '\n'.join(
        '  ' + v for v in violations)


def test_slot_table_pre_seeded_with_matching_caps():
    from lib.llm_dispatch.config._slots import DEFAULT_SLOT_CONFIGS
    for mid, want_caps in _EXPECTED_CAPS.items():
        cfg = DEFAULT_SLOT_CONFIGS.get(mid)
        assert cfg is not None, '%s missing from DEFAULT_SLOT_CONFIGS' % mid
        got = set(cfg.get('caps') or [])
        assert got == want_caps, (
            '%s slot caps %r != template caps %r' % (mid, sorted(got), sorted(want_caps)))


def test_pricing_table_usd_converted_with_family_cache_multipliers():
    from lib.pricing._tables import MODEL_PRICING
    for mid, (want_in, want_out) in _EXPECTED_PRICING.items():
        row = MODEL_PRICING.get(mid)
        assert row is not None, '%s missing from MODEL_PRICING' % mid
        assert row['input'] == pytest.approx(want_in, abs=1e-9), (mid, row)
        assert row['output'] == pytest.approx(want_out, abs=1e-9), (mid, row)
    # Cache multipliers follow the family contract: Claude 1.25/0.10,
    # Gemini 1.00/0.25, GPT-5.6 1.00/0.10.
    assert MODEL_PRICING['claude-fable-5']['cacheWriteMul'] == 1.25
    assert MODEL_PRICING['claude-fable-5']['cacheReadMul'] == 0.10
    for mid in ('gemini-3.6-flash', 'gemini-3.5-flash-lite'):
        assert MODEL_PRICING[mid]['cacheReadMul'] == 0.25, mid
    for mid in ('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'):
        assert MODEL_PRICING[mid]['cacheReadMul'] == 0.10, mid


def test_pricing_tiers_cheap_classification():
    """The 'cheap' tag is derived from MODEL_PRICING, not hand-set — pin the
    classification so a price edit that crosses the Sonnet bracket flips red."""
    from lib.llm_dispatch.config._pricing import get_pricing_tiers
    for mid in _EXPECTED_CHEAP:
        assert 'cheap' in get_pricing_tiers(mid), (
            '%s should classify cheap per its pricing row' % mid)
    for mid in _EXPECTED_NOT_CHEAP:
        assert 'cheap' not in get_pricing_tiers(mid), (
            '%s must NOT classify cheap (flagship/enterprise pricing)' % mid)


def test_alias_group_interchangeable_with_fable_5():
    """claude-fable-5 is Fable 5 reached through the Meituan gateway — it must
    sit in the fable-5 alias group so prefer_model treats them as one model."""
    from lib.llm_dispatch.config._aliases import MODEL_ALIASES
    group = MODEL_ALIASES.get('claude-fable-5')
    assert group is not None, 'claude-fable-5 missing from MODEL_ALIAS_GROUPS'
    assert 'fable-5' in group
    assert MODEL_ALIASES.get('fable-5') is group


# ═══════════════════════════════════════════════════════════
#  2. Wire shape — family detection + build_body
# ═══════════════════════════════════════════════════════════

def test_gpt56_sub_skus_inherit_the_56_wire_shape():
    """terra/luna/sol are GPT-5.6-generation models: is_gpt_56 must hold (the
    ``ultra`` reasoning tier applies) and build_body must emit the
    OpenAI-native ``reasoning_effort`` string, never a thinking block."""
    from lib.llm import build_body
    from lib.model_info._family import is_gpt5, is_gpt_56
    for mid in ('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna'):
        assert is_gpt5(mid), mid
        assert is_gpt_56(mid), '%s must detect as GPT-5.6+ (ultra tier)' % mid
        body = build_body(mid, _DUMMY_MSGS, max_tokens=4096,
                          thinking_enabled=True, thinking_depth='ultra',
                          stream=False)
        assert body.get('reasoning_effort') == 'ultra', (mid, body)
        assert 'thinking' not in body
        assert 'enable_thinking' not in body


def test_claude_fable_5_meituan_name_is_claude_family():
    """The Meituan gateway spells Fable 5 as 'claude-fable-5' — both the
    'claude' and 'fable' substrings hit is_claude, so it takes the Claude
    Messages-API thinking shape (thinking.type='adaptive')."""
    from lib.llm import build_body, is_claude
    assert is_claude('claude-fable-5')
    body = build_body('claude-fable-5', _DUMMY_MSGS, max_tokens=4096,
                      thinking_enabled=True, stream=False)
    assert body.get('thinking', {}).get('type') == 'adaptive'
    assert 'enable_thinking' not in body
    assert 'reasoning_effort' not in body


def test_discovery_registers_fable_meituan_name_correctly():
    """A freshly-probed gateway listing claude-fable-5 must auto-register as
    a vision+thinking Claude-family model (the 'fable' name hint covers both
    the caps inference and the thinking-format vote)."""
    from lib.llm_dispatch.discovery import (
        _detect_thinking_format, _infer_capabilities,
    )
    assert _infer_capabilities('claude-fable-5') == {'text', 'vision', 'thinking'}
    assert _detect_thinking_format(
        [{'model_id': 'claude-fable-5'}], 'generic') == 'thinking_type'


# ═══════════════════════════════════════════════════════════
#  3. NEUTER faces — prove the predicates discriminate
# ═══════════════════════════════════════════════════════════

def test_neuter_template_audit_flags_broken_payload():
    """Feed a synthetic broken template through the same audit predicate:
    missing model + dropped vision must BOTH be flagged. If the audit ever
    degrades to a tautology this face goes red."""
    broken = [
        # gpt-5.6-sol present but vision dropped ( violates the card )
        {'model_id': 'gpt-5.6-sol', 'capabilities': ['text', 'thinking']},
        # every other expected model simply absent
    ]
    violations = _template_violations(broken)
    assert any('gpt-5.6-sol' in v and 'caps' in v for v in violations), violations
    assert any('missing from template' in v for v in violations), violations
    # …and the audit is green on a payload that is exactly right.
    good = [{'model_id': mid, 'capabilities': sorted(caps)}
            for mid, caps in _EXPECTED_CAPS.items()]
    assert _template_violations(good) == []


def test_neuter_pricing_tier_thresholds_discriminate():
    """Prove the cheap bracket is a real threshold, not an always-true tag:
    explicit prices above the Sonnet bracket are NOT cheap, below it ARE."""
    from lib.llm_dispatch.config._pricing import get_pricing_tiers
    assert 'cheap' not in get_pricing_tiers(
        'nonexistent-model', input_price=10.0, output_price=100.0)
    assert 'cheap' in get_pricing_tiers(
        'nonexistent-model', input_price=1.0, output_price=5.0)
    # Boundary is strict: output exactly $15 does not qualify.
    assert 'cheap' not in get_pricing_tiers(
        'nonexistent-model', input_price=2.0, output_price=15.0)


def test_neuter_ultra_tier_discriminates_pre_56():
    """The ultra assertions above are only meaningful if an older GPT-5.x
    really clamps — gpt-5.4 must downgrade ultra→high (mirror of the
    pre-existing ladder guard, kept here as this file's neuter face)."""
    from lib.llm import build_body
    from lib.model_info._family import is_gpt_56
    assert not is_gpt_56('gpt-5.4')
    body = build_body('gpt-5.4', _DUMMY_MSGS, max_tokens=4096,
                      thinking_enabled=True, thinking_depth='ultra',
                      stream=False)
    assert body.get('reasoning_effort') == 'high'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
