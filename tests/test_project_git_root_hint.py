"""tests/test_project_git_root_hint.py — find_git_root walk-up semantics.

A user who picks ``repo/sub`` as a project almost always means ``repo`` —
the modal probes the nearest ENCLOSING ``.git`` marker (dir or gitfile, the
latter covering linked worktrees/submodules) and offers the real root.
These pins fix the walk-up contract the hint endpoint relies on.
"""

from __future__ import annotations

import pytest

from lib.project_mod.scanner import find_git_root

pytestmark = pytest.mark.unit


def test_subdirectory_resolves_enclosing_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert find_git_root(str(sub)) == str(repo)


def test_git_file_marker_counts(tmp_path):
    # Linked worktrees / submodules carry a .git FILE (gitdir pointer), not
    # a directory — the probe must accept both marker shapes.
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere", encoding="utf-8")
    assert find_git_root(str(worktree)) == str(worktree)


def test_chosen_dir_that_is_the_root_reports_itself(tmp_path):
    (tmp_path / ".git").mkdir()
    assert find_git_root(str(tmp_path)) == str(tmp_path)


def test_no_marker_anywhere_returns_none(tmp_path):
    sub = tmp_path / "plain" / "sub"
    sub.mkdir(parents=True)
    assert find_git_root(str(sub)) is None


def test_non_directory_returns_none(tmp_path):
    assert find_git_root(str(tmp_path / "missing")) is None


def test_nested_repo_reports_nearest_root(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "libs" / "inner"
    (outer / ".git").mkdir(parents=True)
    (inner / ".git").mkdir(parents=True)
    sub = inner / "src"
    sub.mkdir()
    assert find_git_root(str(sub)) == str(inner)
