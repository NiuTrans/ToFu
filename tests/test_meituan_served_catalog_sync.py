#!/usr/bin/env python3
"""tests/test_meituan_served_catalog_sync.py — Sep 2026 served-catalog sync guard.

Pins the 2026-09-03 reconciliation of ``static/provider_templates/meituan.json``
against the live-gateway probe cache (``data/config/probe_cache/95a225612ce5cf84.json``,
schema v2, 1122 cells). A model counts as SERVED when any probe cell returned
``ok`` or ``rate_limited`` (HTTP 429 with a per-model RPM message proves the
gateway recognizes the id); ``not_found`` cells are genuinely absent.

Three registration surfaces are audited for the newly added models:
  1. ``static/provider_templates/meituan.json`` (recipe presence + caps)
  2. ``lib.pricing._tables.MODEL_PRICING`` (billable rows for wire ids)
  3. ``lib.llm_dispatch.config._aliases.MODEL_ALIAS_GROUPS`` (V4 cloud mirrors)

plus two deliberate exclusions:
  * ``Ring-1T`` / ``Ling-1T`` (Ant Group Bailing) are listed in the gateway
    catalog but every spelling 400s ``不支持的模型类型`` on both faces — a
    recipe would be a dead slot, so they stay OUT of the template.
  * the original GPT-5 family (gpt-5/5.1/5.2/5-mini/5-nano) stays retired per
    the dated note in ``_slots.py`` even though the gateway still serves them.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_meituan_served_catalog_sync.py -v
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from lib.mcp.registry import is_opensource_build
from lib.provider_template_recipes import offering_recipes

pytestmark = [pytest.mark.auth_mode('open'), pytest.mark.unit]

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# model_id -> expected capabilities (per probe-verified marketplace cards /
# family contracts, 2026-09-03).
_EXPECTED_NEW = {
    'claude-sonnet-5':                {'text', 'vision', 'thinking'},
    'MiniMax-M2.1':                   {'text', 'thinking', 'cheap'},
    'deepseek-v4-flash-vision-exp':   {'text', 'vision', 'thinking', 'cheap'},
    'doubao-seed-1-6-vision-250815':  {'text', 'vision', 'cheap'},
    'gemini-2.5-flash-lite':          {'text', 'vision', 'cheap'},
    'gemini-3.1-flash-lite':          {'text', 'vision', 'cheap'},
    'glm-5':                          {'text', 'thinking'},
    'glm-5-turbo':                    {'text', 'cheap'},
    'glm-4.7':                        {'text', 'thinking', 'cheap'},
    'grok-4.6':                       {'text', 'vision', 'thinking'},
    'kimi-k2.6':                      {'text', 'cheap'},
    'kimi-k2.7-code':                 {'text', 'thinking', 'cheap'},
    'kimi-k2.7-code-highspeed':       {'text', 'thinking', 'cheap'},
    'o3-pro':                         {'text', 'thinking'},
    'qwen-mt-plus':                   {'text', 'cheap'},
    'qwen3-vl-plus':                  {'text', 'vision', 'thinking', 'cheap'},
    'gpt-5.2-codex':                  {'text', 'vision', 'thinking'},
    'gpt-5.3-codex':                  {'text', 'vision', 'thinking'},
}

# New wire ids that must carry a billable pricing row (cost keys on wire id).
_EXPECTED_PRICED = [
    'deepseek-v4-flash-tencent', 'deepseek-v4-flash-meituan',
    'deepseek-v4-pro-tencent', 'deepseek-v4-flash-vision-exp',
    'doubao-seed-1-6-vision-250815', 'gemini-2.5-flash-lite',
    'gemini-3.1-flash-lite', 'glm-5-turbo', 'grok-4.6',
    'kimi-k2.7-code', 'kimi-k2.7-code-highspeed', 'o3-pro', 'qwen-mt-plus',
]

# Models the gateway verifiably does NOT serve for chat — must stay absent.
_NEVER_TEMPLATE = ['Ring-1T', 'Ling-1T', 'ring-1t', 'Ring-1T-Flash',
                   'gemini-3.1-flash-lite-preview']


def _recipes() -> dict[str, dict]:
    tpl = json.loads(open(
        os.path.join(_ROOT, 'static', 'provider_templates', 'meituan.json'),
        encoding='utf-8').read())
    return {m['model_id']: m
            for m in offering_recipes(tpl, allow_legacy=False)}


@pytest.mark.skipif(is_opensource_build(),
                    reason='meituan.json is an internal provider template, '
                           'not shipped in opensource builds')
def test_new_served_models_registered_with_matching_caps():
    by_id = _recipes()
    missing = [mid for mid in _EXPECTED_NEW if mid not in by_id]
    assert not missing, 'served models missing from meituan.json: %s' % missing
    bad = []
    for mid, want in _EXPECTED_NEW.items():
        got = set(by_id[mid].get('capabilities') or [])
        if got != want:
            bad.append('%s: caps %r != %r' % (mid, sorted(got), sorted(want)))
    assert not bad, '\n'.join(bad)


@pytest.mark.skipif(is_opensource_build(), reason='internal template')
def test_gemini_31_flash_lite_ga_rename():
    """The gateway 404s the -preview spelling and serves the GA id — the
    template must carry the GA id and drop the dead one."""
    by_id = _recipes()
    assert 'gemini-3.1-flash-lite' in by_id
    assert 'gemini-3.1-flash-lite-preview' not in by_id


@pytest.mark.skipif(is_opensource_build(), reason='internal template')
def test_deepseek_v4_mirror_request_ids():
    by_id = _recipes()
    flash = set(by_id['deepseek-v4-flash'].get('request_ids') or [])
    assert {'deepseek-v4-flash', 'deepseek-v4-flash-huawei',
            'deepseek-v4-flash-tencent', 'deepseek-v4-flash-meituan'} <= flash
    pro = set(by_id['deepseek-v4-pro'].get('request_ids') or [])
    assert {'deepseek-v4-pro', 'deepseek-v4-pro-tencent'} <= pro


def test_new_wire_ids_priced():
    from lib.pricing._tables import MODEL_PRICING
    missing = [mid for mid in _EXPECTED_PRICED if mid not in MODEL_PRICING]
    assert not missing, 'wire ids without a MODEL_PRICING row: %s' % missing


def test_cheap_tags_match_pricing_rows():
    """The 'cheap' tag is derived from MODEL_PRICING (input < $3 AND output
    < $15) — a recipe whose hand-set caps disagree with its row flips red."""
    from lib.llm_dispatch.config._pricing import get_pricing_tiers
    by_id = _recipes()
    bad = []
    for mid in _EXPECTED_NEW:
        recipe_cheap = 'cheap' in set(by_id[mid].get('capabilities') or [])
        priced_cheap = 'cheap' in get_pricing_tiers(mid)
        if recipe_cheap != priced_cheap:
            bad.append('%s: recipe cheap=%s vs pricing cheap=%s'
                       % (mid, recipe_cheap, priced_cheap))
    assert not bad, '\n'.join(bad)


def test_mirror_alias_groups_extended():
    from lib.llm_dispatch.config._aliases import MODEL_ALIASES
    flash_group = MODEL_ALIASES.get('deepseek-v4-flash-tencent')
    assert flash_group is not None
    assert {'deepseek-v4-flash', 'deepseek-v4-flash-huawei',
            'deepseek-v4-flash-meituan'} <= flash_group
    pro_group = MODEL_ALIASES.get('deepseek-v4-pro-tencent')
    assert pro_group is not None and 'deepseek-v4-pro' in pro_group


@pytest.mark.skipif(is_opensource_build(), reason='internal template')
def test_unserved_and_retired_ids_stay_out():
    by_id = _recipes()
    present = [mid for mid in _NEVER_TEMPLATE if mid in by_id]
    assert not present, 'dead/retired ids must not be recipes: %s' % present
    # Original GPT-5 family stays retired (dated note in _slots.py).
    retired = [m for m in ('gpt-5', 'gpt-5.1', 'gpt-5.2', 'gpt-5-mini',
                           'gpt-5-nano') if m in by_id]
    assert not retired, 'retired GPT-5 ids resurrected: %s' % retired


def test_ring1t_evidence_recorded():
    """Ring-1T (Ant Group Bailing) sits in the gateway catalog but 400s on
    both wire faces — pin the taxonomy so at least creator attribution is
    correct offline (bailing family covers the ring- prefix)."""
    from lib.model_catalog._creator_families import FAMILY_ID_PREFIXES
    assert 'ring-' in FAMILY_ID_PREFIXES['bailing']
    assert 'ling-' in FAMILY_ID_PREFIXES['bailing']


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
