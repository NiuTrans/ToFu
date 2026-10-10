"""scripts/deploy_tofudb_engine.py: atomic engine + pin deployment.

The script defaults to syncing the project ``.env``; these tests always pass
an explicit ``--env-file`` inside their own tmp directory so the real
operator file can never be touched.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'deploy_tofudb_engine.py'


def _candidate(tmp_path: Path, name: str = 'tofu-db',
               body: str = 'echo "usage: tofu-db fake-engine"') -> Path:
    engine = tmp_path / name
    engine.write_text(f'#!/bin/sh\n{body}\n', encoding='utf-8')
    engine.chmod(0o755)
    return engine


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, timeout=60,
    )


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _deploy(tmp_path: Path, candidate: Path, *extra: str) -> subprocess.CompletedProcess:
    env_file = tmp_path / '.env'
    dest_dir = tmp_path / 'certified'
    dest_dir.mkdir(exist_ok=True)  # an existing directory is unambiguously a directory
    return _run('--binary', str(candidate),
                '--dest', str(dest_dir),
                '--env-file', str(env_file), *extra)


def test_deploy_installs_binary_sidecar_and_provenance(tmp_path):
    candidate = _candidate(tmp_path)
    result = _deploy(tmp_path, candidate)
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    dest = tmp_path / 'certified' / 'tofu-db'
    assert dest.read_bytes() == candidate.read_bytes()
    assert os.access(dest, os.X_OK)
    digest = _digest(dest)
    assert (tmp_path / 'certified' / 'tofu-db.sha256').read_text(
        encoding='ascii') == f'{digest}  tofu-db\n'
    build = json.loads((tmp_path / 'certified' / 'build.json').read_text(encoding='utf-8'))
    assert build['binary_sha256'] == digest
    assert build['previous_binary_sha256'] is None
    assert record['binary_sha256'] == digest
    assert record['env_file'] is None  # no dotenv declared a pin, nothing to sync
    assert record['dry_run'] is False


def test_deploy_dry_run_mutates_nothing(tmp_path):
    candidate = _candidate(tmp_path)
    result = _deploy(tmp_path, candidate, '--dry-run')
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['dry_run'] is True
    assert not (tmp_path / 'certified' / 'tofu-db').exists()


def test_redeploy_preserves_backup_and_previous_pin(tmp_path):
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    first = _candidate(tmp_path / 'a')
    second = _candidate(tmp_path / 'b', body='echo "usage: tofu-db upgraded"')
    assert _deploy(tmp_path, first).returncode == 0
    result = _deploy(tmp_path, second)
    assert result.returncode == 0, result.stderr
    dest = tmp_path / 'certified' / 'tofu-db'
    backups = list((tmp_path / 'certified').glob('tofu-db.bak-*'))
    assert len(backups) == 1
    assert backups[0].read_bytes() == first.read_bytes()
    build = json.loads((tmp_path / 'certified' / 'build.json').read_text(encoding='utf-8'))
    assert build['binary_sha256'] == _digest(dest) == _digest(second)
    assert build['previous_binary_sha256'] == _digest(first)
    assert (tmp_path / 'certified' / 'tofu-db.sha256').read_text(
        encoding='ascii').split()[0] == _digest(second)


def test_deploy_rewrites_stale_env_pin(tmp_path):
    candidate = _candidate(tmp_path)
    env_file = tmp_path / '.env'
    env_file.write_text(
        'TOFUDB_EXPECTED_SHA256=' + '0' * 64 + '\nUNRELATED=keep\n', encoding='utf-8')
    result = _deploy(tmp_path, candidate)
    assert result.returncode == 0, result.stderr
    lines = env_file.read_text(encoding='utf-8').splitlines()
    assert f'TOFUDB_EXPECTED_SHA256={_digest(candidate)}' in lines
    assert 'UNRELATED=keep' in lines
    assert json.loads(result.stdout)['env_file'] == str(env_file)


def test_deploy_warns_when_env_bin_points_elsewhere(tmp_path):
    candidate = _candidate(tmp_path)
    env_file = tmp_path / '.env'
    env_file.write_text('TOFUDB_BIN=/somewhere/else/tofu-db\n', encoding='utf-8')
    result = _deploy(tmp_path, candidate)
    assert result.returncode == 0, result.stderr
    assert 'TOFUDB_BIN' in result.stderr


def test_deploy_refuses_non_executable_candidate(tmp_path):
    candidate = _candidate(tmp_path)
    candidate.chmod(0o644)
    result = _deploy(tmp_path, candidate)
    assert result.returncode == 1
    assert 'not executable' in result.stderr
    assert not (tmp_path / 'certified' / 'tofu-db').exists()


def test_deploy_refuses_unidentified_engine(tmp_path):
    candidate = _candidate(tmp_path, body='echo "definitely not the engine"')
    result = _deploy(tmp_path, candidate)
    assert result.returncode == 1
    assert 'unidentified engine' in result.stderr
    assert not (tmp_path / 'certified' / 'tofu-db').exists()


def test_pin_only_redeploy_refreshes_sidecar_without_touching_artifact(tmp_path):
    candidate = _candidate(tmp_path)
    assert _deploy(tmp_path, candidate).returncode == 0
    dest = tmp_path / 'certified' / 'tofu-db'
    before = dest.stat().st_mtime_ns
    (tmp_path / 'certified' / 'tofu-db.sha256').unlink()  # simulate a lost pin
    result = _run('--binary', str(dest), '--dest', str(dest),
                  '--env-file', str(tmp_path / '.env'))
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert record['pin_only'] is True
    assert dest.stat().st_mtime_ns == before
    assert (tmp_path / 'certified' / 'tofu-db.sha256').read_text(
        encoding='ascii').split()[0] == _digest(dest)
