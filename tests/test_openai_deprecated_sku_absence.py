"""Guard unsupported or fabricated OpenAI model IDs across catalogue seams."""
from __future__ import annotations

import os
import sys
from pathlib import Path

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
    'gpt-5.6-pro',   # Pro is reasoning.mode="pro", not a model ID
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

    def test_current_provider_templates_omit_unsupported_ids(self):
        root = Path(__file__).resolve().parents[1]
        bodies = [
            (root / 'lib/model_info/data/openai.json').read_text(),
            (root / 'frontend/src/runtime/app-runtime.js').read_text(),
        ]
        for mid in _RETIRED_EXACT_IDS:
            literal_hits = sum(
                body.count(f"'{mid}'") + body.count(f'"{mid}"')
                for body in bodies)
            assert literal_hits == 0, f'{mid!r} still offered by a provider template'
