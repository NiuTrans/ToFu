"""Executable specification for lib.model_catalog.auto_update.

The scheduled pass must refresh the registry, OpenRouter pricing fills, and
Artificial Analysis scores in one run, fail-closed per phase, and never fetch
scores without an owner key.
"""

from __future__ import annotations

import pytest

from lib.identity import PrincipalContext
from lib.model_catalog import auto_update
from lib.model_routing import OwnerBoundary, empty_document
from lib.model_routing.repository import InMemoryModelRoutingRepository

pytestmark = pytest.mark.unit


def _principal(*, scopes=frozenset({"providers"})):
    return PrincipalContext.system(
        subject_id="test", owner_user_id=23, scopes=set(scopes))


def _boundary():
    return OwnerBoundary.create(23)


def _document_with_unpriced_model():
    document = empty_document()
    document["creators"] = [{"creator_id": "acme", "name": "Acme"}]
    document["models"] = [{
        "creator_id": "acme",
        "model_id": "acme-1",
        "display_name": "Acme 1",
        "capabilities": ["text"],
        "context_window": 32768,
        "quality_rank": 50,
    }]
    return document


def _patch_catalog_sync(monkeypatch):
    import lib.model_routing.world_catalog as world_catalog

    calls = []
    monkeypatch.setattr(
        world_catalog,
        "sync_world_model_catalog",
        lambda **kw: calls.append(kw) or {
            "added": 1, "creators_added": 0, "pruned": 0},
    )
    return calls


def test_missing_scope_denied_before_storage():
    with pytest.raises(PermissionError):
        auto_update.run_model_intelligence_update(
            principal=_principal(scopes={"scheduler:run"}))


def test_one_pass_syncs_fills_and_scores(monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    repository.compare_and_swap(
        boundary, _document_with_unpriced_model(), expected_revision=0)
    catalog_calls = _patch_catalog_sync(monkeypatch)

    monkeypatch.setattr(
        auto_update, "refresh_pricing_block",
        lambda models: {
            "status": "ok",
            "fillable": {"acme::acme-1": {
                "or_id": "acme/acme-1",
                "pricing": {"input": 1.0, "output": 2.0, "currency": "USD",
                            "unit": "per_million_tokens"},
            }},
            "summary": {"models": len(models), "priced": 0, "fillable": 1},
        })
    monkeypatch.setattr(
        auto_update, "resolve_effective_key",
        lambda repository, boundary: ("owner-key", "settings", "…key"))
    scores_calls = []

    def fake_refresh_scores(models, *, api_key, key_source, key_hint):
        scores_calls.append({"models": len(models), "api_key": api_key})
        return {"status": "ok", "scores": {"acme::acme-1": {"intelligence": 42.0}}}

    monkeypatch.setattr(auto_update, "refresh_scores", fake_refresh_scores)

    summary = auto_update.run_model_intelligence_update(
        principal=_principal(), repository=repository)

    assert summary["ok"] is True
    assert catalog_calls and catalog_calls[0]["repository"] is repository
    assert summary["catalog"] == {"added": 1, "creators_added": 0, "pruned": 0}
    assert summary["pricing"]["status"] == "ok"
    assert summary["pricing"]["applied"] == 1
    assert summary["scores"] == {
        "status": "ok", "key_source": "settings", "scores": 1}
    assert scores_calls == [{"models": 1, "api_key": "owner-key"}]

    priced = repository.get(boundary).document["models"][0]["list_pricing"]
    assert priced["input"] == 1.0 and priced["output"] == 2.0


def test_scores_phase_never_fetches_without_key(monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = _boundary()
    repository.compare_and_swap(
        boundary, _document_with_unpriced_model(), expected_revision=0)
    _patch_catalog_sync(monkeypatch)
    monkeypatch.setattr(
        auto_update, "refresh_pricing_block",
        lambda models: {"status": "ok", "fillable": {}, "summary": {}})
    monkeypatch.setattr(
        auto_update, "resolve_effective_key",
        lambda repository, boundary: ("", None, ""))
    monkeypatch.setattr(
        auto_update, "refresh_scores",
        lambda *a, **kw: pytest.fail("must not fetch scores without a key"))

    summary = auto_update.run_model_intelligence_update(
        principal=_principal(), repository=repository)

    assert summary["scores"] == {"status": "no_key", "scores": 0}
    assert summary["pricing"]["status"] == "ok"


def test_pricing_failure_never_masks_scores(monkeypatch):
    repository = InMemoryModelRoutingRepository()
    _patch_catalog_sync(monkeypatch)

    def broken_pricing(models):
        raise RuntimeError("openrouter down")

    monkeypatch.setattr(auto_update, "refresh_pricing_block", broken_pricing)
    monkeypatch.setattr(
        auto_update, "resolve_effective_key",
        lambda repository, boundary: ("owner-key", "env", "…key"))
    monkeypatch.setattr(
        auto_update, "refresh_scores",
        lambda models, **kw: {"status": "ok", "scores": {}})

    summary = auto_update.run_model_intelligence_update(
        principal=_principal(), repository=repository)

    assert summary["ok"] is True
    assert summary["pricing"]["status"] == "error"
    assert "openrouter down" in summary["pricing"]["error"]
    assert summary["scores"]["status"] == "ok"


def test_summary_is_json_serializable_and_bounded(monkeypatch):
    import json

    repository = InMemoryModelRoutingRepository()
    _patch_catalog_sync(monkeypatch)
    fillable = {
        f"acme::m{index}": {
            "or_id": f"acme/m{index}",
            "pricing": {"input": 1.0, "output": 1.0, "currency": "USD",
                        "unit": "per_million_tokens"},
        }
        for index in range(100)
    }
    monkeypatch.setattr(
        auto_update, "refresh_pricing_block",
        lambda models: {"status": "ok", "fillable": fillable, "summary": {}})
    monkeypatch.setattr(
        auto_update, "resolve_effective_key",
        lambda repository, boundary: ("", None, ""))

    summary = auto_update.run_model_intelligence_update(
        principal=_principal(), repository=repository)

    assert len(summary["pricing"]["applied_sample"]) <= 32
    json.dumps(summary, sort_keys=True)


def test_template_feed_precedes_registry_and_failure_keeps_other_phases(monkeypatch):
    import lib.provider.provider_template_feed as feed
    import lib.model_routing.world_catalog as world_catalog

    events = []
    def unavailable(**kw):
        assert 0 < kw['max_runtime'] <= auto_update.MODEL_INTELLIGENCE_UPDATE_MAX_RUNTIME
        events.append('templates')
        raise ConnectionError('public catalog down')
    monkeypatch.setattr(feed, 'refresh_now', unavailable)
    monkeypatch.setattr(world_catalog, 'sync_world_model_catalog',
                        lambda **kw: events.append('registry') or {})
    monkeypatch.setattr(auto_update, 'refresh_pricing_block',
                        lambda models: {'status': 'ok', 'fillable': {}})
    monkeypatch.setattr(auto_update, 'resolve_effective_key',
                        lambda *a: ('', None, ''))
    summary = auto_update.run_model_intelligence_update(
        principal=_principal(), repository=InMemoryModelRoutingRepository())
    assert events == ['templates', 'registry']
    assert summary['templates'] == {'ok': False, 'status': 'unavailable'}
    assert summary['pricing']['status'] == 'ok'
    assert summary['scores']['status'] == 'no_key'


def test_daily_schedule_defaults_and_async_dispatch(monkeypatch):
    from lib.scheduler.manager import ScheduledTaskManager

    manager = ScheduledTaskManager()
    captured = {}
    monkeypatch.setattr(
        manager, "_ensure_default_task",
        lambda **kw: (captured.update(kw) or ({"id": "daily"}, True)))
    manager._ensure_default_model_intelligence_task()
    assert captured["schedule"] == "30 8 * * *"
    assert captured["task_type"] == "model_intelligence_daily"
    assert captured["enabled"] is True
    assert captured["reconcile_enabled"] is False
    assert captured["max_runtime"] == (
        auto_update.MODEL_INTELLIGENCE_UPDATE_MAX_RUNTIME)

    called = []
    monkeypatch.setattr(
        manager, "_dispatch_maintenance_task", lambda task: called.append(task))
    manager._dispatch_claimed_task(
        {"id": "daily", "task_type": "model_intelligence_daily"})
    assert len(called) == 1
