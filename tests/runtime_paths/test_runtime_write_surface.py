"""Writable runtime state derives from the runtime base, never the source tree.

``runtime_path_policy.get_runtime_base()`` is the one startup decision for
data, logs and uploads ("Writable path selection" in
docs/modules/infra_runtime.md). Reconstructible state follows it as well: the
storage-boundary scan cache, the tool caches that MCP launchers and commands
share, persisted oversized tool results, the desktop diagnostics inbox and the
compat temporary directory.

* Normal server: the base is the checkout, so each path stays the
  ``<checkout>/data/...`` it has always been.
* Android local kernel: the payload is the source tree, immutable and replaced
  by every update; ``--data-dir`` selects the base (docs/ANDROID_KERNELS.md).
* Fresh source checkout: the base is the per-user directory, and one file
  under ``<checkout>/data`` would make the next boot select the checkout,
  leaving the user's database behind.

The pytest process runs with an explicit ``TOFU_DATA_DIR`` outside the
checkout, the local kernel's policy branch, so import-time constants are
checked against this process's own base.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import re
import stat

import pytest

import runtime_path_policy

pytestmark = pytest.mark.unit

# The repository root a normal server hands to the policy.
SOURCE_ROOT = os.path.dirname(os.path.abspath(runtime_path_policy.__file__))
_TOOL_CACHE_KEYS = (
    'UV_CACHE_DIR', 'XDG_CACHE_HOME', 'npm_config_cache', 'PIP_CACHE_DIR',
    'XDG_DATA_HOME', 'UV_PYTHON_INSTALL_DIR', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR',
)


def _inside(path, root) -> bool:
    return Path(path).resolve().is_relative_to(Path(root).resolve())


@pytest.fixture
def runtime_base(monkeypatch, tmp_path):
    """Make the startup base whatever the real policy picks for a layout."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))

    def install(environment: dict[str, str], repository_root) -> Path:
        base = runtime_path_policy.resolve_runtime_base(
            environment, repository_root=repository_root)
        monkeypatch.setattr(runtime_path_policy, '_startup_base', base)
        return Path(base)
    return install


@pytest.fixture
def ambient_tool_caches_unset(monkeypatch):
    monkeypatch.delenv('TOFU_MCP_CACHE_DIR', raising=False)
    for key in (*_TOOL_CACHE_KEYS, 'CARGO_HOME', 'npm_config_prefix',
                'NPM_CONFIG_PREFIX'):
        monkeypatch.delenv(key, raising=False)


# The one module of the synthetic source tree, relative to the tree's root.
_SOURCE_MODULE = Path('lib', 'business.py')


def _source_tree(root: Path) -> Path:
    """A source tree with one module the boundary scan has to parse."""
    module = root / _SOURCE_MODULE
    module.parent.mkdir(parents=True)
    module.write_text("note = 'execute'\n", encoding='utf-8')
    return root


def _listing(root: Path) -> dict[str, tuple[bool, int, int]]:
    return {
        path.relative_to(root).as_posix():
            (path.is_dir(), path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob('*')}


@contextlib.contextmanager
def _read_only(root: Path):
    modes = {path: stat.S_IMODE(path.stat().st_mode)
             for path in (root, *root.rglob('*'))}
    for path, mode in modes.items():
        path.chmod(mode & ~0o222)
    try:
        yield
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


def _tool_environments(workspace: Path, *, prepare: bool):
    """The child environments an MCP launcher and a command receive."""
    from lib.mcp.client._vendor import _ensure_writable_caches
    from lib.project_mod.run_command import _get_cmd_env

    launcher: dict[str, str] = {}
    _ensure_writable_caches(launcher, prepare_directories=prepare)
    workspace.mkdir(exist_ok=True)
    return launcher, _get_cmd_env(str(workspace), project_root=str(workspace))


def test_read_only_payload_boundary_cache_lives_in_the_kernel_data_root(
        tmp_path, runtime_base, monkeypatch):
    from lib.storage import storage_boundary

    payload = _source_tree(tmp_path / 'payload')
    base = runtime_base({'TOFU_DATA_DIR': str(tmp_path / 'kernel')}, payload)
    scanned = []
    scan = storage_boundary._scan_python_file
    monkeypatch.setattr(
        storage_boundary, '_scan_python_file',
        lambda path, relative: scanned.append(relative) or scan(path, relative))
    before = _listing(payload)
    with _read_only(payload):
        for _boot in range(2):
            assert storage_boundary.boundary_report(payload)['ready']
    assert _listing(payload) == before, 'the boundary scan wrote into its payload'
    assert scanned == [_SOURCE_MODULE.as_posix()], 'the second boot did not reuse the cache'
    assert (base / 'data' / '.tofu-storage-boundary-cache.json').is_file()


def test_boundary_scan_keeps_a_fresh_checkout_on_its_per_user_data_root(
        tmp_path, runtime_base):
    from lib.storage import storage_boundary

    checkout = _source_tree(tmp_path / 'checkout')
    environment = {'XDG_DATA_HOME': str(tmp_path / 'xdg'),
                   'LOCALAPPDATA': str(tmp_path / 'xdg')}
    base = runtime_base(environment, checkout)
    assert not _inside(base, checkout)
    storage_boundary.boundary_report(checkout)
    assert not (checkout / 'data').exists()
    assert runtime_path_policy.resolve_runtime_base(
        environment, repository_root=checkout) == str(base), (
        'the next boot would select the checkout and leave the database behind')


@pytest.mark.parametrize('environment, existing_install', [
    ({}, True),
    ({'TOFU_DATA_LAYOUT': 'intree'}, False),
])
def test_normal_server_boundary_cache_stays_in_the_checkout_data_directory(
        tmp_path, runtime_base, environment, existing_install):
    from lib.storage import storage_boundary

    checkout = _source_tree(tmp_path / 'checkout')
    if existing_install:
        (checkout / 'data').mkdir()
        (checkout / 'data' / 'tofu.db').write_bytes(b'')
    assert runtime_base(environment, checkout) == checkout
    storage_boundary.boundary_report(checkout)
    assert (checkout / 'data' / '.tofu-storage-boundary-cache.json').is_file()


def test_kernel_tool_caches_live_in_its_data_root(
        tmp_path, runtime_base, ambient_tool_caches_unset):
    base = runtime_base({'TOFU_DATA_DIR': str(tmp_path / 'kernel')}, SOURCE_ROOT)
    assert not _inside(base, SOURCE_ROOT)
    for environment in _tool_environments(tmp_path / 'workspace', prepare=True):
        for key in _TOOL_CACHE_KEYS:
            assert _inside(environment[key], base / 'data' / 'mcp-cache'), key
    assert (base / 'data' / 'mcp-cache' / 'share').is_dir()


def test_explicit_tool_cache_directory_still_wins(
        tmp_path, runtime_base, ambient_tool_caches_unset, monkeypatch):
    runtime_base({'TOFU_DATA_DIR': str(tmp_path / 'kernel')}, SOURCE_ROOT)
    override = tmp_path / 'operator-cache'
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(override))
    for environment in _tool_environments(tmp_path / 'workspace', prepare=False):
        assert environment['UV_CACHE_DIR'] == str(override / 'uv')
        assert environment['UV_TOOL_DIR'] == str(override / 'share' / 'uv' / 'tools')


def test_normal_server_tool_caches_stay_in_the_checkout_data_directory(
        tmp_path, runtime_base, ambient_tool_caches_unset):
    from lib.mcp.client import _vendor
    from lib.project_mod import run_command

    assert str(runtime_base({'TOFU_DATA_LAYOUT': 'intree'}, SOURCE_ROOT)) == SOURCE_ROOT
    # The defaults every earlier release computed from the module locations.
    launcher_default = os.path.join(os.path.abspath(os.path.join(
        os.path.dirname(_vendor.__file__), '..', '..', '..')), 'data', 'mcp-cache')
    command_default = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(run_command.__file__))), 'data', 'mcp-cache')
    launcher, command = _tool_environments(tmp_path / 'workspace', prepare=False)
    assert launcher['UV_CACHE_DIR'] == os.path.join(launcher_default, 'uv')
    assert command['UV_CACHE_DIR'] == os.path.join(command_default, 'uv')
    assert launcher['XDG_DATA_HOME'] == command['XDG_DATA_HOME'] == os.path.join(
        launcher_default, 'share')


def test_oversized_tool_results_persist_inside_the_runtime_data_root():
    from lib.tasks_pkg.compaction._persist import _persist_to_disk

    result = _persist_to_disk(
        'x' * 100_000, 'run_command', 'tc_write_surface', 'conv_surface')
    persisted = Path(re.search(r'\[Persisted to: (.+?)\]', result).group(1))
    assert persisted.is_file()
    assert _inside(persisted, Path(runtime_path_policy.get_runtime_base())
                   / 'data' / 'tool-results')
    assert not _inside(persisted, SOURCE_ROOT)


def test_desktop_diagnostics_inbox_is_written_beside_the_other_logs():
    from lib.log import LOG_DIR
    import routes.api_v1.desktop as desktop

    assert Path(desktop._DIAG_LOG).parent == Path(LOG_DIR)


def test_compat_temp_dir_lives_in_the_runtime_data_root(tmp_path, runtime_base):
    from lib.compat import get_temp_dir

    base = runtime_base({'TOFU_DATA_DIR': str(tmp_path / 'kernel')}, SOURCE_ROOT)
    assert Path(get_temp_dir()) == base / 'data' / 'tmp'
