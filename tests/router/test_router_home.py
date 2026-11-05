"""The router home is an owner-scoped Tofu-created Git repository (docs/ROUTER.md R1)."""
from __future__ import annotations

import os
import subprocess

import pytest

from lib.router.home import is_router_home, router_home_location, router_home_path

pytestmark = pytest.mark.unit


def _git(root, *args):
    result = subprocess.run(['git', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / 'data' / 'router'
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(path))
    return path


def test_location_is_owner_scoped_and_creates_nothing(root):
    first, second = router_home_location(7), router_home_location(8)
    assert first != second and first.startswith(str(root.resolve()))
    assert not os.path.exists(first)
    assert is_router_home(7, first + '/') and not is_router_home(8, first)
    assert not is_router_home(7, '') and not is_router_home(7, os.path.dirname(first))
    with pytest.raises((TypeError, ValueError)):
        router_home_location(0)


def test_creation_is_an_idempotent_committed_repository(root):
    home = router_home_path(7)
    assert home == router_home_location(7)
    head = _git(home, 'rev-parse', 'HEAD')
    tracked = set(_git(home, 'ls-files').splitlines())
    assert {'README.md', '.gitignore'} <= tracked
    assert _git(home, 'status', '--porcelain') == ''
    assert _git(home, 'rev-parse', '--show-toplevel') == home
    assert router_home_path(7) == home
    assert _git(home, 'rev-parse', 'HEAD') == head


def test_home_inside_a_source_checkout_is_its_own_repository(tmp_path, monkeypatch):
    outer = tmp_path / 'checkout'
    outer.mkdir()
    _git(outer, 'init', '-q')
    (outer / 'tracked.txt').write_text('outer\n')
    _git(outer, 'add', 'tracked.txt')
    _git(outer, '-c', 'user.name=T', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'outer')
    monkeypatch.setattr('lib.router.home.router_home_root', lambda: str(outer / 'data' / 'router'))
    home = router_home_path(3)
    assert _git(home, 'rev-parse', '--show-toplevel') == home
    assert _git(outer, 'status', '--porcelain') in ('', '?? data/')


def test_existing_guards_accept_the_home_as_a_worker_project_root(root):
    from lib.tasks_pkg.workspace_delivery import isolated_workspace_refusal
    home = router_home_path(7)
    assert isolated_workspace_refusal(home) is None
