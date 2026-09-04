from __future__ import annotations

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


def test_runtime_selection_reads_only_the_requested_owner_v2_authority():
    from lib.model_profiles import (
        configured_model_profiles,
        select_model_for_tier,
    )
    from lib.model_routing import (
        InMemoryModelRoutingRepository,
        OwnerBoundary,
        empty_document,
        upsert_local_provider,
    )

    repository = InMemoryModelRoutingRepository()
    alice = OwnerBoundary.create(41, 'tenant-a')
    bob = OwnerBoundary.create(42, 'tenant-a')
    repository.compare_and_swap(alice, empty_document(), expected_revision=0)
    repository.compare_and_swap(bob, empty_document(), expected_revision=0)
    upsert_local_provider(
        repository,
        alice,
        provider_id='alice-local',
        display_name='Alice local',
        base_url='http://127.0.0.1:18001/v1',
        models=[{
            'model_id': 'gpt-5.6-terra',
            'capabilities': ['text', 'thinking'],
            'context_window': 256_000,
        }],
    )

    alice_profiles = configured_model_profiles(
        owner_user_id=41,
        tenant_id='tenant-a',
        repository=repository,
    )
    bob_profiles = configured_model_profiles(
        owner_user_id=42,
        tenant_id='tenant-a',
        repository=repository,
    )
    assert [(row['providerId'], row['modelId']) for row in alice_profiles] == [
        ('alice-local', 'gpt-5.6-terra')]
    assert bob_profiles == []
    assert select_model_for_tier(
        'heavy',
        parent_model='parent-model',
        owner_user_id=41,
        tenant_id='tenant-a',
        repository=repository,
    ) == 'gpt-5.6-terra'
    assert select_model_for_tier(
        'heavy',
        parent_model='parent-model',
        owner_user_id=42,
        tenant_id='tenant-a',
        repository=repository,
    ) == 'parent-model'
