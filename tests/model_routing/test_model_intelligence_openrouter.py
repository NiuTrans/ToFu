"""OpenRouter pricing is a policy-guarded projection, never a silent source."""

from __future__ import annotations

import asyncio

import pytest
from quart import Quart, g

from lib.api_keys import local_admin_context
from lib.identity import principal_from_auth_context
from lib.model_catalog import openrouter
from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary, empty_document


pytestmark = pytest.mark.unit

_OR_PAYLOAD = {
    "data": [
        {
            "id": "thinkingmachines/inkling",
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.00000405",
                "input_cache_read": "0.00000017",
            },
        },
        {
            "id": "thinkingmachines/inkling:free",
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "~z-ai/glm-flash-latest",
            "alias_target": {"slug": "z-ai/glm-5.3-flash"},
            "pricing": {"prompt": "0.000000075", "completion": "0.00000025"},
        },
        {
            "id": "acme/toy-model",
            "pricing": {"prompt": "0", "completion": "0"},
        },
        {
            "id": "acme/toy-pro",
            "pricing": {"prompt": "0.000002", "completion": "0.000008"},
        },
        {
            "id": "coveredco/covered-alpha",
            "pricing": {"prompt": "0.00001", "completion": "0.00005"},
        },
        {
            "id": "coveredco/covered-beta",
            "pricing": {"prompt": "0.000003", "completion": "0.000009"},
        },
    ]
}


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(openrouter, "_memo", None)
    monkeypatch.setattr(openrouter, "_background", {
        "running": False, "last_attempt": 0.0, "thread": None,
    })
    monkeypatch.setattr(
        openrouter, "_cache_path",
        lambda: str(tmp_path / "openrouter" / "models.json"))


def _fetcher(_url, **kwargs):
    assert kwargs["timeout"] > 0

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return _OR_PAYLOAD

    return Response()


def _dataset(now=1000.0):
    return openrouter._dataset(force=True, fetcher=_fetcher, now=now)


def _official_lookup(model_id):
    return {"input": 1.0} if model_id == "covered-alpha" else None


def _models():
    return [
        {
            "creator_id": "thinkingmachines",
            "model_id": "thinkingmachines/thinkingmachines/inkling",
            "display_name": "Inkling",
        },
        {"creator_id": "acme", "model_id": "toy-model", "display_name": "Toy"},
        {
            "creator_id": "acme",
            "model_id": "toy-pro",
            "display_name": "Toy Pro",
            "list_pricing": {
                "input": 2.0, "output": 8.0,
                "currency": "USD", "unit": "per_million_tokens",
            },
        },
        {
            "creator_id": "coveredco",
            "model_id": "covered-alpha",
            "list_pricing": {
                "input": 1.0, "output": 1.0,
                "currency": "USD", "unit": "per_million_tokens",
            },
        },
        {"creator_id": "coveredco", "model_id": "covered-beta"},
        {"creator_id": "ghost", "model_id": "ghost-1"},
    ]


def test_parse_skips_variants_and_converts_per_token_strings():
    rows = openrouter._parse_dataset(_OR_PAYLOAD)
    by_id = {row["or_id"]: row for row in rows}

    assert "thinkingmachines/inkling:free" not in by_id
    assert "~z-ai/glm-flash-latest" not in by_id
    inkling = by_id["thinkingmachines/inkling"]
    assert inkling["input"] == 1.0
    assert inkling["output"] == 4.05
    assert inkling["cache_read"] == 0.17
    assert inkling["positive"] is True
    assert by_id["acme/toy-model"]["positive"] is False


def test_unpriced_uncovered_model_is_fillable_via_leaf_fold():
    block = openrouter._block(
        _models(), _dataset(), official_lookup=_official_lookup)

    fill = block["fillable"][
        "thinkingmachines::thinkingmachines/thinkingmachines/inkling"]
    assert fill["or_id"] == "thinkingmachines/inkling"
    assert fill["pricing"] == {
        "input": 1.0,
        "output": 4.05,
        "currency": "USD",
        "unit": "per_million_tokens",
        "cache_read": 0.17,
    }
    assert "provider" not in str(block).lower()


def test_official_covered_creator_is_reported_never_filled():
    block = openrouter._block(
        _models(), _dataset(), official_lookup=_official_lookup)

    assert block["official_covered"] == ["coveredco::covered-beta"]
    assert "coveredco::covered-beta" not in block["fillable"]


def test_all_zero_openrouter_row_never_becomes_a_fill():
    block = openrouter._block(
        _models(), _dataset(), official_lookup=_official_lookup)

    assert block["openrouter_free"] == ["acme::toy-model"]
    assert "acme::toy-model" not in block["fillable"]


def test_priced_models_stay_authoritative_and_report_drift():
    block = openrouter._block(
        _models(), _dataset(), official_lookup=_official_lookup)

    # toy-pro's list price equals its OpenRouter row — no drift.
    assert "acme::toy-pro" not in block["drift"]
    # covered-alpha lists $1/$1 against OpenRouter's $10/$50 — cross-check only.
    drift = block["drift"]["coveredco::covered-alpha"]
    assert drift["or_id"] == "coveredco/covered-alpha"
    assert drift["list_pricing"]["input"] == 1.0
    assert drift["openrouter"]["input"] == 10.0
    assert "coveredco::covered-alpha" not in block["fillable"]


def test_unmatched_unpriced_models_are_listed():
    block = openrouter._block(
        _models(), _dataset(), official_lookup=_official_lookup)

    assert block["unmatched"] == ["ghost::ghost-1"]


def test_fill_document_skips_priced_rows_and_zero_fills():
    block = openrouter._block(
        _models(), _dataset(), official_lookup=_official_lookup)
    fillable = dict(block["fillable"])
    fillable["ghost::ghost-1"] = {
        "or_id": "ghost/ghost-1",
        "pricing": {"input": 0.0, "output": 0.0},
    }
    document = {
        "models": [
            {
                "creator_id": "thinkingmachines",
                "model_id": "thinkingmachines/thinkingmachines/inkling",
            },
            {
                "creator_id": "acme",
                "model_id": "toy-pro",
                "list_pricing": {
                    "input": 2.0, "output": 8.0,
                    "currency": "USD", "unit": "per_million_tokens",
                },
            },
            {"creator_id": "ghost", "model_id": "ghost-1"},
        ],
    }

    applied = openrouter.fill_document_list_pricing(document, fillable)

    assert applied == ["thinkingmachines::thinkingmachines/thinkingmachines/inkling"]
    assert document["models"][0]["list_pricing"] == {
        "input": 1.0,
        "output": 4.05,
        "currency": "USD",
        "unit": "per_million_tokens",
        "cache_read": 0.17,
    }
    assert document["models"][1]["list_pricing"]["input"] == 2.0
    assert "list_pricing" not in document["models"][2]


def test_route_refresh_fills_authority_and_get_reports(monkeypatch):
    import lib.pricing
    import lib.http_transport.client
    from routes.api_v1 import model_intelligence as route

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    document = empty_document()
    document["creators"] = [
        {"creator_id": "thinkingmachines", "name": "Thinking Machines"}]
    document["models"] = [{
        "creator_id": "thinkingmachines",
        "model_id": "thinkingmachines/thinkingmachines/inkling",
        "display_name": "Inkling",
        "capabilities": ["text"],
        "context_window": 262_144,
        "quality_rank": 32.2,
    }]
    repository.compare_and_swap(boundary, document, expected_revision=0)
    monkeypatch.setattr(route, "_repository", lambda: repository)
    monkeypatch.setattr(lib.http_transport.client, "http_get", _fetcher)
    monkeypatch.setattr(lib.pricing, "pricing_for_model", lambda _model_id: None)

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
        refreshed = await client.post(
            "/api/v1/model-intelligence/openrouter-pricing/refresh")
        assert refreshed.status_code == 200
        payload = await refreshed.get_json()
        assert payload["openrouter_pricing"]["status"] == "ok"
        assert payload["applied"] == [
            "thinkingmachines::thinkingmachines/thinkingmachines/inkling"]
        assert payload["revision"] == 2

        loaded = await client.get("/api/v1/model-intelligence/openrouter-pricing")
        assert loaded.status_code == 200
        report = (await loaded.get_json())["openrouter_pricing"]
        assert report["summary"]["priced"] == 1
        assert report["fillable"] == {}

    asyncio.run(exercise())
    models = repository.get(boundary).document["models"]
    assert models[0]["list_pricing"] == {
        "input": 1.0,
        "output": 4.05,
        "currency": "USD",
        "unit": "per_million_tokens",
        "cache_read": 0.17,
    }
