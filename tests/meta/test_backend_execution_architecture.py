"""Architecture gates for the single backend execution command path."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit


def test_routes_cannot_import_model_execution_or_reserve_provider_slots(tmp_path):
    from scripts.check_architecture import route_llm_bypass_violations

    route = tmp_path / "bad_route.py"
    route.write_text(
        "from lib.llm_dispatch import async_dispatch_stream\n"
        "def endpoint():\n"
        "    return get_dispatcher().pick_and_reserve(capability='text')\n",
        encoding="utf-8",
    )
    violations = route_llm_bypass_violations(
        [route.resolve()], routes_root=tmp_path,
    )
    assert len(violations) == 2
    assert "LLM dispatch directly" in violations[0]
    assert "reserve provider slots directly" in violations[1]


def test_current_routes_have_no_model_execution_bypass():
    from scripts.check_architecture import ROOT, route_llm_bypass_violations

    paths = sorted((ROOT / "routes").rglob("*.py"))
    assert route_llm_bypass_violations(paths) == []


def test_authority_mutations_use_public_dispatcher_lifecycle():
    """HTTP, local_serve, and template supply reset the public singleton."""
    from scripts.check_architecture import ROOT

    owners = (
        ROOT / "routes/api_v1/providers.py",
        ROOT / "lib/local_serve/_register.py",
        ROOT / "lib/model_routing/template_supply.py",
    )
    required = "from lib.llm_dispatch import reset_dispatcher"
    forbidden = (
        "from lib.llm_dispatch.health_local import _rebuild_dispatcher_slots"
    )
    for path in owners:
        source = path.read_text(encoding="utf-8")
        assert required in source, path
        assert forbidden not in source, path
