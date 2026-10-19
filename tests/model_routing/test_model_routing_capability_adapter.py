"""Owner isolation and eligibility contracts for non-chat v2 routing."""

from __future__ import annotations

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    ModelRoutingError,
    OPENAI_COMPATIBLE_PROTOCOLS,
    OwnerBoundary,
    empty_document,
    list_capability_route_groups,
    list_capability_routes,
    mint_capability_slot_group,
    upsert_local_provider,
)


pytestmark = pytest.mark.unit


def _owner_with_image_route(owner_user_id: int):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(owner_user_id, "tenant-a")
    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    # Mint the catalog model FIRST (complete synthetic registration, or the
    # admission boundary leaves it a non-callable draft): upsert's identity
    # reconcile can only confirm a pending offering against an existing
    # catalog entry.
    from tests.support.model_registration import complete_registration
    authority = repository.get(boundary)
    seeded = complete_registration({
        'contract_version': 'tofu.model-routing/v2',
        'revision': authority.revision,
        'creators': [{'creator_id': 'local', 'name': 'Local providers'}],
        'models': [{
            'creator_id': 'local',
            'model_id': 'local-image-v1',
            'display_name': 'local-image-v1',
            'capabilities': ['image_gen'],
            'context_window': 8192,
            'quality_rank': 1,
        }],
        'providers': [], 'provider_accesses': [], 'connections': [],
        'credentials': [], 'offerings': [], 'deployments': [],
    })
    repository.compare_and_swap(
        boundary, seeded, expected_revision=authority.revision)
    upsert_local_provider(
        repository,
        boundary,
        provider_id=f"owner_{owner_user_id}_images",
        display_name=f"Owner {owner_user_id} Images",
        base_url="http://127.0.0.1:18100/v1",
        models=[{
            "model_id": "local-image-v1",
            "capabilities": ["image_gen"],
            "context_window": 8192,
            "rpm": 12,
        }],
    )
    return repository, boundary


def test_capability_listing_is_owner_scoped_and_secret_free() -> None:
    repository, alice = _owner_with_image_route(41)
    bob = OwnerBoundary.create(42, "tenant-a")

    alice_routes = list_capability_routes(repository, alice, "image_gen")

    assert [route.public_dict() for route in alice_routes] == [{
        "model": "local-image-v1",
        "display_name": "local-image-v1",
        "available": True,
        "provider_id": "owner_41_images",
        "provider_name": "Owner 41 Images",
        "offering_id": alice_routes[0].offering_id,
    }]
    assert list_capability_routes(repository, bob, "image_gen") == []
    assert len(list_capability_routes(
        repository,
        alice,
        "image_gen",
        required_protocols=OPENAI_COMPATIBLE_PROTOCOLS,
    )) == 1
    assert "secret" not in repr(alice_routes[0].public_dict()).lower()


def test_capability_listing_ignores_legacy_switch_and_probe_diagnostic() -> None:
    repository, boundary = _owner_with_image_route(41)
    authority = repository.get(boundary)
    document = authority.document
    document["deployments"][0]["enabled"] = False
    document["deployments"][0]["probe_status"] = "failed"
    repository.compare_and_swap(
        boundary, document, expected_revision=authority.revision)

    assert len(list_capability_routes(repository, boundary, "image_gen")) == 1
    assert "enabled" not in repository.get(boundary).document["deployments"][0]


def test_capability_mint_fails_closed_for_foreign_owner(monkeypatch) -> None:
    repository, _alice = _owner_with_image_route(41)
    bob = OwnerBoundary.create(42, "tenant-a")
    monkeypatch.setattr(
        "lib.model_routing.capability_adapter.mint_routed_slot_group",
        lambda *_args, **_kwargs: pytest.fail("must not mint without a route"),
    )

    with pytest.raises(ModelRoutingError) as error:
        mint_capability_slot_group(repository, bob, "image_gen")

    assert error.value.kind == "model_route_unavailable"


def test_capability_protocol_filter_and_candidate_budget_are_forwarded(
    monkeypatch,
) -> None:
    repository, boundary = _owner_with_image_route(41)
    authority = repository.get(boundary)
    document = authority.document
    document["connections"][0]["protocol"] = "anthropic"
    repository.compare_and_swap(
        boundary, document, expected_revision=authority.revision)

    assert list_capability_routes(
        repository,
        boundary,
        "image_gen",
        required_protocols=frozenset({"openai"}),
    ) == []

    # Restore an eligible protocol, then prove the capability adapter carries
    # the caller's resident-slot budget into the dispatch bridge.
    authority = repository.get(boundary)
    document = authority.document
    document["connections"][0]["protocol"] = "openai"
    repository.compare_and_swap(
        boundary, document, expected_revision=authority.revision)
    sentinel = object()
    captured = {}

    def _mint(*_args, **kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(
        "lib.model_routing.capability_adapter.mint_routed_slot_group", _mint)
    _model, group = mint_capability_slot_group(
        repository, boundary, "image_gen", max_candidates=3)

    assert group is sentinel
    assert captured["max_candidates"] == 3


def test_grouped_capability_listing_reads_and_indexes_one_authority_once(
    monkeypatch,
) -> None:
    import lib.model_routing.routing as routing

    repository, boundary = _owner_with_image_route(41)
    original_get = repository.get
    original_normalize = routing.normalize_document
    reads = []
    normalizations = []

    def counted_get(owner_boundary):
        reads.append(owner_boundary)
        return original_get(owner_boundary)

    def counted_normalize(document, *args, **kwargs):
        normalizations.append(document)
        return original_normalize(document, *args, **kwargs)

    monkeypatch.setattr(repository, "get", counted_get)
    monkeypatch.setattr(routing, "normalize_document", counted_normalize)

    groups = list_capability_route_groups(
        repository,
        boundary,
        {
            "image_gen": OPENAI_COMPATIBLE_PROTOCOLS,
            "transcription": OPENAI_COMPATIBLE_PROTOCOLS,
        },
    )

    assert len(groups["image_gen"]) == 1
    assert groups["transcription"] == []
    assert reads == [boundary]
    assert len(normalizations) == 1



def test_capability_groups_above_old_quota_share_one_authority_revision(monkeypatch):
    from lib.model_routing import capability_adapter as adapter
    repository, boundary = _owner_with_image_route(41)
    reads = []
    get = repository.get
    def counted(owner):
        reads.append(owner)
        return get(owner)
    monkeypatch.setattr(repository, 'get', counted)
    requirements = {f'capability-{i}': frozenset() for i in range(33)}
    requirements['image_gen'] = frozenset()
    groups = list_capability_route_groups(repository, boundary, requirements)
    assert list(groups) == list(requirements)
    assert len(groups['image_gen']) == 1
    assert reads == [boundary]
