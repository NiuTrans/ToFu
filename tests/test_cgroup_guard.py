#!/usr/bin/env python3
"""Tests for lib.cgroup_guard — the shared-cgroup memory-pressure defenses.

Covers the three defenses (① startup self-check, ② runtime relief monitor,
③ large-request headroom guard), the graceful NO-OP when the cgroup is
unreadable (bare metal / restricted sandbox), and a NEUTER reverse-control per
defense proving the pressure signal is load-bearing.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_cgroup_guard.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

pytestmark = pytest.mark.unit

_GIB = 1 << 30


@pytest.fixture()
def guard(monkeypatch):
    import lib.cgroup_guard as cg
    # Neutralise env so defaults apply unless a test sets them.
    for k in ('TOFU_CGROUP_WARN_PCT', 'TOFU_CGROUP_RELIEF_PCT',
              'TOFU_CGROUP_REQUEST_PCT', 'TOFU_CGROUP_POLL_SEC',
              'TOFU_CGROUP_REQUEST_MIN_BYTES', 'TOFU_CGROUP_REQUEST_GUARD'):
        monkeypatch.delenv(k, raising=False)
    return cg


def _set_pressure(cg, monkeypatch, pct, swap=0):
    limit = 200 * _GIB
    monkeypatch.setattr(cg, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(cg, 'mem_usage_bytes', lambda: int(limit * pct / 100.0))
    monkeypatch.setattr(cg, 'swap_total_bytes', lambda: swap)


# ── graceful no-op when cgroup unreadable ──

def test_unreadable_cgroup_is_total_noop(guard, monkeypatch):
    """Bare metal / restricted: readers return None → every defense no-ops, no raise."""
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: None)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: None)
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: None)
    assert guard.pressure() is None
    assert guard.startup_self_check() is None          # ①
    assert guard.run_monitor_once() is None            # ②
    assert guard.start_monitor() is False              # ② thread not started
    ok, reason = guard.check_request_headroom('x', approx_bytes=50 * 1024 * 1024)  # ③
    assert ok is True and reason is None


# ── ① startup self-check ──

def test_self_check_warns_when_full_and_no_swap(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=99.7, swap=0)
    snap = guard.startup_self_check()
    assert snap is not None
    assert snap['pct'] >= 90.0


def test_self_check_silent_when_roomy(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=40.0, swap=0)
    assert guard.startup_self_check() is None


def test_self_check_neuter_swap_present_suppresses(guard, monkeypatch):
    """NEUTER: same near-full usage but swap>0 → no CRITICAL (swap signal load-bearing)."""
    _set_pressure(guard, monkeypatch, pct=99.7, swap=8 * _GIB)
    assert guard.startup_self_check() is None


# ── ② runtime relief monitor ──

def test_monitor_relieves_over_threshold(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=95.0, swap=0)
    calls = {}
    monkeypatch.setattr(guard, 'relieve_memory',
                        lambda reason: calls.setdefault('r', reason) or {'reason': reason})
    out = guard.run_monitor_once()
    assert out is not None
    assert 'r' in calls  # relief was invoked


def test_monitor_neuter_below_threshold_no_relief(guard, monkeypatch):
    """NEUTER: usage below relief threshold → relieve_memory NOT called."""
    _set_pressure(guard, monkeypatch, pct=80.0, swap=0)
    called = {'n': 0}
    monkeypatch.setattr(guard, 'relieve_memory',
                        lambda reason: called.__setitem__('n', called['n'] + 1))
    assert guard.run_monitor_once() is None
    assert called['n'] == 0


def test_relieve_clears_caches_and_trims(guard, monkeypatch):
    """relieve_memory drops registered TTLCaches and calls malloc_trim."""
    from lib.ttl_cache import TTLCache, clear_all_caches  # noqa: F401
    c = TTLCache(ttl=60, name='cg_relief_probe')
    c.set('a', 1)
    c.set('b', 2)
    _set_pressure(guard, monkeypatch, pct=93.0, swap=0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    stats = guard.relieve_memory('test')
    assert stats['dropped'] >= 2  # our two entries were cleared
    assert len(c) == 0


# ── ③ large-request headroom guard ──

def test_request_guard_refuses_when_critical(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=99.0, swap=0)
    # relief cannot help in this scenario (still critical after trim)
    monkeypatch.setattr(guard, 'relieve_memory', lambda reason: {'reason': reason})
    ok, reason = guard.check_request_headroom('conv=abc', approx_bytes=40 * 1024 * 1024)
    assert ok is False
    assert 'refusing' in reason


def test_request_guard_passes_small_body_even_when_full(guard, monkeypatch):
    """Small bodies always pass — the guard only gates large ones."""
    _set_pressure(guard, monkeypatch, pct=99.9, swap=0)
    ok, reason = guard.check_request_headroom('conv=abc', approx_bytes=1000)
    assert ok is True and reason is None


def test_request_guard_passes_when_roomy(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=50.0, swap=0)
    ok, reason = guard.check_request_headroom('conv=abc', approx_bytes=40 * 1024 * 1024)
    assert ok is True and reason is None


def test_request_guard_relief_rescues(guard, monkeypatch):
    """If a trim drops usage back under the threshold, the request proceeds."""
    seq = iter([99.0, 50.0])  # before trim critical, after trim fine

    def fake_pressure():
        pct = next(seq)
        return {'limit': 200 * _GIB, 'usage': int(200 * _GIB * pct / 100),
                'pct': pct, 'swap': 0}
    monkeypatch.setattr(guard, 'pressure', fake_pressure)
    monkeypatch.setattr(guard, 'relieve_memory', lambda reason: {'reason': reason})
    ok, reason = guard.check_request_headroom('conv=abc', approx_bytes=40 * 1024 * 1024)
    assert ok is True and reason is None


def test_request_guard_neuter_ignoring_pressure_would_pass(guard, monkeypatch):
    """NEUTER: if pressure() returned None (signal ignored), the critical case passes.

    Proves the pressure reading is load-bearing for the refusal.
    """
    monkeypatch.setattr(guard, 'pressure', lambda: None)
    ok, reason = guard.check_request_headroom('conv=abc', approx_bytes=40 * 1024 * 1024)
    assert ok is True and reason is None


def test_approx_body_bytes_counts_text(guard):
    body = {'messages': [
        {'role': 'user', 'content': 'x' * 1000},
        {'role': 'assistant', 'content': [{'type': 'text', 'text': 'y' * 500},
                                          {'type': 'image', 'source': {}}]},
    ]}
    n = guard.approx_body_bytes(body)
    assert n >= 1500  # 1000 + 500 text, plus fixed cost for the image part


def test_approx_body_bytes_handles_garbage(guard):
    assert guard.approx_body_bytes(None) == 0
    assert guard.approx_body_bytes(42) == 0


# ── page-cache relief (fadvise) ──

def test_fadvise_real_file_returns_size(guard, tmp_path):
    p = tmp_path / 'big.log'
    p.write_bytes(b'x' * 100_000)
    assert guard.fadvise_dontneed(str(p)) == 100_000


def test_fadvise_missing_file_is_noop(guard, tmp_path):
    assert guard.fadvise_dontneed(str(tmp_path / 'nope.log')) == 0


def test_drop_files_cache_respects_floor(guard, tmp_path):
    big = tmp_path / 'big.log'
    big.write_bytes(b'x' * 100_000)
    small = tmp_path / 'small.log'
    small.write_bytes(b'x' * 10)
    stats = guard.drop_files_cache([str(big), str(small)], min_bytes=1000)
    assert stats['files'] == 1
    assert stats['bytes'] == 100_000


def test_drop_logs_cache_picks_log_files(guard, tmp_path):
    """Only *.log* files above the size floor are dropped."""
    (tmp_path / 'big.log').write_bytes(b'x' * (2 << 20))
    (tmp_path / 'small.log').write_bytes(b'y' * 100)
    (tmp_path / 'notalog.txt').write_bytes(b'w' * (2 << 20))
    stats = guard.drop_logs_cache(str(tmp_path))
    assert stats['files'] == 1 and stats['bytes'] == (2 << 20)


def test_relieve_includes_log_page_drop(guard, monkeypatch, tmp_path):
    """relieve_memory must call drop_logs_cache and report its bytes."""
    _set_pressure(guard, monkeypatch, pct=93.0, swap=0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    called = {'n': 0}

    def fake_drop(log_dir='logs'):
        called['n'] += 1
        return {'files': 3, 'bytes': 12_000_000}
    monkeypatch.setattr(guard, 'drop_logs_cache', fake_drop)
    stats = guard.relieve_memory('test')
    assert called['n'] == 1
    assert stats['log_pages_bytes'] == 12_000_000


def test_relieve_neuter_without_log_drop(guard, monkeypatch):
    """NEUTER: TOFU_CGROUP_DROP_LOGS=0 must skip the page-cache drop entirely."""
    _set_pressure(guard, monkeypatch, pct=93.0, swap=0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setenv('TOFU_CGROUP_DROP_LOGS', '0')
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': 1 / 0)  # must never be called
    stats = guard.relieve_memory('test')
    assert stats['log_pages_bytes'] == 0


# ── pressure journal ──

def test_pressure_journal_writes_and_rings(guard, monkeypatch, tmp_path):
    jpath = tmp_path / 'journal.log'
    monkeypatch.setattr(guard, '_JOURNAL_PATH', str(jpath))
    monkeypatch.setattr(guard, '_JOURNAL_MAX_BYTES', 400)
    monkeypatch.setattr(guard, '_top_rss_processes', lambda n=3: [])
    monkeypatch.setattr(guard, '_read_memory_stat',
                        lambda: {'cache': 1 << 30, 'rss': 2 << 30, 'kmem': 3 << 30})
    snap = {'pct': 50.0, 'usage': 100 << 30, 'limit': 200 << 30}
    for _ in range(30):
        assert guard.write_pressure_journal(snap) is True
    import json as _json
    lines = jpath.read_text().strip().split('\n')
    assert len(lines) < 30                      # ring bound kept the tail only
    rec = _json.loads(lines[-1])
    assert rec['pct'] == 50.0
    assert rec['cache_gib'] == 1.0 and rec['kmem_gib'] == 3.0
    assert os.path.getsize(jpath) <= 400 + 200  # bounded (last append may exceed slightly)


def test_pressure_journal_env_off(guard, monkeypatch, tmp_path):
    jpath = tmp_path / 'journal.log'
    monkeypatch.setattr(guard, '_JOURNAL_PATH', str(jpath))
    monkeypatch.setenv('TOFU_CGROUP_JOURNAL', '0')
    assert guard.write_pressure_journal({'pct': 99.0, 'usage': 1, 'limit': 2}) is False
    assert not jpath.exists()


def test_monitor_tick_journals_and_watches(guard, monkeypatch):
    """run_monitor_once must journal + oom-watch EVERY tick (even roomy ones)."""
    _set_pressure(guard, monkeypatch, pct=50.0, swap=0)
    calls = {'j': 0, 'o': 0}
    monkeypatch.setattr(guard, 'write_pressure_journal', lambda snap: calls.__setitem__('j', calls['j'] + 1) or True)
    monkeypatch.setattr(guard, 'check_oom_kill_count', lambda: calls.__setitem__('o', calls['o'] + 1) or False)
    assert guard.run_monitor_once() is None     # roomy → no relief
    assert calls['j'] == 1 and calls['o'] == 1


# ── oom_kill witness ──

def test_oom_kill_watch_fires_on_increment(guard, monkeypatch, tmp_path):
    ctl = tmp_path / 'oom_control'
    ctl.write_text('oom_kill_disable 0\nunder_oom 0\noom_kill 2\n')
    monkeypatch.setattr(guard, '_OOM_CONTROL_PATH', str(ctl))
    monkeypatch.setattr(guard, '_last_oom_kill_count', 1)
    fired = []
    monkeypatch.setattr(guard, 'audit_log', lambda *a, **k: fired.append((a, k)))
    assert guard.check_oom_kill_count() is True
    assert fired  # audit_log called
    assert guard._last_oom_kill_count == 2


def test_oom_kill_watch_neuter_no_increment(guard, monkeypatch, tmp_path):
    """NEUTER: unchanged counter must NOT fire (no false alarms)."""
    ctl = tmp_path / 'oom_control'
    ctl.write_text('oom_kill_disable 0\nunder_oom 0\noom_kill 2\n')
    monkeypatch.setattr(guard, '_OOM_CONTROL_PATH', str(ctl))
    monkeypatch.setattr(guard, '_last_oom_kill_count', 2)
    monkeypatch.setattr(guard, 'audit_log',
                        lambda *a, **k: 1 / 0)   # must never be called
    assert guard.check_oom_kill_count() is False


def test_oom_kill_watch_first_read_baselines(guard, monkeypatch, tmp_path):
    """First read (prev=None) only baselines — never fires on boot."""
    ctl = tmp_path / 'oom_control'
    ctl.write_text('oom_kill_disable 0\nunder_oom 0\noom_kill 7\n')
    monkeypatch.setattr(guard, '_OOM_CONTROL_PATH', str(ctl))
    monkeypatch.setattr(guard, '_last_oom_kill_count', None)
    monkeypatch.setattr(guard, 'audit_log', lambda *a, **k: 1 / 0)
    assert guard.check_oom_kill_count() is False
    assert guard._last_oom_kill_count == 7


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
