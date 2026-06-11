"""Tests for the file-history store compaction (snapshots.jsonl rotation
+ orphan-blob GC) added alongside the 2026-06 robustness pass.

These exercise ``lib.file_history.store`` directly with a temp project
dir — no DB, no Flask, no network.
"""
from __future__ import annotations

import importlib

import pytest

store = importlib.import_module('lib.file_history.store')
api = importlib.import_module('lib.file_history.api')


@pytest.fixture()
def proj(tmp_path):
    return str(tmp_path)


def _edit_and_snapshot(base, rel, content, *, task_id='t'):
    """Write a file, stage its backup, and pin a snapshot — one 'round'.

    ``content`` MUST have a length distinct from the previous round's,
    otherwise ``stage_backup``'s size+mtime dedup can collapse two rounds
    into one version when both writes land in the same mtime tick (a real
    behaviour we rely on in production, but a footgun in tests).
    """
    import os
    abs_p = os.path.join(base, rel)
    os.makedirs(os.path.dirname(abs_p) or '.', exist_ok=True)
    with open(abs_p, 'w', encoding='utf-8') as f:
        f.write(content)
    api.track_edit(base, rel, task_id=task_id)
    return api.make_snapshot(base, task_id=task_id, rel_paths=[rel])


def _content(i):
    """Distinct content with a distinct length per round (defeats dedup)."""
    return f'round-{i}-' + 'x' * i


def test_below_threshold_is_noop(proj, monkeypatch):
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 100)
    for i in range(10):
        _edit_and_snapshot(proj, 'a.txt', _content(i))
    res = store.compact_store(proj)
    assert res['snapshots_before'] == 10
    assert res['snapshots_after'] == 10
    assert res['blobs_removed'] == 0


def test_rotation_keeps_newest_n(proj, monkeypatch):
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 5)
    ids = [_edit_and_snapshot(proj, 'a.txt', _content(i)) for i in range(12)]
    res = store.compact_store(proj)
    assert res['snapshots_before'] == 12
    assert res['snapshots_after'] == 5
    surviving = [s['id'] for s in store.iter_snapshots(proj)]
    assert surviving == ids[-5:]  # newest 5, chronological order preserved


def test_v1_blob_always_preserved(proj, monkeypatch):
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 3)
    for i in range(10):
        _edit_and_snapshot(proj, 'a.txt', _content(i))
    store.compact_store(proj)
    # v1 must survive so "rewind to start of session" still works.
    assert store.read_blob(proj, 'a.txt', 1) is not None


def test_latest_version_preserved_after_compaction(proj, monkeypatch):
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 3)
    for i in range(10):
        _edit_and_snapshot(proj, 'a.txt', _content(i))
    tracked = store.load_tracked(proj)
    latest = tracked['a.txt']['latest_version']
    store.compact_store(proj)
    # The current on-disk state's blob must never be GC'd.
    assert store.read_blob(proj, 'a.txt', latest) is not None


def test_orphan_blobs_removed_but_referenced_kept(proj, monkeypatch):
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 2)
    for i in range(8):
        _edit_and_snapshot(proj, 'a.txt', _content(i))
    res = store.compact_store(proj)
    # Some middle versions are no longer pinned by any surviving snapshot
    # (nor v1, nor latest) → they must be reclaimed.
    assert res['blobs_removed'] > 0
    # Every version referenced by a surviving snapshot must still load.
    for snap in store.iter_snapshots(proj):
        for rel, v in (snap.get('files') or {}).items():
            if isinstance(v, int) and v > 0:
                assert store.read_blob(proj, rel, v) is not None


def test_rewind_still_works_after_compaction(proj, monkeypatch):
    import os
    monkeypatch.setattr(store, 'MAX_SNAPSHOTS', 3)
    contents = [_content(i) for i in range(10)]
    ids = [_edit_and_snapshot(proj, 'a.txt', c) for c in contents]
    store.compact_store(proj)
    # Undo the most recent round; file should revert to the prior round's
    # content (the second-to-last write).
    api.rewind_to(proj, ids[-1])
    with open(os.path.join(proj, 'a.txt'), encoding='utf-8') as f:
        assert f.read() == contents[-2]


def test_maybe_compact_gate_only_fires_on_interval(proj, monkeypatch):
    calls = []
    monkeypatch.setattr(store, 'COMPACT_CHECK_EVERY', 5)
    monkeypatch.setattr(store, 'compact_store', lambda base: calls.append(base))
    for n in range(1, 11):
        store.maybe_compact_store(proj, n)
    assert len(calls) == 2  # fired at n=5 and n=10 only
