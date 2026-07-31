"""Guard: deprecated OpenAI SKUs must NOT reappear in any seam.

2026-07-24 owner-directed retirement:
  * GPT-5.6 shipped as a two-tier lineup (flagship + pro) — the
    generation dropped mini/nano.
  * The original GPT-5 family (gpt-5, gpt-5.2, gpt-5-mini, gpt-5-nano)
    was retired since 5.4 and 5.6 fully supersede its capabilities and
    the OpenAI gateway no longer routes to it.

If auto-discovery / bootstrap templates / a future refresh accidentally
resurrects one of these IDs, this test fires. Codex-branded snapshots
(``gpt-5.2-codex`` / ``gpt-5.1-codex-mini``) are intentionally kept —
Codex is a separate lineup on its own cadence.

There is NO ``gpt-5.5``: OpenAI's cadence went 5.2 → 5.4 → 5.6, so the
guard also blocks a stray 5.5 from being invented.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Exact model IDs the owner retired this turn. Substring matches (e.g.
# ``gpt-5.2-codex`` containing ``gpt-5.2``) are intentionally excluded
# — the guard checks dict membership, not substring occurrence.
_RETIRED_EXACT_IDS = (
    'gpt-5',
    'gpt-5.2',
    'gpt-5-mini',
    'gpt-5-nano',
    'gpt-5.6-mini',
    'gpt-5.6-nano',
    'gpt-5.5',        # never existed; guard against future invention
    'gpt-5.5-mini',
    'gpt-5.5-pro',
    'gpt-5.5-nano',
)


@pytest.mark.unit
class TestDeprecatedOpenAISkuAbsence:
    def test_slots_table_omits_retired_ids(self):
        from lib.llm_dispatch.config import DEFAULT_SLOT_CONFIGS
        for mid in _RETIRED_EXACT_IDS:
            assert mid not in DEFAULT_SLOT_CONFIGS, (
                f'{mid!r} resurfaced in DEFAULT_SLOT_CONFIGS — did '
                'auto-discovery repopulate a retired SKU? See '
                'lib/llm_dispatch/config/_slots.py comment.'
            )

    def test_pricing_table_omits_retired_ids(self):
        from lib.pricing._tables import MODEL_PRICING
        for mid in _RETIRED_EXACT_IDS:
            assert mid not in MODEL_PRICING, (
                f'{mid!r} resurfaced in MODEL_PRICING — pricing must '
                'be pruned when a slot is retired.'
            )

    def test_bootstrap_openai_template_omits_retired_ids(self):
        """The bootstrap installer's inline OpenAI template must not
        offer a retired SKU as a first-run default."""
        from bootstrap import _BUILTIN_PROVIDER_TEMPLATES
        openai_tpl = next(
            t for t in _BUILTIN_PROVIDER_TEMPLATES if t['key'] == 'openai'
        )
        offered = {m['model_id'] for m in openai_tpl['models']}
        for mid in _RETIRED_EXACT_IDS:
            assert mid not in offered, (
                f'{mid!r} still offered by bootstrap OpenAI template'
            )

    def test_provider_templates_js_omits_retired_ids(self):
        """The frontend Settings provider-picker mirror must not offer
        retired SKUs either. Text-grep against exact quoted IDs (the
        file is not JSON, so parse-vs-grep is a wash — grep is stricter
        because it also catches stray occurrences in comments)."""
        js_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'static', 'js', 'settings', 'provider_templates.js',
        )
        with open(js_path, 'r', encoding='utf-8') as f:
            body = f.read()
        for mid in _RETIRED_EXACT_IDS:
            # Match the model_id literal only ('gpt-5.6-mini') — a bare
            # ``gpt-5`` substring would false-positive on 5.4 / 5.6, so
            # anchor on a quote + exact ID + non-word terminator.
            pattern = r"['\"]" + re.escape(mid) + r"['\"]"
            hits = re.findall(pattern, body)
            assert not hits, (
                f'{mid!r} still referenced in provider_templates.js '
                f'({len(hits)} hit[s])'
            )
