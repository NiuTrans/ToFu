"""Integration seams between model cards and the native health owner.

Detailed behavior lives in ``test_frontend_key_stats_vite.py`` and
``test_frontend_model_edit_redesign.py``.  This guard pins the retained
settings renderer to those native owners without evaluating deleted classic
``settings/key_stats.js`` bytes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section

pytestmark = pytest.mark.unit


def test_model_card_health_and_pricing():
    provider = runtime_section('settings/provider_render.js')
    editor = runtime_section('settings/model_edit.js')
    native = (Path(__file__).resolve().parents[1]
              / 'frontend/src/features/settings/key-stats.ts').read_text()

    # Price resolution remains explicit override → discovery → global cache.
    override = provider.index(
        'if (m.pricing && m.pricing.input != null && m.pricing.output != null)')
    discovery = provider.index(
        'else if (m.input_price != null && m.output_price != null)', override)
    cache = provider.index("_modelPricingCache[m.model_id] || null", discovery)
    assert override < discovery < cache
    assert 'stg-price-custom' in provider

    # The retained renderer consumes the native model-health bridge and keeps
    # refreshes scoped to the strip rather than repainting the edit form.
    for binding in ('_modelCardHealthCls', '_modelCardHealthHTML'):
        assert f"typeof {binding} === 'function'" in provider
    assert 'data-prov="' in provider and 'data-model="' in provider

    # The native owner folds the complete wire pool and publishes every bridge
    # method the retained renderer/settings lifecycle calls.
    for export in ('modelWireIds', 'modelCardHealthRow', 'modelCardHealthHtml',
                   'modelCardHealthClass', 'refreshAllModelCardHealth'):
        assert f'export function {export}' in native
    for bridge in ('_modelCardHealthRow', '_modelCardHealthHTML',
                   '_modelCardHealthCls', '_refreshAllModelCardHealth'):
        assert f'bridge.{bridge} =' in native
    assert 'aggregate.contention_errors +=' in native
    assert 'aggregate.gateway_errors +=' in native
    assert "aggregate.verdict.level === 'degraded'" in native
    assert "aggregate.verdict.level === 'down'" in native

    # Invalid partial/negative overrides reject before mutation; a valid pair
    # persists the real rate pair used by backend accounting.
    validation = editor.index('if (!_bothEmpty && !_bothValid)')
    mutation = editor.index('m.model_id =', validation)
    assert validation < mutation
    assert '_pin >= 0 && _pout >= 0' in editor
    assert '_pr.input = _pin;' in editor and '_pr.output = _pout;' in editor
    assert "_pr.unit = 'per_million_tokens';" in editor
