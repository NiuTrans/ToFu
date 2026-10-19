"""Contracts for the prebuilt tofu-db engine install path.

The prebuilt fast path (download -> verify -> deploy) replaced a per-user
cargo compile as install.sh's default tofu-db supply. These tests pin the
verification discipline of scripts/install_tofudb_prebuilt.py, the packaging
contract of scripts/build_tofudb_prebuilt.py, and install.sh's fallback
order (prebuilt -> source build -> warned skip, never a gate).
"""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / 'install.sh'
INSTALL_SCRIPT = ROOT / 'scripts/install_tofudb_prebuilt.py'
BUILD_SCRIPT = ROOT / 'scripts/build_tofudb_prebuilt.py'
sys.path.insert(0, str(ROOT / 'scripts'))

import build_tofudb_prebuilt  # noqa: E402

# A stand-in engine: deploy_tofudb_engine's smoke run requires the candidate
# to execute and identify as tofu-db within a bounded no-arg run.
FAKE_ENGINE = b'#!/bin/sh\necho "usage: tofu-db <init|serve> ..."\n'


def _make_engine(tmp_path: Path) -> Path:
    engine = tmp_path / 'engine-src' / 'tofu-db'
    engine.parent.mkdir(parents=True, exist_ok=True)
    engine.write_bytes(FAKE_ENGINE)
    engine.chmod(0o755)
    return engine


def _make_archive(tmp_path: Path, members: dict[str, bytes], name: str = 'tofu-db-9.9.9-x86_64-unknown-linux-musl.tar.gz') -> Path:
    archive = tmp_path / name
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'w:gz') as bundle:
        for member_name, payload in members.items():
            info = tarfile.TarInfo(member_name)
            info.size = len(payload)
            info.mode = 0o755
            bundle.addfile(info, io.BytesIO(payload))
    return archive


def _make_checksum(archive: Path, digest: str | None = None, names: str | None = None) -> Path:
    checksum = archive.with_name(archive.name + '.sha256')
    digest = digest or hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f'{digest}  {names or archive.name}\n', encoding='ascii')
    return checksum


def _run_install(archive: Path, checksum: Path, dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(INSTALL_SCRIPT), str(archive), str(checksum), str(dest)],
        capture_output=True, text=True, timeout=60,
    )


def _happy_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    archive = _make_archive(tmp_path, {'tofu-db': FAKE_ENGINE})
    checksum = _make_checksum(archive)
    dest = tmp_path / 'target/release/tofu-db'
    return archive, checksum, dest


def test_prebuilt_install_deploys_engine_with_launch_pin(tmp_path):
    archive, checksum, dest = _happy_paths(tmp_path)
    result = _run_install(archive, checksum, dest)
    assert result.returncode == 0, result.stderr
    assert dest.read_bytes() == FAKE_ENGINE
    assert stat.S_IMODE(dest.stat().st_mode) & 0o111
    sidecar = dest.with_name('tofu-db.sha256')
    assert sidecar.read_text(encoding='ascii').split()[0] == hashlib.sha256(FAKE_ENGINE).hexdigest()
    record = dest.parent / 'build.json'
    assert 'prebuilt archive' in record.read_text(encoding='utf-8')


def test_prebuilt_install_refuses_a_checksum_mismatch(tmp_path):
    archive, checksum, dest = _happy_paths(tmp_path)
    checksum.write_text(f'{"0" * 64}  {archive.name}\n', encoding='ascii')
    result = _run_install(archive, checksum, dest)
    assert result.returncode == 1
    assert 'checksum mismatch' in result.stderr
    assert not dest.exists()


def test_prebuilt_install_refuses_a_checksum_naming_another_file(tmp_path):
    archive, checksum, dest = _happy_paths(tmp_path)
    _make_checksum(archive, names='tofu-db-9.9.9-aarch64-unknown-linux-musl.tar.gz')
    result = _run_install(archive, checksum, dest)
    assert result.returncode == 1
    assert 'must name exactly the downloaded archive' in result.stderr
    assert not dest.exists()


def test_prebuilt_install_refuses_an_archive_without_exactly_one_engine(tmp_path):
    two_files = _make_archive(tmp_path, {'tofu-db': FAKE_ENGINE, 'README': b'x'}, name='tofu-db-9.9.9-x86_64-unknown-linux-musl.tar.gz')
    result = _run_install(two_files, _make_checksum(two_files), tmp_path / 'out/tofu-db')
    assert result.returncode == 1
    assert 'exactly one engine binary' in result.stderr

    no_engine = _make_archive(tmp_path / 'empty', {'tofu-db.txt': FAKE_ENGINE}, name='tofu-db-9.9.9-x86_64-unknown-linux-musl.tar.gz')
    result = _run_install(no_engine, _make_checksum(no_engine), tmp_path / 'out2/tofu-db')
    assert result.returncode == 1


def test_prebuilt_install_refuses_path_traversal(tmp_path):
    archive = _make_archive(tmp_path, {'../tofu-db': FAKE_ENGINE})
    result = _run_install(archive, _make_checksum(archive), tmp_path / 'out/tofu-db')
    assert result.returncode == 1
    assert not (tmp_path / 'tofu-db').exists()


def test_prebuilt_install_refuses_a_non_executing_candidate(tmp_path):
    archive = _make_archive(tmp_path, {'tofu-db': b'not an executable format'})
    result = _run_install(archive, _make_checksum(archive), tmp_path / 'out/tofu-db')
    assert result.returncode == 1
    assert not (tmp_path / 'out/tofu-db').exists()


def test_build_script_packages_a_deterministic_asset(tmp_path):
    engine = _make_engine(tmp_path)
    first_asset, first_sidecar = build_tofudb_prebuilt.package_asset(
        engine, '9.9.9', 'x86_64-unknown-linux-musl', tmp_path / 'one')
    second_asset, second_sidecar = build_tofudb_prebuilt.package_asset(
        engine, '9.9.9', 'x86_64-unknown-linux-musl', tmp_path / 'two')
    assert first_asset.name == 'tofu-db-9.9.9-x86_64-unknown-linux-musl.tar.gz'
    assert first_asset.read_bytes() == second_asset.read_bytes()
    expected = hashlib.sha256(first_asset.read_bytes()).hexdigest()
    assert first_sidecar.read_text(encoding='ascii') == f'{expected}  {first_asset.name}\n'
    with tarfile.open(first_asset, 'r:gz') as bundle:
        entries = bundle.getmembers()
        assert [entry.name for entry in entries] == ['tofu-db']
        assert entries[0].mode == 0o755
        assert bundle.extractfile(entries[0]).read() == FAKE_ENGINE


def test_build_script_rejects_unknown_triples():
    result = subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), '--target', 'mips64-unknown-none'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 2
    assert 'invalid choice' in result.stderr


def _installer_tofudb_block() -> str:
    source = INSTALLER.read_text(encoding='utf-8')
    start = source.index('Step 8.47: tofu-db storage engine')
    end = source.index('Step 8.5: Select the standalone personal storage topology', start)
    return source[start:end]


def test_installer_tries_the_verified_prebuilt_before_any_source_build():
    block = _installer_tofudb_block()
    prebuilt = block.index('_tofu_dl "${_TOFUDB_BASE}/${_TOFUDB_ASSET}"')
    build = block.index('_install_run 900 "$_CARGO" build --release')
    assert prebuilt < build, 'the prebuilt download must precede the cargo fallback'
    assert '_TOFUDB_READY' in block, 'the prebuilt success path must gate the source build'


def test_installer_tofudb_step_stays_an_accelerant_never_a_gate():
    block = _installer_tofudb_block()
    no_tofudb = block.index('if [[ "${NO_TOFUDB:-0}"')
    assert no_tofudb < block.index('_tofu_dl "${_TOFUDB_BASE}/${_TOFUDB_ASSET}"')
    assert no_tofudb < block.index('_install_run 900 "$_CARGO" build --release')
    assert 'TOFU_TOFUDB_NO_DOWNLOAD' in block
    assert 'TOFU_TOFUDB_PREBUILT_BASE_URL' in block
    # A failed prebuilt must fall through to the source build, not abort.
    assert 'falling back to a source build' in block


def test_installer_tofudb_prebuilt_uses_the_release_channel_and_static_triples():
    block = _installer_tofudb_block()
    assert 'rangehow/ToFu/releases/download/v${_TOFUDB_VERSION}' in block
    assert 'x86_64-unknown-linux-musl' in block
    assert 'aarch64-unknown-linux-musl' in block
    assert 'aarch64-apple-darwin' in block
    # Deployment goes through the one supported engine deployment path.
    assert 'deploy_tofudb_engine' in (ROOT / 'scripts/install_tofudb_prebuilt.py').read_text(encoding='utf-8')
