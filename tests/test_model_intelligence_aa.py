"""Artificial Analysis is owner-scoped Model enrichment, never Provider state."""

from __future__ import annotations

import asyncio

import pytest
from quart import Quart, g

from lib.api_keys import local_admin_context
from lib.identity import principal_from_auth_context
from lib.model_catalog import aa
from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document


pytestmark = pytest.mark.unit

_AA_ROW = {
    "name": "DeepSeek V4 Flash",
    "slug": "deepseek-v4-flash",
    "model_creator": {"name": "DeepSeek"},
    "evaluations": {
        "artificial_analysis_intelligence_index": 78.4,
        "artificial_analysis_coding_index": 81.2,
        "artificial_analysis_agentic_index": 79.1,
        "artificial_analysis_math_index": 76.0,
    },
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.delenv("TOFU_AA_API_KEY", raising=False)
    monkeypatch.setattr(aa, "_memo", None)
    monkeypatch.setattr(aa, "_background", {
        "running": False, "last_attempt": 0.0, "thread": None,
    })
    monkeypatch.setattr(
        aa, "_cache_path", lambda: str(tmp_path / "aa_index" / "models.json"))


def _fetcher(expected_key: str):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [_AA_ROW]}

    def fetch(_url, **kwargs):
        assert kwargs["headers"]["x-api-key"] == expected_key
        return Response()

    return fetch


def _models():
    return [{
        "creator_id": "deepseek",
        "model_id": "deepseek-v4-flash",
        "display_name": "DeepSeek V4 Flash",
    }]


def test_no_key_is_explicit_and_never_invents_scores():
    block = aa.aa_block_for_models(
        _models(), api_key="", key_source=None)
    assert block["status"] == "no_key"
    assert block["scores"] == {}
    assert block["key_source"] is None


def test_scores_are_keyed_by_exact_creator_model_identity():
    dataset = aa._dataset(
        "owner-key", force=True, fetcher=_fetcher("owner-key"), now=1000.0)
    block = aa._block(
        _models(), dataset, key_source="settings", key_hint="•••-key")
    assert block["scores"]["deepseek::deepseek-v4-flash"] == {
        "intelligence": 78.4,
        "coding": 81.2,
        "agentic": 79.1,
        "math": 76.0,
        "aa_name": "DeepSeek V4 Flash",
        "aa_slug": "deepseek-v4-flash",
    }
    assert "provider" not in str(block).lower()


def test_same_model_name_from_another_creator_is_never_attached():
    other_creator = {
        **_AA_ROW,
        "model_creator": {"name": "Unrelated Lab"},
        "evaluations": {
            **_AA_ROW["evaluations"],
            "artificial_analysis_intelligence_index": 3.0,
        },
    }
    parsed = aa._parse_dataset({"data": [other_creator, _AA_ROW]})

    block = aa._block(
        _models(),
        {"status": "ok", "fetched_at": 1000.0, "models": parsed},
        key_source="settings",
        key_hint="•••-key",
    )

    assert block["scores"]["deepseek::deepseek-v4-flash"]["intelligence"] == 78.4


def test_fetch_follows_bounded_api_pagination():
    requested_urls = []

    class Response:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fetch(url, **kwargs):
        requested_urls.append(url)
        assert kwargs["headers"]["x-api-key"] == "owner-key"
        if url == aa.AA_API_URL:
            return Response({
                "data": [_AA_ROW],
                "pagination": {"page": 1, "total_pages": 2, "has_more": True},
            })
        assert url == f"{aa.AA_API_URL}?page=2"
        return Response({
            "data": [{**_AA_ROW, "name": "Qwen Max", "slug": "qwen-max"}],
            "pagination": {"page": 2, "total_pages": 2, "has_more": False},
        })

    rows = aa._fetch_dataset("owner-key", fetch)

    assert requested_urls == [aa.AA_API_URL, f"{aa.AA_API_URL}?page=2"]
    assert [row["aa_slug"] for row in rows] == ["deepseek-v4-flash", "qwen-max"]


def test_route_encrypts_owner_key_and_never_returns_plaintext(monkeypatch):
    import lib.http_client
    from routes.api_v1 import model_intelligence as route

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    document = empty_document()
    document["creators"] = [{"creator_id": "deepseek", "name": "DeepSeek"}]
    document["models"] = [{
        "creator_id": "deepseek",
        "model_id": "deepseek-v4-flash",
        "display_name": "DeepSeek V4 Flash",
        "capabilities": ["text", "thinking"],
        "context_window": 1_000_000,
        "quality_rank": 50,
        "list_pricing": {
            "input": 0.14,
            "output": 0.28,
            "currency": "USD",
            "unit": "per_million_tokens",
        },
    }]
    repository.compare_and_swap(boundary, document, expected_revision=0)
    monkeypatch.setattr(route, "_repository", lambda: repository)
    monkeypatch.setattr(route, "_legacy_config_key", lambda: "")
    monkeypatch.setattr(route, "_remove_legacy_config_key", lambda: None)
    monkeypatch.setattr(lib.http_client, "http_get", _fetcher("owner-aa-secret"))

    app = Quart(__name__, static_folder=None)
    app.config["TESTING"] = True

    @app.before_request
    def identity() -> None:
        context = local_admin_context()
        g.auth_ctx = context
        g.principal_context = principal_from_auth_context(
            context, allow_personal_owner=True)

    app.register_blueprint(route.api_v1_model_intelligence_bp)

    async def exercise():
        client = app.test_client()
        saved = await client.put(
            "/api/v1/model-intelligence/aa/key",
            json={"api_key": " owner-aa-secret "},
        )
        assert saved.status_code == 200
        saved_payload = await saved.get_json()
        assert saved_payload["aa"]["status"] == "ok"
        assert saved_payload["aa"]["key_source"] == "settings"
        assert saved_payload["aa"]["scores"][
            "deepseek::deepseek-v4-flash"
        ]["intelligence"] == 78.4
        assert "owner-aa-secret" not in str(saved_payload)

        loaded = await client.get("/api/v1/model-intelligence/aa")
        loaded_payload = await loaded.get_json()
        assert loaded_payload["aa"]["status"] == "ok"
        assert "owner-aa-secret" not in str(loaded_payload)

    asyncio.run(exercise())
    assert repository.resolve_secret(
        boundary, route._AA_SECRET_REFERENCE) == "owner-aa-secret"
