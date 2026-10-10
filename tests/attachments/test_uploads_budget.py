"""Uploads budget: the quota rejects growth past the declared ceiling, and
the orphan sweep quarantines before it deletes — user state is never
silently removed, and an in-flight upload is never swept."""

from __future__ import annotations

import os
import time

import pytest

from lib import uploads_budget

pytestmark = pytest.mark.unit


def _write(path: str, size: int, *, age_s: float = 0) -> None:
    with open(path, 'wb') as f:
        f.write(b'x' * size)
    if age_s:
        mtime = time.time() - age_s
        os.utime(path, (mtime, mtime))


def test_quota_rejects_when_directory_would_overflow(
        tmp_path, monkeypatch):
    monkeypatch.setattr(
        uploads_budget, 'uploads_quota_bytes', lambda: 1024 * 1024)
    images = tmp_path / 'images'
    images.mkdir()
    _write(str(images / 'a.png'), 700 * 1024)
    assert uploads_budget.check_quota(str(images), 200 * 1024) is None
    error = uploads_budget.check_quota(str(images), 400 * 1024)
    assert error and 'storage is full' in error


def test_quota_env_override_is_clamped(monkeypatch):
    monkeypatch.setenv('TOFU_UPLOADS_QUOTA_MB', 'not-a-number')
    assert uploads_budget.uploads_quota_bytes() == (
        uploads_budget.DEFAULT_UPLOADS_QUOTA_MB * 1024 * 1024)
    monkeypatch.setenv('TOFU_UPLOADS_QUOTA_MB', '1')
    assert uploads_budget.uploads_quota_bytes() == 64 * 1024 * 1024


def test_sweep_quarantines_only_old_unreferenced_files(tmp_path):
    images = tmp_path / 'images'
    images.mkdir()
    old = images / 'old.png'
    fresh = images / 'fresh.png'
    keep = images / 'keep.png'
    _write(str(old), 10, age_s=40 * 24 * 3600)
    _write(str(fresh), 10)
    _write(str(keep), 10, age_s=40 * 24 * 3600)

    stats = uploads_budget.sweep_orphans(str(images), {'keep.png'})

    assert stats['quarantined'] == 1
    assert not old.exists()
    assert (images / '.orphans' / 'old.png').is_file()
    assert fresh.is_file(), 'an in-flight upload must never be swept'
    assert keep.is_file(), 'a referenced image must never be swept'


def test_sweep_deletes_only_after_full_quarantine_window(tmp_path):
    images = tmp_path / 'images'
    quarantine = images / '.orphans'
    quarantine.mkdir(parents=True)
    recent = quarantine / 'recent.png'
    ancient = quarantine / 'ancient.png'
    _write(str(recent), 10, age_s=10 * 24 * 3600)
    _write(str(ancient), 10, age_s=40 * 24 * 3600)

    stats = uploads_budget.sweep_orphans(str(images), set())

    assert stats['deleted'] == 1
    assert not ancient.exists()
    assert recent.is_file(), 'quarantined files get the full second window'
