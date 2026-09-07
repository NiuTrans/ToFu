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
