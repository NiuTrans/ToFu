"""A supervisor relaunches exactly the engine bytes it admitted.

Executable specification for ``lib/storage/admitted_engine.py``. On
2026-10-27 a staged deployment replaced the certified engine path at 04:57
while the admitted engine kept running; at 06:58 every restart was refused
because the supervisor reopened that path. The admitted bytes must stay
launchable whatever later happens to the configured path, the private copy
must survive a temporary-file cleaner, and copies no live process leases must
not accumulate.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import stat

import pytest

import lib.storage.admitted_engine as admitted_engine
from lib.storage.admitted_engine import AdmittedEngine, AdmittedEngineLost


pytestmark = pytest.mark.unit
_BUDGET = 1024 * 1024


@pytest.fixture
def root(tmp_path, monkeypatch):
    root = tmp_path / 'admitted'
    monkeypatch.setattr(admitted_engine, 'private_root', lambda: (
        admitted_engine.ensure_private_directory(root), root)[1])
    return root


def _engine(directory: Path, payload: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'tofu-db'
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def _admit(path: Path, seen: list | None = None) -> AdmittedEngine:
    def verify(digest):
        if seen is not None:
            seen.append(digest)
    return AdmittedEngine.admit(str(path), verify, max_bytes=_BUDGET)


def _deploy_by_rename(path: Path, payload: bytes) -> None:
    staged = path.with_name(path.name + '.staged')
    staged.write_bytes(payload)
    staged.chmod(0o755)
    os.replace(staged, path)


def test_relaunch_runs_admitted_bytes_after_a_rename_deployment(tmp_path, root):
    path = _engine(tmp_path / 'certified', b'admitted-engine')
    seen = []
    engine = _admit(path, seen)
    try:
        assert seen == [hashlib.sha256(b'admitted-engine').hexdigest()]
        _deploy_by_rename(path, b'deployed-replacement')

        launched = Path(engine.launch_path())
        assert launched.read_bytes() == b'admitted-engine'
        assert launched.name == 'tofu-db'  # argv[0] keeps the engine name
        assert launched.parent.name == engine.sha256
        assert stat.S_IMODE(launched.stat().st_mode) & 0o222 == 0
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert engine.path == str(path)
    finally:
        engine.close()


def test_relaunch_survives_an_in_place_rewrite_and_removal_of_the_path(tmp_path, root):
    path = _engine(tmp_path / 'certified', b'admitted-engine')
    engine = _admit(path)
    try:
        path.write_bytes(b'rewritten in place')
        assert Path(engine.launch_path()).read_bytes() == b'admitted-engine'
        path.unlink()
        assert Path(engine.launch_path()).read_bytes() == b'admitted-engine'
    finally:
        engine.close()


def test_a_cleaned_copy_is_republished_from_the_held_descriptor(tmp_path, root):
    path = _engine(tmp_path / 'certified', b'admitted-engine')
    engine = _admit(path)
    try:
        _deploy_by_rename(path, b'deployed-replacement')
        shutil.rmtree(root / engine.sha256)  # an age-based tmp cleaner
        launched = Path(engine.launch_path())
        assert launched.read_bytes() == b'admitted-engine'
        # The republished copy is leased again, so collection keeps it.
        other = _admit(_engine(tmp_path / 'other', b'other-engine'))
        other.close()
        assert launched.read_bytes() == b'admitted-engine'
    finally:
        engine.close()


def test_a_copy_altered_in_place_is_a_lost_admission(tmp_path, root):
    path = _engine(tmp_path / 'certified', b'admitted-engine')
    engine = _admit(path)
    try:
        copy = Path(engine.launch_path())
        copy.chmod(0o700)
        copy.write_bytes(b'tampered')
        with pytest.raises(AdmittedEngineLost, match='no longer holds'):
            engine.launch_path()
    finally:
        engine.close()


def test_a_rejected_digest_publishes_nothing(tmp_path, root):
    path = _engine(tmp_path / 'certified', b'unpinned-engine')

    def refuse(_digest):
        raise RuntimeError('tofu-db SHA-256 mismatch')

    with pytest.raises(RuntimeError, match='SHA-256 mismatch'):
        AdmittedEngine.admit(str(path), refuse, max_bytes=_BUDGET)
    assert sorted(entry.name for entry in root.iterdir()) == []


def test_an_engine_beyond_the_verification_budget_is_refused(tmp_path, root):
    path = _engine(tmp_path / 'certified', b'x' * 64)
    with pytest.raises(RuntimeError, match='verification budget'):
        AdmittedEngine.admit(str(path), lambda _digest: None, max_bytes=16)
    assert sorted(entry.name for entry in root.iterdir()) == []


def test_admission_collects_only_copies_no_live_process_leases(tmp_path, root):
    held = _admit(_engine(tmp_path / 'held', b'held-engine'))
    released = _admit(_engine(tmp_path / 'released', b'released-engine'))
    released_digest = released.sha256
    released.close()
    shared_a = _admit(_engine(tmp_path / 'shared-a', b'shared-engine'))
    shared_b = _admit(_engine(tmp_path / 'shared-b', b'shared-engine'))
    try:
        newest = _admit(_engine(tmp_path / 'newest', b'newest-engine'))
        try:
            names = {entry.name for entry in root.iterdir()}
            assert held.sha256 in names
            assert shared_a.sha256 in names
            assert newest.sha256 in names
            assert released_digest not in names
            # Two admissions of one digest share one copy.
            assert shared_a.launch_path() == shared_b.launch_path()
        finally:
            newest.close()
    finally:
        held.close()
        shared_a.close()
        shared_b.close()
