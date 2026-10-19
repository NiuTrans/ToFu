"""Snapshot evidence is the observed full source, not a post-read disk guess."""
from __future__ import annotations

import hashlib
import os

import pytest

from lib import write_freshness as freshness
from lib.project_mod.read_tools import tool_read_files
from lib.tasks_pkg.handlers._write_freshness_gate import record_read_paths

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    freshness._reset_for_tests()
    yield
    freshness._reset_for_tests()


def observe(root, args):
    observations = []
    result = tool_read_files(str(root), args.get('reads', [args]), read_observations=observations)
    return result, observations


def stamp(root, args, result, observations):
    return record_read_paths({'id': 'actor', 'convId': 'actor'}, args, str(root), result,
                             observations=observations)


def test_observed_bytes_win_mutation_before_token_creation(tmp_path, monkeypatch):
    target = tmp_path / 'a.txt'
    target.write_text('observed\n')
    args = {'path': 'a.txt'}
    result, observations = observe(tmp_path, args)
    target.write_text('unseen!!\n')
    # Issuing the token must not even attempt a path fingerprint.
    monkeypatch.setattr(freshness, '_fingerprint', lambda *a, **kw: pytest.fail('late reread'))
    assert stamp(tmp_path, args, result, observations) == 1
    assert 'observed' in result and 'unseen' not in result
    assert freshness.is_stale('actor', str(target))


def test_ranged_read_hashes_entire_observed_source_above_legacy_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(freshness, '_CONTENT_HASH_MAX_BYTES', 8)
    target = tmp_path / 'a.txt'
    before = b'visible\nhidden line\n'
    target.write_bytes(before)
    args = {'path': 'a.txt', 'start_line': 1, 'end_line': 1}
    result, observations = observe(tmp_path, args)
    assert 'hidden line' not in result
    assert observations[0].fingerprint[:3] == ('s', len(before), hashlib.sha256(before).hexdigest())
    assert stamp(tmp_path, args, result, observations) == 1
    st = target.stat()
    target.write_bytes(b'visible\nUNSEEN line\n')
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert freshness.is_stale('actor', str(target))


@pytest.mark.parametrize('bad_first', [False, True])
def test_mixed_batch_records_only_successful_item(tmp_path, bad_first):
    target = tmp_path / 'ok.txt'
    target.write_text('ok\n')
    bad = tmp_path / 'bad.txt'
    bad.write_bytes(b'\x00opaque')
    paths = ['bad.txt', 'ok.txt'] if bad_first else ['ok.txt', 'bad.txt']
    args = {'reads': [{'path': path} for path in paths]}
    result, observations = observe(tmp_path, args)
    assert stamp(tmp_path, args, result, observations) == 1
    assert freshness.has_token('actor', str(target))
    assert not freshness.has_token('actor', str(bad))


def test_legacy_result_without_observations_never_claims_a_digest(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('now\n')
    assert record_read_paths({'id': 'actor'}, {'path': 'a.txt'}, str(tmp_path),
                             'File: a.txt\nprevious bytes') == 0
    assert not freshness.has_token('actor', str(target))


def test_empty_source_has_an_observed_digest(tmp_path):
    target = tmp_path / 'empty.txt'
    target.write_bytes(b'')
    args = {'path': target.name}
    result, observations = observe(tmp_path, args)
    assert stamp(tmp_path, args, result, observations) == 1
    assert not freshness.is_stale('actor', str(target))
    target.write_text('new')
    assert freshness.is_stale('actor', str(target))


def test_replaced_semantic_root_is_stale_even_with_identical_bytes(tmp_path):
    root = tmp_path / 'root'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('same\n')
    args = {'path': 'a.txt'}
    result, observations = observe(root, args)
    root.rename(tmp_path / 'old-root')
    root.mkdir()
    target.write_text('same\n')
    assert stamp(root, args, result, observations) == 1
    assert freshness.is_stale('actor', str(target))


def test_symlink_replacement_does_not_validate_observation(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('same\n')
    args = {'path': 'a.txt'}
    result, observations = observe(tmp_path, args)
    assert stamp(tmp_path, args, result, observations) == 1
    target.rename(tmp_path / 'elsewhere.txt')
    target.symlink_to(tmp_path / 'elsewhere.txt')
    assert freshness.is_stale('actor', str(target))


def test_successful_write_renewal_preserves_root_identity(tmp_path):
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    args = {'path': target.name}
    result, observations = observe(tmp_path, args)
    assert stamp(tmp_path, args, result, observations) == 1
    target.write_text('after\n')
    freshness.record_written('actor', str(target), b'after\n')
    assert not freshness.is_stale('actor', str(target))
    assert freshness._tokens[('actor', str(target))][0][3:] == observations[0].fingerprint[3:]


def test_snapshot_token_persistence_keeps_original_digest(tmp_path, monkeypatch):
    target = tmp_path / 'a.txt'
    target.write_text('before\n')
    args = {'path': target.name}
    result, observations = observe(tmp_path, args)
    assert stamp(tmp_path, args, result, observations) == 1
    monkeypatch.setattr(freshness, '_snapshot_path', lambda: str(tmp_path / 'tokens.json'))
    assert freshness.save_snapshot()
    freshness._reset_for_tests()
    target.write_text('changed\n')
    assert freshness.load_snapshot() == 1
    assert freshness.is_stale('actor', str(target))
