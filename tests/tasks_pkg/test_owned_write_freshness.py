"""Owned command observation transitions; command admission supplies exclusion."""
from __future__ import annotations

import pytest

from lib import write_freshness as wf

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def tokens(monkeypatch):
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    wf._reset_for_tests()
    yield
    wf._reset_for_tests()


def test_owned_write_advances_only_observed_owner(tmp_path):
    path = tmp_path / 'source.py'
    path.write_bytes(b'x=1\n')
    wf.record('owner', str(path))
    wf.record('sibling', str(path))
    evidence = wf.capture_owned_write('owner', str(path))
    assert evidence is not None
    path.write_bytes(b'x = 1\n')
    assert wf.reconcile_owned_write(evidence)
    assert not wf.is_stale('owner', str(path))
    assert wf.is_stale('sibling', str(path))
    assert not wf.reconcile_owned_write(evidence)


def test_stale_preimage_and_unobserved_file_never_gain_ownership(tmp_path):
    path = tmp_path / 'source.py'
    path.write_bytes(b'x=1\n')
    wf.record('owner', str(path))
    path.write_bytes(b'x=2\n')
    assert wf.capture_owned_write('owner', str(path)) is None
    assert wf.capture_owned_write('unknown', str(path)) is None
    assert not wf.has_token('unknown', str(path))
    assert wf.is_stale('owner', str(path))


@pytest.mark.parametrize('intervening', ['reread', 'reset', 'drop'])
def test_reconciliation_does_not_overwrite_newer_observation(tmp_path, intervening):
    path = tmp_path / 'source.py'
    path.write_bytes(b'x=1\n')
    wf.record('owner', str(path))
    evidence = wf.capture_owned_write('owner', str(path))
    if intervening == 'reread':
        # Same bytes, but another observation is an ABA generation change.
        wf.record('owner', str(path))
    elif intervening == 'reset':
        wf._reset_for_tests()
        wf.record('owner', str(path))
    else:
        wf.drop('owner', str(path))
    path.write_bytes(b'x = 1\n')
    assert not wf.reconcile_owned_write(evidence)
    if intervening != 'drop':
        assert wf.is_stale('owner', str(path))
    else:
        assert not wf.has_token('owner', str(path))


def test_disabled_missing_noop_and_large_metadata_evidence_are_not_owned(tmp_path, monkeypatch):
    path = tmp_path / 'source.py'
    path.write_bytes(b'x=1\n')
    wf.record('owner', str(path))
    evidence = wf.capture_owned_write('owner', str(path))
    assert not wf.reconcile_owned_write(evidence)
    path.unlink()
    assert not wf.reconcile_owned_write(evidence)
    path.write_bytes(b'x=2\n')
    monkeypatch.setenv('TOFU_WRITE_FRESHNESS_GATE', '0')
    assert wf.capture_owned_write('owner', str(path)) is None
    assert not wf.reconcile_owned_write(evidence)
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE')
    monkeypatch.setattr(wf, '_CONTENT_HASH_MAX_BYTES', 1)
    wf.record('owner', str(path))
    assert wf.capture_owned_write('owner', str(path)) is None
