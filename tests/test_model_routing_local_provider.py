"""Result contracts for owner-scoped local endpoint registration."""

from __future__ import annotations

import copy

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    ModelRoutingError,
    OwnerBoundary,
    connection_urls,
    delete_local_provider,
    empty_document,
    normalize_document,
    upsert_local_provider,
)


pytestmark = pytest.mark.unit


def _active_repository() -> tuple[InMemoryModelRoutingRepository, OwnerBoundary]:
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(41, "tenant-a")
    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    return repository, boundary


def _models(*model_ids: str) -> list[dict]:
    return [
        {
            "model_id": model_id,
            "capabilities": ["text", "thinking"],
            "context_window": 65_536,
            "rpm": 60,
        }
        for model_id in model_ids
    ]


def test_upsert_builds_complete_pending_identity_bundle() -> None:
    repository, boundary = _active_repository()

    result = upsert_local_provider(
        repository,
        boundary,
        provider_id="auto_ollama_11434",
        display_name="Ollama (auto)",
        base_url="http://127.0.0.1:11434/v1/",
        models=_models("qwen3:8b", "llama3.1"),
    )

    assert result.changed is True
    assert result.authority.revision == 2
    document = normalize_document(result.authority.document)
    assert document["providers"] == [{
        "provider_id": "auto_ollama_11434",
        "name": "Ollama (auto)",
        "scope": "owner",
        "brand": "local",
    }]
    assert document["models"] == []
    assert document["creators"] == []
    assert len(document["provider_accesses"]) == 1
    assert len(document["connections"]) == 1
    assert document["connections"][0]["base_url"] == (
        "http://127.0.0.1:11434/v1")
    assert document["connections"][0]["protocol"] == "local"
    assert document["credentials"][0]["kind"] == "local_identity"
    assert document["credentials"][0]["secret_reference"] == ""
    assert document["credentials"][0]["authorization"]["models"] == []
    assert {row["pending_model_id"] for row in document["offerings"]} == {
        "qwen3:8b", "llama3.1"}
    assert {row["identity_state"] for row in document["offerings"]} == {
        "pending_identity"}
    assert {row["wire_model_id"] for row in document["deployments"]} == {
        "qwen3:8b", "llama3.1"}
    assert {row["probe_status"] for row in document["deployments"]} == {
        "passed"}


def test_upsert_is_idempotent_and_replaces_drift_without_growth() -> None:
    repository, boundary = _active_repository()
    arguments = {
        "provider_id": "managed_vllm_18100",
        "display_name": "Qwen (local)",
        "base_url": "http://127.0.0.1:18100/v1",
        "models": _models("qwen3"),
    }

    first = upsert_local_provider(repository, boundary, **arguments)
    repeated = upsert_local_provider(repository, boundary, **arguments)
    changed = upsert_local_provider(
        repository,
        boundary,
        **{**arguments, "models": _models("qwen3", "qwen3-coder")},
    )

    assert first.changed is True
    assert repeated.changed is False
    assert repeated.authority.revision == first.authority.revision
    assert changed.changed is True
    assert changed.authority.revision == first.authority.revision + 1
    assert len(changed.authority.document["providers"]) == 1
    assert len(changed.authority.document["provider_accesses"]) == 1
    assert len(changed.authority.document["connections"]) == 1
    assert len(changed.authority.document["credentials"]) == 1
    assert len(changed.authority.document["offerings"]) == 2
    assert len(changed.authority.document["deployments"]) == 2


def test_refresh_preserves_user_policy_while_updating_discovered_facts() -> None:
    repository, boundary = _active_repository()
    first = upsert_local_provider(
        repository,
        boundary,
        provider_id="auto_ollama_11434",
        display_name="Ollama (auto)",
        base_url="http://127.0.0.1:11434/v1",
        models=_models("qwen3:8b"),
    )
    edited = copy.deepcopy(first.authority.document)
    edited["providers"][0]["name"] = "My Ollama"
    edited["provider_accesses"][0].update({
        "enabled": False,
        "display_name": "Desk GPU",
        "quota_policy": {"rpm": 7},
    })
    edited["connections"][0].update({
        "enabled": False,
        "priority": 9,
        "extra_headers": {"X-Local-Route": "desk"},
    })
    edited["credentials"][0].update({
        "enabled": False,
        "quota_policy": {"rpm": 5},
    })
    edited["offerings"][0].update({
        "enabled": False,
        "priority": 8,
        "actual_pricing": {
            "input": 1,
            "output": 2,
            "currency": "USD",
            "unit": "per_million_tokens",
        },
    })
    edited["deployments"][0].update({"enabled": False, "priority": 6})
    repository.compare_and_swap(
        boundary, edited, expected_revision=first.authority.revision)

    refreshed = upsert_local_provider(
        repository,
        boundary,
        provider_id="auto_ollama_11434",
        display_name="Ollama discovered name",
        base_url="http://127.0.0.1:11434/v1",
        models=[
            {
                "model_id": "qwen3:8b",
                "capabilities": ["text"],
                "context_window": 131_072,
            },
            {
                "model_id": "new-model",
                "capabilities": ["text"],
                "context_window": 16_384,
            },
        ],
    )

    document = refreshed.authority.document
    assert document["providers"][0]["name"] == "My Ollama"
    assert document["provider_accesses"][0]["display_name"] == "Desk GPU"
    assert document["provider_accesses"][0]["enabled"] is False
    assert document["provider_accesses"][0]["quota_policy"] == {"rpm": 7}
    assert document["connections"][0]["enabled"] is False
    assert document["connections"][0]["priority"] == 9
    assert document["connections"][0]["extra_headers"] == {
        "X-Local-Route": "desk"}
    assert document["credentials"][0]["enabled"] is False
    assert document["credentials"][0]["quota_policy"] == {"rpm": 5}
    offerings = {
        row["pending_model_id"]: row for row in document["offerings"]}
    assert offerings["qwen3:8b"]["enabled"] is False
    assert offerings["qwen3:8b"]["priority"] == 8
    assert offerings["qwen3:8b"]["actual_pricing"]["input"] == 1
    assert offerings["qwen3:8b"]["capabilities"] == ["text"]
    assert offerings["qwen3:8b"]["context_window"] == 131_072
    assert offerings["new-model"]["enabled"] is True
    deployments = {
        row["wire_model_id"]: row for row in document["deployments"]}
    assert deployments["qwen3:8b"]["enabled"] is False
    assert deployments["qwen3:8b"]["priority"] == 6
    assert deployments["new-model"]["enabled"] is True


def test_delete_removes_only_provider_owned_resources() -> None:
    repository, boundary = _active_repository()
    upsert_local_provider(
        repository,
        boundary,
        provider_id="auto_ollama_11434",
        display_name="Ollama",
        base_url="http://127.0.0.1:11434/v1",
        models=_models("llama3.1"),
    )
    before = repository.get(boundary)
    document = copy.deepcopy(before.document)
    document["creators"].append({"creator_id": "kept", "name": "Kept"})
    document["models"].append({
        "creator_id": "kept",
        "model_id": "kept-model",
        "display_name": "Kept Model",
        "capabilities": ["text"],
        "context_window": 8_192,
        "quality_rank": 1,
    })
    repository.compare_and_swap(
        boundary, document, expected_revision=before.revision)

    removed = delete_local_provider(
        repository, boundary, provider_id="auto_ollama_11434")
    repeated = delete_local_provider(
        repository, boundary, provider_id="auto_ollama_11434")

    assert removed.changed is True
    assert removed.authority.document["providers"] == []
    assert removed.authority.document["provider_accesses"] == []
    assert removed.authority.document["connections"] == []
    assert removed.authority.document["credentials"] == []
    assert removed.authority.document["offerings"] == []
    assert removed.authority.document["deployments"] == []
    assert removed.authority.document["creators"][0]["creator_id"] == "kept"
    assert removed.authority.document["models"][0]["model_id"] == "kept-model"
    assert repeated.changed is False
    assert repeated.authority.revision == removed.authority.revision


def test_connection_projection_and_owner_isolation() -> None:
    repository, first_boundary = _active_repository()
    second_boundary = OwnerBoundary.create(42, "tenant-a")
    repository.compare_and_swap(
        second_boundary, empty_document(), expected_revision=0)
    upsert_local_provider(
        repository,
        first_boundary,
        provider_id="local-a",
        display_name="Local A",
        base_url="http://127.0.0.1:8000/v1",
        models=_models("a"),
    )

    assert connection_urls(repository.get(first_boundary).document) == {
        "local-a": ["http://127.0.0.1:8000/v1"]}
    assert connection_urls(repository.get(second_boundary).document) == {}


def test_inactive_authority_and_invalid_model_fail_closed() -> None:
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(41)
    with pytest.raises(ModelRoutingError) as inactive:
        upsert_local_provider(
            repository,
            boundary,
            provider_id="local-a",
            display_name="Local A",
            base_url="http://127.0.0.1:8000/v1",
            models=_models("a"),
        )
    assert inactive.value.kind == "model_routing_authority_inactive"

    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    with pytest.raises(ModelRoutingError) as invalid:
        upsert_local_provider(
            repository,
            boundary,
            provider_id="local-a",
            display_name="Local A",
            base_url="http://127.0.0.1:8000/v1",
            models=[{"model_id": "\n"}],
        )
    assert invalid.value.kind == "local_provider_invalid"
