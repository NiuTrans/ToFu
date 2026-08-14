from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def _entry(model_id: str, quality: str, price: float, roles: list[str]) -> dict:
    return {
        'model_id': model_id,
        'capabilities': ['text'],
        'pricing': {'input': price, 'output': price},
        'capability_profile': {
            'family': 'test-family',
            'quality': quality,
            'roles': roles,
            'evidence': 'operator',
            'confidence': 1.0,
        },
    }


def test_gpt_56_product_hierarchy_is_explicit_and_proven():
    from lib.model_profiles import build_model_profile

    profiles = {
        model: build_model_profile(model)
        for model in ('gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna')
    }
    assert profiles['gpt-5.6-sol']['qualityScore'] > profiles['gpt-5.6-terra']['qualityScore']
    assert profiles['gpt-5.6-terra']['qualityScore'] > profiles['gpt-5.6-luna']['qualityScore']
    assert all(profile['autoSelectable'] for profile in profiles.values())


def test_role_selection_uses_cheapest_model_that_clears_hard_tier():
    from lib.model_profiles import select_model_for_tier

    providers = [{
        'id': 'supplier', 'enabled': True,
        'models': [
            _entry('frontier-expensive', 'frontier', 20, ['coder']),
            _entry('heavy-cheap', 'heavy', 2, ['coder']),
            _entry('light-cheapest', 'light', 0.2, ['coder']),
        ],
    }]
    assert select_model_for_tier(
        'heavy', parent_model='frontier-expensive', role='coder',
        provider_id='supplier', providers=providers,
    ) == 'heavy-cheap'


def test_role_selection_never_uses_below_threshold_or_wrong_role():
    from lib.model_profiles import select_model_for_tier

    providers = [{
        'id': 'supplier', 'enabled': True,
        'models': [
            _entry('heavy-writer', 'heavy', 0.1, ['writer']),
            _entry('light-coder', 'light', 0.2, ['coder']),
        ],
    }]
    assert select_model_for_tier(
        'heavy', parent_model='chosen-parent', role='coder',
        provider_id='supplier', providers=providers,
    ) == 'chosen-parent'


def test_catalog_profile_refresh_preserves_operator_override_and_ignores_timestamp_only():
    from lib.llm_dispatch.model_catalog_sync import reconcile_catalog_models

    operator = _entry('operator-model', 'heavy', 2, ['coder'])
    incoming_operator = _entry('operator-model', 'light', 1, ['writer'])
    incoming_operator['capability_profile']['evidence'] = 'provider_catalog'
    result = reconcile_catalog_models([operator], [incoming_operator])
    assert result['updated'] == []
    assert result['models'][0]['capability_profile']['quality'] == 'heavy'

    current = _entry('auto-model', 'heavy', 2, ['coder'])
    current['capability_profile'].update(evidence='provider_catalog', updated_at=100)
    incoming = _entry('auto-model', 'heavy', 2, ['coder'])
    incoming['capability_profile'].update(evidence='provider_catalog', updated_at=200)
    result = reconcile_catalog_models([current], [incoming])
    assert result['updated'] == []
    assert result['models'][0]['capability_profile']['updated_at'] == 100


def test_capabilities_api_exposes_profile_per_provider(monkeypatch):
    import lib
    from routes.api_v1.capabilities import _models_summary

    monkeypatch.setattr(lib, '_SAVED_CONFIG', {'providers': [{
        'id': 'supplier', 'models': [
            _entry('gpt-5.6-terra', 'heavy', 2, ['coder']),
        ],
    }]})
    profile = _models_summary()[0]['capability_profile']
    assert profile['providerId'] == 'supplier'
    assert profile['quality'] == 'heavy'
    assert profile['autoSelectable'] is True


def test_settings_card_renders_profile_provenance_and_routing_state():
    source = Path('frontend/src/runtime/app-runtime.js').read_text(
        encoding='utf-8')
    assert 'm.capability_profile || null' in source
    assert "settings.modelProfileDetail" in source
    assert "settings.modelProfileAuto" in source
    assert "settings.modelProfileManual" in source
