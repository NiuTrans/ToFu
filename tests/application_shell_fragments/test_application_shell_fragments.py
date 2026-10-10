"""Closed-system contract for frontend application-shell fragments."""

from __future__ import annotations

import asyncio
import gzip

import pytest
from quart import Quart

import lib.application_shell_fragments as fragments


pytestmark = pytest.mark.unit


def _served_index_html() -> str:
    """Return the shell through the production HTTP composition path."""
    import routes.common as common

    async def _request() -> str:
        app = Quart(__name__, static_folder=None)
        app.register_blueprint(common.common_bp)
        response = await app.test_client().get("/")
        assert response.status_code == 200
        return await response.get_data(as_text=True)

    return asyncio.run(_request())


def test_fragment_markers_and_files_are_a_closed_set():
    # index_page returns 503 if the real index marker set and authored fragment
    # set differ or if a marker is duplicated. A successful production render
    # is therefore the executable closed-set contract.
    assembled = _served_index_html()
    assert fragments.list_fragment_names()
    assert not fragments.find_markers(assembled)


def test_assembled_shell_has_one_copy_of_each_agent_mode_control():
    assembled = _served_index_html()
    assert not fragments.find_markers(assembled)
    for element_id in (
        "submenuAgentMode",
        "agentModeStandard",
        "planModeToggle",
        "autopilotToggle",
        "mobileAgentModeStandard",
        "mobilePlanMode",
        "mobileAutopilot",
    ):
        assert assembled.count(f'id="{element_id}"') == 1
    assembled_bytes = assembled.encode("utf-8")
    # The exact-one assertions catch duplicated controls; retain a compact
    # delivery measurement as a second signal for accidental shell bloat.
    assert len(gzip.compress(assembled_bytes, compresslevel=9, mtime=0)) \
        < len(assembled_bytes) // 2


def test_fragment_signature_changes_with_fragment_content(tmp_path, monkeypatch):
    fragment_dir = tmp_path / "fragments"
    fragment_dir.mkdir()
    fragment = fragment_dir / "sample.html"
    fragment.write_text("<div>first</div>", encoding="utf-8")
    monkeypatch.setattr(fragments, "FRAGMENTS_DIR", fragment_dir)
    before = fragments.fragments_signature()
    fragment.write_text("<div>second and longer</div>", encoding="utf-8")
    after = fragments.fragments_signature()
    assert before != after


def test_fragment_parity_drift_fails_closed(tmp_path, monkeypatch):
    fragment_dir = tmp_path / "fragments"
    fragment_dir.mkdir()
    monkeypatch.setattr(fragments, "FRAGMENTS_DIR", fragment_dir)
    with pytest.raises(ValueError, match="missing_files"):
        fragments.inject_fragments(fragments.marker_for("missing"))


def test_index_cache_owns_fragment_signature_and_injection(monkeypatch):
    import routes.common as common

    for key in common._bundled_index_cache:
        common._bundled_index_cache[key] = 0 if key == "mtime" else None

    revision = ["fragments-v1"]
    signature_calls = []
    injection_calls = []
    real_inject = fragments.inject_fragments

    def _signature():
        signature_calls.append(revision[0])
        return revision[0]

    def _inject(html):
        injection_calls.append(revision[0])
        return real_inject(html)

    monkeypatch.setattr(
        common, "_application_shell_fragments_signature", _signature
    )
    monkeypatch.setattr(common, "_inject_application_shell_fragments", _inject)

    async def _exercise_cache():
        app = Quart(__name__, static_folder=None)
        app.register_blueprint(common.common_bp)
        client = app.test_client()
        first = await client.get("/")
        second = await client.get("/")
        revision[0] = "fragments-v2"
        third = await client.get("/")
        return first, second, third

    responses = asyncio.run(_exercise_cache())
    assert [response.status_code for response in responses] == [200, 200, 200]
    assert signature_calls == ["fragments-v1", "fragments-v1", "fragments-v2"]
    assert injection_calls == ["fragments-v1", "fragments-v2"]
