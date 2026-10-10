"""Focused contract for the recent-projects existence probe.

``GET /api/v1/project/recent`` stamps ``exists`` per stored path so the
modal can badge renamed/moved directories and offer relink. The probe must
stay bounded on a slow network filesystem: concurrent stats with an
overall deadline, and an optimistic ``True`` for probes that do not
answer in time (a wrong ``False`` would offer relink for a directory that
is merely slow to stat).
"""

from __future__ import annotations

import os
import time

import pytest


pytestmark = pytest.mark.unit


def test_annotate_recent_exists_stamps_real_state(tmp_path):
    from routes.api_v1.project import _annotate_recent_exists

    projects = [
        {"path": str(tmp_path)},
        {"path": str(tmp_path / "missing")},
        {"path": ""},
    ]
    _annotate_recent_exists(projects)
    assert [item["exists"] for item in projects] == [True, False, False]


def test_annotate_recent_exists_deadline_stays_optimistic(
        tmp_path, monkeypatch):
    from routes.api_v1 import project as project_routes

    hang = str(tmp_path / "slow-mount")
    real_isdir = os.path.isdir

    def _stat(path):
        if os.path.abspath(path) == os.path.abspath(hang):
            time.sleep(3)
        return real_isdir(path)

    monkeypatch.setattr(os.path, "isdir", _stat)
    monkeypatch.setattr(project_routes, "_RECENT_EXISTS_DEADLINE_S", 0.2)
    projects = [{"path": hang}, {"path": str(tmp_path)}]
    started = time.monotonic()
    project_routes._annotate_recent_exists(projects)
    elapsed = time.monotonic() - started
    # The hung probe keeps its optimistic True; the responsive one is
    # stamped honestly, and the whole pass stays inside the deadline.
    assert [item["exists"] for item in projects] == [True, True]
    assert elapsed < 2.0
