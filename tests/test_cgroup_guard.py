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


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
