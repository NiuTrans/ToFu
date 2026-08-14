"""Filesystem fault gates exercised from the project-backed pytest root."""

from __future__ import annotations

import errno
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from lib.storage import StorageError
from lib.storage_sidecar import preflight


pytestmark = pytest.mark.unit


def _fs_type(path: Path) -> str:
    result = subprocess.run(
        ['findmnt', '-r', '-n', '-T', str(path), '-o', 'FSTYPE'],
        text=True, capture_output=True, check=False, timeout=10,
    )
    return result.stdout.strip() if result.returncode == 0 else 'unknown'


@pytest.mark.skipif(not __import__('sys').platform.startswith('linux'),
                    reason='Linux FUSE certification')
def test_real_project_mount_proves_fsync_replace_and_locking(tmp_path):
    fs_type = _fs_type(tmp_path)
    if 'fuse' not in fs_type.lower():
        pytest.skip(f'not running from FUSE (fs_type={fs_type})')

    report = preflight.run_filesystem_preflight(tmp_path / 'data')

    assert report.atomic_replace is True
    assert report.file_lock is True
    assert report.fsync_ms > 0
    assert not list((tmp_path / 'data').glob('.storage-preflight-*'))


def test_preflight_fails_closed_when_space_probe_reports_full(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        preflight.shutil, 'disk_usage',
        lambda _path: SimpleNamespace(total=1024, used=1024, free=0),
    )

    with pytest.raises(StorageError) as raised:
        preflight.run_filesystem_preflight(tmp_path / 'data')

    assert raised.value.code == 'database_unavailable'
    assert 'space' in raised.value.message.lower()


def test_preflight_classifies_short_write_without_leaving_probe_files(
        tmp_path, monkeypatch):
    original_open = Path.open

    class _ShortWriter:
        def __init__(self, stream):
            self._stream = stream

        def __enter__(self):
            self._stream.__enter__()
            return self

        def __exit__(self, *args):
            return self._stream.__exit__(*args)

        def write(self, payload):
            self._stream.write(payload)
            return len(payload) - 1

        def fileno(self):
            return self._stream.fileno()

    def faulty_open(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if mode == 'xb' and path.name.endswith('.new'):
            return _ShortWriter(stream)
        return stream

    monkeypatch.setattr(Path, 'open', faulty_open)
    data_dir = tmp_path / 'data'
    with pytest.raises(StorageError) as raised:
        preflight.run_filesystem_preflight(data_dir)

    assert raised.value.code == 'database_unavailable'
    assert not list(data_dir.glob('.storage-preflight-*'))


def test_preflight_fails_closed_on_atomic_replace_error(tmp_path, monkeypatch):
    def fail_replace(_source, _target):
        raise OSError(errno.EIO, 'injected atomic replace failure')

    monkeypatch.setattr(preflight.os, 'replace', fail_replace)
    data_dir = tmp_path / 'data'
    with pytest.raises(StorageError) as raised:
        preflight.run_filesystem_preflight(data_dir)

    assert raised.value.code == 'database_unavailable'
    assert not list(data_dir.glob('.storage-preflight-*'))


def test_preflight_fails_closed_when_fsync_profile_exceeds_bound(
        tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_PREFLIGHT_MAX_MS', '0')

    with pytest.raises(StorageError) as raised:
        preflight.run_filesystem_preflight(tmp_path / 'data')

    assert raised.value.code == 'database_unavailable'
    assert 'latency' in raised.value.message.lower()
