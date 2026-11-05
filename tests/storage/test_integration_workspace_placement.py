"""Placement of managed task checkouts: data root by default, local by opt-in."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib import integration_control as control
from serverctl_pkg import local_placement


pytestmark = pytest.mark.unit


@pytest.fixture
def roots(tmp_path, monkeypatch):
    data = tmp_path / 'data'
    local = tmp_path / 'local-state'
    monkeypatch.setattr(control, 'data_root', lambda: str(data))
    monkeypatch.setattr(control, '_PLACED_WORKSPACE_ROOTS', {})
    for name in ('TOFU_INTEGRATION_WORKSPACE_DIR', 'TOFU_LOCAL_WORKSPACES', 'XDG_STATE_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('TOFU_LOCAL_DIR', str(local))
    return data, local


def _select(monkeypatch, root: Path):
    decisions = []

    def choose(source, purpose, **_kwargs):
        decisions.append((Path(source), purpose))
        return local_placement.Placement(True, purpose, 'on', 'selected', root=str(root))

    monkeypatch.setattr(local_placement, 'choose_local_root', choose)
    return decisions


def test_checkouts_stay_under_the_data_root_by_default(roots):
    data, local = roots

    assert control._workspace_root() == data / 'integration' / 'worktrees'
    assert not local.exists()


def test_opted_in_checkouts_use_a_per_data_root_local_namespace(roots, monkeypatch):
    data, local = roots
    monkeypatch.setenv('TOFU_LOCAL_WORKSPACES', 'on')
    placed = local / 'workspaces'
    decisions = _select(monkeypatch, placed)

    first = control._workspace_root()
    second = control._workspace_root()

    assert first == second and first.parent == placed and first.is_dir()
    assert decisions == [(data / 'integration' / 'worktrees',
                          local_placement.PURPOSE_WORKSPACES)], 'decided once per process'
    monkeypatch.setattr(control, 'data_root', lambda: str(data.parent / 'other-data'))
    assert control._workspace_root().parent == placed and control._workspace_root() != first


def test_removal_accepts_every_controlled_root_and_nothing_else(roots, monkeypatch, caplog):
    data, local = roots
    controlled = control._controlled_workspace_roots()
    namespaced = control._local_workspace_root(local / 'workspaces')

    assert (data / 'integration' / 'worktrees').resolve() in controlled
    assert namespaced.resolve() in controlled, 'a later placement change never strands a checkout'

    stray = data.parent / 'user-project'
    stray.mkdir()
    control._remove_controlled_worktree_owned(data, stray)
    assert stray.exists()
    assert 'refused to remove uncontrolled path' in caplog.text
