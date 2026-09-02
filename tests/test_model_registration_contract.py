"""Canonical model-registration contract and GLM-5.3 regression guards."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_public_registration_rejects_routing_cost():
    from lib.model_registration import (
        ModelRegistrationError, normalize_model_entry,
    )

    with pytest.raises(ModelRegistrationError, match='pricing.input'):
        normalize_model_entry({
            'model_id': 'new-model',
            'capabilities': ['text'],
            'cost': 0.004,
        }, reject_legacy_cost=True)


def test_legacy_cost_is_removed_and_real_prices_are_canonical():
    from lib.model_registration import normalize_model_entry

    row = normalize_model_entry({
        'model_id': 'legacy-model',
        'capabilities': ['text'],
        'rpm': 60,
        'cost': 0.004,
        'input_price': 1.25,
        'output_price': 5,
    })
    assert 'cost' not in row
    assert 'input_price' not in row
    assert 'output_price' not in row
    assert row['pricing'] == {
        'input': 1.25,
        'output': 5.0,
        'currency': 'USD',
        'unit': 'per_million_tokens',
    }


def test_registration_prices_logical_and_wire_ids_and_derives_routing_value():
    from lib.model_registration import register_model, routing_cost_per_1k
    from lib.pricing import clear_provider_pricing, lookup_pricing

    provider_id = 'registration-contract-test'
    clear_provider_pricing(provider_id)
    try:
        row = register_model({
            'model_id': 'logical-model',
            'request_ids': ['wire-a', 'wire-b'],
            'capabilities': ['text'],
            'rpm': 30,
            'context_window': 256_000,
            'pricing': {'input': 2, 'output': 6, 'currency': 'USD'},
        }, provider_id=provider_id)
        assert 'cost' not in row
        for model_id in ('logical-model', 'wire-a', 'wire-b'):
            price = lookup_pricing(model_id, provider_id)
            assert price['input'] == 2
            assert price['output'] == 6
        assert routing_cost_per_1k(
            row, provider_id=provider_id, wire_model_id='wire-a') == 0.004
    finally:
        clear_provider_pricing(provider_id)


def test_unscoped_registration_installs_global_billable_pricing():
    from lib.model_registration import register_model
    from lib.pricing import MODEL_PRICING, lookup_pricing

    model_id = 'global-registration-contract-model'
    MODEL_PRICING.pop(model_id, None)
    try:
        register_model({
            'model_id': model_id,
            'capabilities': ['text'],
            'pricing': {'input': 0.5, 'output': 2, 'currency': 'USD'},
        })
        assert lookup_pricing(model_id)['input'] == pytest.approx(0.5)
        assert lookup_pricing(model_id)['output'] == pytest.approx(2)
    finally:
        MODEL_PRICING.pop(model_id, None)


def test_replacing_provider_snapshot_clears_removed_model_metadata():
    from lib.model_registration import (
        canonicalize_providers, registered_context_profile,
    )
    from lib.pricing import lookup_pricing

    provider_id = 'registration-replacement-test'
    canonicalize_providers([{
        'id': provider_id,
        'models': [{
            'model_id': 'removed-registration-model',
            'context_window': 64_000,
            'pricing': {'input': 1, 'output': 3},
        }],
    }])
    assert registered_context_profile(
        'removed-registration-model', provider_id)['window'] == 64_000
    assert lookup_pricing(
        'removed-registration-model', provider_id)['input'] == 1

    canonicalize_providers([{'id': provider_id, 'models': []}])
    assert registered_context_profile(
        'removed-registration-model', provider_id) is None
    assert lookup_pricing('removed-registration-model', provider_id) is None


def test_registered_context_is_the_model_info_source_of_truth():
    from lib.model_info import context_profile
    from lib.model_registration import register_model

    register_model({
        'model_id': 'context-contract-model',
        'capabilities': ['text'],
        'context_window': 512_000,
    }, provider_id='context-contract-provider')
    assert context_profile(
        'context-contract-model', 'context-contract-provider') == {
            'window': 512_000,
            'source': 'model_registration',
            'exact': True,
        }


def test_glm53_uses_the_complete_registration_shape():
    from lib.provider_template_recipes import offering_recipes

    for filename in ('glm.json', 'meituan.json'):
        template = json.loads((
            ROOT / 'static' / 'provider_templates' / filename
        ).read_text(encoding='utf-8'))
        recipes = offering_recipes(template, allow_legacy=False)
        row = next(
            model for model in recipes if model.get('model_id') == 'glm-5.3')
        assert 'cost' not in row, filename
        assert row['context_window'] == 1_000_000, filename
        assert row['pricing']['input'] == pytest.approx(3.45), filename
        assert row['pricing']['output'] == pytest.approx(13.81), filename
