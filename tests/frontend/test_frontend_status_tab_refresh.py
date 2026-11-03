"""Project Brain refreshes its complete projection on entry and push."""
import json
import re
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
BRAIN_JS = REPO / "frontend/src/runtime/sections/project-brain.js"


def _extract_fn(src: str, name: str) -> str:
    match = re.search(r"(?:async\s+)?function %s\s*\(" % re.escape(name), src)
    assert match, f"{name} not found"
    start = src.index("{", match.start())
    depth = 0
    for index in range(start, len(src)):
        if src[index] == "{":
            depth += 1
        elif src[index] == "}":
            depth -= 1
            if depth == 0:
                return src[match.start():index + 1]
    raise AssertionError(f"unbalanced braces extracting {name}")


def _source(name: str) -> str:
    return _extract_fn(BRAIN_JS.read_text(encoding="utf-8"), name)


def test_selection_does_not_refresh_unrelated_panels():
    source = _source("_selectTab")
    assert "refreshAll" not in source
    assert "ProjectBrainIntegration.refreshIntegration" in source


def test_push_refreshes_all_project_evidence():
    source = _source("_subscribe")
    assert "refreshAll(path);" in source
    assert "event.type === 'project_brain_changed' || event.type === 'path_overlap'" in source


def test_full_refresh_includes_status_and_watch():
    source = _source("refreshAll")
    for endpoint in ("api.board(path)", "api.feed(path, 0)", "api.charter(path)",
                     "api.brainCheckers(path)", "api.brainStatus(path)",
                     "api.brainWatchList(path)"):
        assert endpoint in source
    assert "renderStatus(values[4] || {}, values[5] || {});" in source


def test_neuter_dropping_status_refresh_is_detected():
    source = _source("refreshAll")
    neutered = re.sub(r"\s*renderStatus\(values\[4\] \|\| \{\}, values\[5\] \|\| \{\}\);", "", source, count=1)
    assert neutered != source
    assert "renderStatus" not in neutered
