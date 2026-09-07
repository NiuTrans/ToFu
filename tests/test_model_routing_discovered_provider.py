"""Authenticated discovery compiles directly into model-routing v2."""

from __future__ import annotations

import asyncio
import copy

import pytest
from quart import Quart, g

from lib.api_keys import local_admin_context
from lib.identity import principal_from_auth_context
from lib.model_routing import (
    build_discovered_provider_bundle,
    discovered_provider_id,
    empty_document,
    normalize_document,
)


pytestmark = pytest.mark.unit


def _bundle() -> dict:
    return build_discovered_provider_bundle(
        provider_id=discovered_provider_id(
            "deepseek", "https://api.deepseek.com/v1/"),
        display_name="DeepSeek",
        brand="deepseek",
        base_url="https://api.deepseek.com/v1/",
        models=[
            {
                "model_id": "deepseek-chat",
                "capabilities": ["text", "cheap", "text"],
                "context_window": 65_536,
                "rpm": 40,
            },
            {
                "model_id": "deepseek-reasoner",
                "capabilities": ["text", "thinking"],
                "rpm": 20,
            },
        ],
    )


def test_discovery_bundle_is_stable_complete_and_pending_identity() -> None:
    first = _bundle()
    repeated = _bundle()

    assert first == repeated
    assert first["provider"]["scope"] == "owner"
    assert first["provider_access"]["quota_policy"] == {"rpm": 20}
    assert first["connections"][0]["base_url"] == (
        "https://api.deepseek.com/v1")
    assert first["credentials"][0]["secret_reference"] == ""
    assert {row["identity_state"] for row in first["offerings"]} == {
        "pending_identity"}
    assert {row["pending_model_id"] for row in first["offerings"]} == {
        "deepseek-chat", "deepseek-reasoner"}
    assert {row["wire_model_id"] for row in first["deployments"]} == {
        "deepseek-chat", "deepseek-reasoner"}

    document = empty_document()
    document["providers"].append(first["provider"])
    document["provider_accesses"].append(first["provider_access"])
    for collection in (
        "connections", "credentials", "offerings", "deployments",
    ):
        document[collection].extend(copy.deepcopy(first[collection]))
    document["credentials"][0]["secret_reference"] = "vault-test"
    assert normalize_document(document)["providers"][0]["provider_id"] == (
        first["provider"]["provider_id"])


def test_probe_route_returns_secret_free_v2_draft(monkeypatch) -> None:
    from lib.llm_dispatch import discovery
    from routes.api_v1.providers import api_v1_providers_bp

    monkeypatch.setattr(discovery, "probe_provider", lambda *_args, **_kwargs: {
        "ok": True,
        "brand": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "is_local": False,
        "models": [{
            "model_id": "deepseek-chat",
            "capabilities": ["text"],
            "context_window": 65_536,
        }],
        "balance_url": "https://api.deepseek.com/balance",
        "thinking_format": "reasoning_content",
        "summary": {"total": 1},
    })

    app = Quart(__name__, static_folder=None)
    app.config["TESTING"] = True

    @app.before_request
    def _identity() -> None:
        context = local_admin_context()
        g.auth_ctx = context
        g.principal_context = principal_from_auth_context(
            context, allow_personal_owner=True)

    app.register_blueprint(api_v1_providers_bp)

    async def exercise() -> dict:
        response = await app.test_client().post(
            "/api/v1/providers/probe",
            json={
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "sk-do-not-echo",
            },
        )
        assert response.status_code == 200
        return await response.get_json()

    payload = asyncio.run(exercise())
    assert payload["ok"] is True
    assert payload["model_count"] == 1
    assert payload["provider_bundle"]["offerings"][0][
        "pending_model_id"] == "deepseek-chat"
    assert payload["credential_id"] == payload["provider_bundle"][
        "credentials"][0]["credential_id"]
    serialized = repr(payload)
    assert "sk-do-not-echo" not in serialized
    assert "balance" not in serialized
    assert "reasoning_content" not in serialized
