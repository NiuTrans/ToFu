"""Tests for write-freshness token persistence across restarts
(``lib/write_freshness.py`` save_snapshot / load_snapshot, pt_1bbd3cc82eb44ddc).

The store is in-memory: any restart used to wipe every token and leave the
gate fail-open until each conversation's next read — the auto-restart
watcher made that window recur on every HEAD move. The snapshot carries
the small LRU across a restart. Replay preserves EXISTENCE ("this conv
demonstrably read/wrote this file"), not freshness across downtime: the
first is_stale after replay re-fingerprints the file, so downtime edits
are still caught (refuse → re-read — the safe direction).
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Fresh store + snapshot redirected to a tmp file (never real data/)."""
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    from lib import write_freshness as wf
    wf._reset_for_tests()
    snap = tmp_path / 'snap.json'
    monkeypatch.setattr(wf, '_snapshot_path', lambda: str(snap))
    yield {'snap': snap, 'wf': wf}
    wf._reset_for_tests()


@pytest.fixture
def target(tmp_path):
    f = tmp_path / 'a.py'
    f.write_text('def foo():\n    return 1\n')
    return str(f)


@pytest.mark.unit
def test_roundtrip_preserves_tokens_and_conv_namespacing(_isolate, target):
    wf = _isolate['wf']
    wf.record('convA', target)
    assert wf.save_snapshot() is True
    assert _isolate['snap'].is_file()
    wf._reset_for_tests()  # ← the restart: in-memory store gone
    assert wf.is_stale('convA', target) is False  # no token → blind already
    loaded = wf.load_snapshot()
    assert loaded == 1
    # convA's knowledge survived; convB is still its own namespace.
    assert wf.is_stale('convA', target) is False
    with open(target, 'a', encoding='utf-8') as f:
        f.write('# sibling\n')
    assert wf.is_stale('convA', target) is True
    assert wf.is_stale('convB', target) is False


@pytest.mark.unit
def test_replayed_token_catches_same_tick_same_size_edit(_isolate, target):
    """THE ticket's money test: after a restart + replay, the content
    fingerprint still catches a same-1-second-tick, same-byte-count edit
    made while the server was DOWN (1s-granularity FUSE mtime is blind to
    it; the replayed blake2b is not)."""
    wf = _isolate['wf']
    assert os.path.getsize(target) == len('def foo():\n    return 2\n')
    aligned = 1_700_000_000_000_000_000
    os.utime(target, ns=(aligned, aligned))
    wf.record('convA', target)
    wf.save_snapshot()
    wf._reset_for_tests()
    # ── server down; a same-tick, same-length, different-content edit ──
    with open(target, 'w', encoding='utf-8') as f:
        f.write('def foo():\n    return 2\n')
    os.utime(target, ns=(aligned, aligned))  # mtime identical to pre-restart
    # ── server up; replay ──
    assert wf.load_snapshot() == 1
    st = os.stat(target)
    assert (st.st_mtime_ns, st.st_size) == (aligned, len('def foo():\n    return 1\n'))
    assert wf.is_stale('convA', target) is True  # caught, despite FUSE blindness


@pytest.mark.unit
def test_load_missing_corrupt_or_wrong_version_is_noop(_isolate):
    wf = _isolate['wf']
    snap = _isolate['snap']
    assert wf.load_snapshot() == 0  # missing
    snap.write_text('not json at all {{{')
    assert wf.load_snapshot() == 0  # corrupt
    snap.write_text(json.dumps({'version': 999, 'tokens': []}))
    assert wf.load_snapshot() == 0  # wrong version
    snap.write_text(json.dumps({'version': 1, 'tokens': 'not-a-list'}))
    assert wf.load_snapshot() == 0  # malformed payload
    # Store untouched throughout.
    assert wf.is_stale('convA', '/nonexistent') is False


@pytest.mark.unit
def test_neuter_no_replay_reopens_the_window(_isolate, target):
    """NEUTER / causal proof: skip load_snapshot (the amputated wiring) and
    the post-restart stale write passes silently — the exact fail-open
    window this epic closes. With replay, the same check refuses."""
    wf = _isolate['wf']
    wf.record('convA', target)
    wf.save_snapshot()
    wf._reset_for_tests()
    with open(target, 'a', encoding='utf-8') as f:
        f.write('# sibling wrote while server was down\n')
    # Amputated: no load_snapshot → no token → fail-open (window OPEN).
    assert wf.is_stale('convA', target) is False
    # Wired: replay → token exists → stale (window CLOSED).
    assert wf.load_snapshot() == 1
    assert wf.is_stale('convA', target) is True


@pytest.mark.unit
def test_gate_disabled_skips_persistence(_isolate, monkeypatch):
    wf = _isolate['wf']
    monkeypatch.setenv('TOFU_WRITE_FRESHNESS_GATE', '0')
    wf.record('convA', '/tmp/x')
    assert wf.save_snapshot() is False
    assert not _isolate['snap'].exists()
    assert wf.load_snapshot() == 0


@pytest.mark.unit
def test_oversized_snapshot_keeps_newest(_isolate, target):
    wf = _isolate['wf']
    snap = _isolate['snap']
    items = [{'conv': 'convA', 'path': f'/tmp/f{i}.py',
              'fp': ['c', 1, f'{i:032x}'], 'ts': float(i)}
             for i in range(wf._MAX_ENTRIES + 50)]
    snap.write_text(json.dumps({'version': 1, 'tokens': items}))
    loaded = wf.load_snapshot()
    assert loaded == wf._MAX_ENTRIES
    # The NEWEST entries survive the cap.
    with wf._lock:
        keys = {k for k in wf._tokens}
    assert ('convA', f'/tmp/f{wf._MAX_ENTRIES + 49}.py') in keys
    assert ('convA', '/tmp/f0.py') not in keys


@pytest.mark.unit
def test_save_failure_never_raises(_isolate, monkeypatch):
    wf = _isolate['wf']
    wf.record('convA', '/tmp/x.py')
    monkeypatch.setattr(wf, '_snapshot_path',
                        lambda: '/proc/1/definitely/not/writable/snap.json')
    assert wf.save_snapshot() is False  # logged, not raised


@pytest.mark.unit
def test_reexec_saves_snapshot_before_execv(monkeypatch):
    """Wiring pin: _perform_server_reexec must save the snapshot BEFORE the
    execv (atexit does NOT run on execv — the signal path's atexit hook
    cannot cover restarts)."""
    from routes.api_v1 import update as upd
    import lib.shutdown_marker as sm
    calls = []
    monkeypatch.setattr('lib.write_freshness.save_snapshot',
                        lambda: calls.append('save') or True)
    monkeypatch.setattr(sm, 'mark_clean', lambda *a, **k: None)
    monkeypatch.setattr(upd, '_close_inheritable_listen_sockets', lambda: None)
    monkeypatch.setattr(upd.os, 'execv', lambda *a: calls.append('execv'))
    assert upd._perform_server_reexec('test') is True
    assert calls == ['save', 'execv']
