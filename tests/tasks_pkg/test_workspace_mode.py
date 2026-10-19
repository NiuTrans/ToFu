"""Workspace admission policy is local, explicit, and independent of publication."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from lib.project_mod.workspace_policy import WorkspacePolicyError, resolve_workspace_mode

pytestmark = pytest.mark.unit


def _git(root, *args):
    return subprocess.run(
        ['git', '-C', str(root), *args], check=True,
        capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def clean_policy_environment(tmp_path, monkeypatch):
    for name in tuple(os.environ):
        if name.upper().startswith('GIT_'):
            monkeypatch.delenv(name)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    monkeypatch.delenv('TOFU_WORKSPACE_MODE', raising=False)
    monkeypatch.delenv('TOFU_LINEAR_GIT_CHECKPOINT', raising=False)


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    _git(root, 'init', '-b', 'main')
    return root


def test_missing_policy_defaults_to_direct_without_changing_git_config(repository):
    before = (repository / '.git/config').read_bytes()

    assert resolve_workspace_mode(repository) == 'direct'

    assert (repository / '.git/config').read_bytes() == before


@pytest.mark.parametrize('source', ['environment', 'local-config'])
@pytest.mark.parametrize('raw,expected', [
    ('direct', 'direct'),
    ('isolated', 'isolated'),
    (' DiReCt\t', 'direct'),
    ('\tISOLATED ', 'isolated'),
])
def test_explicit_workspace_modes_are_normalized(repository, monkeypatch, source, raw, expected):
    if source == 'environment':
        monkeypatch.setenv('TOFU_WORKSPACE_MODE', raw)
    else:
        _git(repository, 'config', '--local', 'tofu.workspaceMode', raw)

    assert resolve_workspace_mode(repository) == expected


@pytest.mark.parametrize('override,local', [
    ('direct', 'isolated'),
    ('isolated', 'direct'),
    ('direct', 'unknown'),
    ('isolated', ''),
])
def test_process_override_precedes_local_config(repository, monkeypatch, override, local):
    _git(repository, 'config', '--local', 'tofu.workspaceMode', local)
    monkeypatch.setenv('TOFU_WORKSPACE_MODE', override)
    before = (repository / '.git/config').read_bytes()

    assert resolve_workspace_mode(repository) == override

    assert (repository / '.git/config').read_bytes() == before


@pytest.mark.parametrize('source', ['environment', 'local-config'])
@pytest.mark.parametrize('raw', ['', ' ', 'automatic', 'linear', 'true', 'direct\nisolated'])
def test_invalid_explicit_policy_fails_closed(repository, monkeypatch, source, raw):
    if source == 'environment':
        _git(repository, 'config', '--local', 'tofu.workspaceMode', 'isolated')
        monkeypatch.setenv('TOFU_WORKSPACE_MODE', raw)
    else:
        _git(repository, 'config', '--local', 'tofu.workspaceMode', raw)
    before = (repository / '.git/config').read_bytes()

    with pytest.raises(WorkspacePolicyError):
        resolve_workspace_mode(repository)

    assert (repository / '.git/config').read_bytes() == before


@pytest.mark.parametrize('second', ['direct', 'isolated'])
def test_duplicate_local_workspace_modes_fail_closed(repository, second):
    _git(repository, 'config', '--local', '--add', 'tofu.workspaceMode', 'direct')
    _git(repository, 'config', '--local', '--add', 'tofu.workspaceMode', second)
    before = (repository / '.git/config').read_bytes()

    with pytest.raises(WorkspacePolicyError):
        resolve_workspace_mode(repository)

    assert (repository / '.git/config').read_bytes() == before


def test_malformed_local_git_config_does_not_fall_back_to_direct(repository):
    config = repository / '.git/config'
    config.write_text('[tofu\nworkspaceMode = isolated\n')
    before = config.read_bytes()

    with pytest.raises(WorkspacePolicyError):
        resolve_workspace_mode(repository)

    assert config.read_bytes() == before


def test_global_git_policy_cannot_opt_a_project_into_isolation(repository, monkeypatch):
    monkeypatch.delenv('GIT_CONFIG_GLOBAL')
    global_config = Path(os.environ['HOME']) / '.gitconfig'
    global_config.write_text('[tofu]\n\tworkspaceMode = isolated\n')
    assert _git(repository, 'config', '--global', '--get', 'tofu.workspaceMode') == 'isolated'

    assert resolve_workspace_mode(repository) == 'direct'

    _git(repository, 'config', '--local', 'tofu.workspaceMode', 'direct')
    assert resolve_workspace_mode(repository) == 'direct'


@pytest.mark.parametrize('local', [None, 'direct', 'isolated'])
@pytest.mark.parametrize('linear', ['true', 'false', 'not-a-valid-publication-mode'])
def test_linear_publication_setting_does_not_select_workspace_mode(
        repository, monkeypatch, local, linear):
    if local is not None:
        _git(repository, 'config', '--local', 'tofu.workspaceMode', local)
    _git(repository, 'config', '--local', 'tofu.linearCheckpoint', linear)
    monkeypatch.setenv('TOFU_LINEAR_GIT_CHECKPOINT', linear)

    assert resolve_workspace_mode(repository) == (local or 'direct')


@pytest.mark.parametrize('local', [None, 'direct', 'isolated'])
@pytest.mark.parametrize('injection', ['repository', 'config-file', 'config-count', 'config-parameters'])
def test_ambient_git_routing_and_config_cannot_override_project_policy(
        repository, tmp_path, monkeypatch, local, injection):
    if local is not None:
        _git(repository, 'config', '--local', 'tofu.workspaceMode', local)
    expected = local or 'direct'
    foreign_mode = 'isolated' if expected == 'direct' else 'direct'
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    _git(foreign, 'init', '-b', 'main')
    _git(foreign, 'config', '--local', 'tofu.workspaceMode', foreign_mode)
    before = (repository / '.git/config').read_bytes()
    foreign_before = (foreign / '.git/config').read_bytes()
    if injection == 'repository':
        monkeypatch.setenv('GIT_DIR', str(foreign / '.git'))
        monkeypatch.setenv('GIT_WORK_TREE', str(foreign))
        monkeypatch.setenv('GIT_COMMON_DIR', str(foreign / '.git'))
    elif injection == 'config-file':
        monkeypatch.setenv('GIT_CONFIG', str(foreign / '.git/config'))
        monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(foreign / '.git/config'))
        monkeypatch.setenv('GIT_CONFIG_SYSTEM', str(foreign / '.git/config'))
    elif injection == 'config-count':
        monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
        monkeypatch.setenv('GIT_CONFIG_KEY_0', 'tofu.workspaceMode')
        monkeypatch.setenv('GIT_CONFIG_VALUE_0', foreign_mode)
    else:
        monkeypatch.setenv('GIT_CONFIG_PARAMETERS', f"'tofu.workspaceMode={foreign_mode}'")

    assert resolve_workspace_mode(repository) == expected

    assert (repository / '.git/config').read_bytes() == before
    assert (foreign / '.git/config').read_bytes() == foreign_before


def test_subdirectory_reads_its_own_repository_local_policy(repository):
    _git(repository, 'config', '--local', 'tofu.workspaceMode', 'isolated')
    nested = repository / 'src' / 'package'
    nested.mkdir(parents=True)

    assert resolve_workspace_mode(nested) == 'isolated'
