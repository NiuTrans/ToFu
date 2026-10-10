"""Native whole-authority relocation must never become a fresh-install fallback."""
from __future__ import annotations

from pathlib import Path
import hashlib
import pytest

from lib.storage_sidecar import native_launch

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('artifact', ['shadow', 'front'])
def test_missing_native_authority_with_fastpath_artifacts_never_runs_init(tmp_path, monkeypatch, artifact):
    authority = tmp_path / 'data' / 'tofudb-authority'
    namespace = hashlib.sha256(str(authority.resolve()).encode('utf-8')).hexdigest()
    if artifact == 'shadow':
        retained = authority.parent / 'tofudb-fastpath-shadow' / namespace
        monkeypatch.delenv('TOFU_DB_FASTPATH_DIR', raising=False)
    else:
        local = tmp_path / 'local'
        retained = local / 'tofudb-fastpath' / namespace
        monkeypatch.setenv('TOFU_DB_FASTPATH_DIR', str(local))
    retained.mkdir(parents=True)
    (retained / 'evidence').write_bytes(b'preserved')
    called = []
    monkeypatch.setattr(native_launch.subprocess, 'run', lambda *args, **kwargs: called.append(args))
    with pytest.raises(RuntimeError, match='recovery artifacts exist'):
        native_launch._bootstrap_fresh_authority('/test/tofu-db', authority)
    assert not called and not authority.exists()
    assert (retained / 'evidence').read_bytes() == b'preserved'


@pytest.mark.parametrize('extra', [
    ['--durability', 'normal'],
    ['--backup-dir', '/separate-backup'],
    ['--fastpath-sync-interval-ms', '999'],
])
def test_native_cli_rejects_ambiguous_durability_or_shadow_settings_before_activation(tmp_path, extra):
    import os
    import subprocess

    binary = os.environ.get('TOFUDB_BIN')
    if not binary:
        pytest.skip('set TOFUDB_BIN to the tested native binary for this contract lane')
    assert Path(binary).is_file(), 'TOFUDB_BIN must name the tested native binary'
    authority = tmp_path / 'authority'
    local = tmp_path / 'local'
    local.mkdir()
    subprocess.run([binary, 'init', '--data-dir', str(authority)], check=True, capture_output=True)
    result = subprocess.run([
        binary, 'serve', '--data-dir', str(authority), '--owner-id', '11',
        '--tenant-id', '7', '--fastpath-dir', str(local), *extra,
    ], input=b'', capture_output=True,
       env={**os.environ, 'TOFU_STORAGE_TOKEN': 'native-fastpath-contract-test-secret'})
    assert result.returncode != 0
    assert not (authority / 'FASTPATH').exists()
    assert not (authority / 'FASTPATH_PENDING').exists()
    assert not (tmp_path / 'tofudb-fastpath-shadow').exists()
