"""Smoke tests for every current Tofu plugin entry-point surface."""

import pytest


pytest.importorskip("lib", reason="Tofu host (core) not installed")


def test_storage_entry_point_exposes_data_only_manifest():
    from tofu_trading.storage_manifest import MANIFEST

    assert not callable(MANIFEST)
    assert MANIFEST["namespace"] == "tofu.trading"
    actions = {operation["action"] for operation in MANIFEST["operations"]}
    assert {"get", "list", "put", "batch", "legacy_scan"} <= actions


def test_blueprint_registrar_returns_expected_blueprints():
    import tofu_trading.web as web

    blueprints = web.register()
    names = [blueprint.name for blueprint in blueprints]
    assert len(blueprints) == 9
    assert "api_v1_trading_holdings_bp" in names
    assert "api_v1_trading_simulator_bp" in names
    assert "trading_pages" in names


def test_task_runtimes_hook_exposes_trading_sim():
    import tofu_trading.web as web

    kinds = [runtime.kind for runtime in web.get_task_runtimes()]
    assert "trading-sim" in kinds


def test_flags_registrar_noops_without_host_registry():
    import tofu_trading.flags as flags

    flags.register(None)
    captured = {}
    flags.register(lambda **values: captured.update(values))
    assert captured["json_key"] == "trading_enabled"
    assert captured["needs_restart"] is False
