"""Source builds under launcher caches are not members of the repository Cargo workspace.

uv builds a dependency without a matching wheel (pydantic-core via maturin) from
its sdist inside the launcher cache, ``<repo>/data/mcp-cache/uv/sdists-*/``.
Cargo resolves a crate's workspace by walking ancestor manifests, and the
repository-root ``[workspace]`` claimed every crate beneath it, so maturin's
``cargo metadata`` refused the sdist ("current package believes it's in a
workspace when it's not") and the MCP server exited before its handshake. A
worktree nested under ``.tofu`` is captured the same way by the outer checkout
for every crate its own root excludes.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from lib.mcp.client._vendor import _mcp_cache_root
from lib.tools.process_environment import tool_cache_defaults

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def _cargo() -> str:
    cargo = shutil.which('cargo') or shutil.which(
        'cargo', path=str(Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo')) / 'bin'))
    if cargo is None:
        pytest.skip('Cargo is required to verify actual workspace discovery')
    return cargo


def _crate(directory: Path, name: str) -> Path:
    (directory / 'src').mkdir(parents=True, exist_ok=True)
    (directory / 'src/lib.rs').write_text('')
    (directory / 'Cargo.toml').write_text(
        f'[package]\nname = "{name}"\nversion = "0.0.0"\nedition = "2021"\n')
    return directory


def _checkout(directory: Path) -> None:
    """The real root manifest over loadable stand-ins for its members."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest = (ROOT / 'Cargo.toml').read_text()
    for index, member in enumerate(tomllib.loads(manifest)['workspace']['members']):
        _crate(directory / member, f'member-{index}')
    (directory / 'Cargo.toml').write_text(manifest)


@pytest.mark.parametrize('checkout', ['.', '.tofu/worktrees/feature'])
def test_launcher_cache_sdist_crate_is_its_own_workspace(tmp_path, monkeypatch, checkout):
    cargo = _cargo()
    monkeypatch.delenv('TOFU_MCP_CACHE_DIR', raising=False)
    uv_cache = Path(tool_cache_defaults(_mcp_cache_root())['UV_CACHE_DIR']).relative_to(ROOT)
    _checkout(tmp_path)
    _checkout(tmp_path / checkout)
    crate = _crate(tmp_path / checkout / uv_cache / 'sdists-v10/pypi/pydantic-core/2.46.4/build/src',
                   'pydantic-core')

    result = subprocess.run(
        [cargo, 'metadata', '--format-version', '1', '--no-deps',
         '--manifest-path', str(crate / 'Cargo.toml')],
        capture_output=True, text=True, timeout=60, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)['workspace_root']) == crate
