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
def guard(monkeypatch, tmp_path):
    import lib.cgroup_guard as cg
    from lib.observability import reset_for_tests
    reset_for_tests()
    # Monitor tests drive synthetic 40%/80%/99% snapshots. Never append those
    # fixtures to the live incident journal in a concurrently running checkout.
    monkeypatch.setattr(cg, '_JOURNAL_PATH', str(tmp_path / 'pressure.log'))
    # Neutralise env so defaults apply unless a test sets them.
    for k in ('TOFU_CGROUP_WARN_PCT', 'TOFU_CGROUP_RELIEF_PCT',
              'TOFU_CGROUP_REQUEST_PCT', 'TOFU_CGROUP_POLL_SEC',
              'TOFU_CGROUP_REQUEST_MIN_BYTES', 'TOFU_CGROUP_REQUEST_GUARD',
              'TOFU_CGROUP_RELIEF_COOLDOWN_SEC'):
        monkeypatch.delenv(k, raising=False)
    for k in ('TOFU_PROCESS_RSS_RELIEF_MB',
              'TOFU_PROCESS_RSS_COOLDOWN_SEC',
              'TOFU_PROCESS_RSS_RECYCLE_MB'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cg, '_last_process_rss_relief_at', 0.0)
    monkeypatch.setattr(cg, '_process_rss_recycle_requested', False)
    monkeypatch.setattr(cg, '_ineffective_reliefs', 0)
    monkeypatch.setattr(cg, '_ineffective_escalated', False)
    monkeypatch.setattr(cg, '_relief_suppressed_until', 0.0)
    monkeypatch.setattr(cg, '_ineffective_backoff_exponent', 0)
    yield cg
    reset_for_tests()


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


def test_bare_metal_personal_rss_defaults_leave_room_for_the_desktop(
        guard, monkeypatch):
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: None)
    defaults = {
        'TOFU_PROCESS_RSS_RELIEF_MB': 2048,
        'TOFU_PROCESS_RSS_RECYCLE_MB': 3072,
    }
    monkeypatch.setattr(
        guard, 'deployment_resource_default',
        lambda name, _environment: defaults[name])

    assert guard._process_rss_relief_limit_bytes() == 2 * _GIB
    assert guard._process_rss_recycle_limit_bytes() == 3 * _GIB


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


def test_monitor_cools_down_after_ineffective_shared_relief(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=99.0, swap=0)
    monkeypatch.setattr(guard, '_relief_suppressed_until', float('inf'))
    monkeypatch.setattr(
        guard, 'relieve_memory',
        lambda _reason: (_ for _ in ()).throw(AssertionError('must cool down')))

    assert guard.run_monitor_once() is None


def test_ineffective_probe_renews_cooldown_after_the_first_escalation(
        guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=99.0, swap=0)
    monkeypatch.setattr(guard, '_ineffective_reliefs', guard._INEFFECTIVE_LIMIT)
    monkeypatch.setattr(guard, '_ineffective_escalated', True)
    monkeypatch.setattr(guard, '_relief_suppressed_until', 0.0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: False)
    monkeypatch.setattr(guard, 'drop_logs_cache', lambda: {
        'files': 0, 'bytes': 0, 'skipped': 0})

    guard.relieve_memory('cooldown probe')

    assert guard._relief_suppressed_until > 0


def test_ineffective_probes_back_off_exponentially_to_one_hour(
        guard, monkeypatch):
    """A shared-cgroup squeeze must not flush hot caches every ten minutes."""
    import time

    _set_pressure(guard, monkeypatch, pct=99.0, swap=0)
    monkeypatch.setenv('TOFU_CGROUP_RELIEF_COOLDOWN_SEC', '600')
    monkeypatch.setattr(guard, '_ineffective_reliefs', guard._INEFFECTIVE_LIMIT)
    monkeypatch.setattr(guard, '_ineffective_escalated', True)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: False)
    monkeypatch.setattr(guard, 'drop_logs_cache', lambda: {
        'files': 0, 'bytes': 0, 'skipped': 0})
    now = [1000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])

    observed = []
    for _ in range(5):
        guard.relieve_memory('adaptive probe')
        observed.append(guard._relief_suppressed_until - now[0])
        now[0] += observed[-1]

    assert observed == [600.0, 1200.0, 2400.0, 3600.0, 3600.0]


def test_monitor_rearms_backoff_after_pressure_episode_ends(
        guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=80.0, swap=0)
    monkeypatch.setattr(guard, '_ineffective_reliefs', 8)
    monkeypatch.setattr(guard, '_ineffective_escalated', True)
    monkeypatch.setattr(guard, '_relief_suppressed_until', float('inf'))
    monkeypatch.setattr(guard, '_ineffective_backoff_exponent', 3)

    assert guard.run_monitor_once() is None
    assert guard._ineffective_reliefs == 0
    assert guard._ineffective_escalated is False
    assert guard._relief_suppressed_until == 0.0
    assert guard._ineffective_backoff_exponent == 0


def test_process_rss_relief_bypasses_aggregate_backoff(guard, monkeypatch):
    """External pressure backoff must not disable process-owned RSS relief."""
    _set_pressure(guard, monkeypatch, pct=99.0, swap=0)
    monkeypatch.setenv('TOFU_PROCESS_RSS_RELIEF_MB', '1024')
    monkeypatch.setattr(guard, '_relief_suppressed_until', float('inf'))
    rss = iter([5 * _GIB, 2 * _GIB])
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: next(rss))
    monkeypatch.setattr(guard, 'write_pressure_journal', lambda _snap: True)
    monkeypatch.setattr(guard, 'check_oom_kill_count', lambda: False)
    monkeypatch.setattr(
        guard, 'relieve_memory', lambda reason: {'reason': reason})

    result = guard.run_monitor_once()

    assert result['process_rss_before'] == 5 * _GIB
    assert result['process_rss_after'] == 2 * _GIB
    assert result['process_rss_reclaimed_bytes'] == 3 * _GIB


def test_monitor_relieves_process_rss_even_when_cgroup_is_roomy(
        guard, monkeypatch):
    """A large Tofu process must not hide behind a roomy shared cgroup."""
    _set_pressure(guard, monkeypatch, pct=40.0, swap=0)
    monkeypatch.setenv('TOFU_PROCESS_RSS_RELIEF_MB', '1024')
    rss = iter([5 * _GIB, 2 * _GIB])
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: next(rss))
    monkeypatch.setattr(guard, 'write_pressure_journal', lambda _snap: True)
    monkeypatch.setattr(guard, 'check_oom_kill_count', lambda: False)
    called = []
    monkeypatch.setattr(
        guard, 'relieve_memory',
        lambda reason: called.append(reason) or {'reason': reason})
    out = guard.run_monitor_once()
    assert out['process_rss_before'] == 5 * _GIB
    assert out['process_rss_after'] == 2 * _GIB
    assert called and 'process RSS' in called[0]


def test_process_rss_relief_is_cooldown_bounded(guard, monkeypatch):
    monkeypatch.setenv('TOFU_PROCESS_RSS_RELIEF_MB', '1024')
    monkeypatch.setenv('TOFU_PROCESS_RSS_COOLDOWN_SEC', '300')
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: 5 * _GIB)
    called = []
    monkeypatch.setattr(
        guard, 'relieve_memory',
        lambda reason: called.append(reason) or {'reason': reason})
    assert guard._maybe_relieve_process_rss(now=1000.0) is not None
    assert guard._maybe_relieve_process_rss(now=1100.0) is None
    assert guard._maybe_relieve_process_rss(now=1301.0) is not None
    assert len(called) == 2


def test_process_rss_hard_ceiling_requests_one_recycle_after_failed_relief(
        guard, monkeypatch):
    monkeypatch.setenv('TOFU_PROCESS_RSS_RELIEF_MB', '1024')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '2048')
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: 3 * _GIB)
    monkeypatch.setattr(guard, 'relieve_memory', lambda _reason: {})
    requested = []

    first = guard._maybe_relieve_process_rss(
        now=1000.0, recycle_callback=requested.append)
    second = guard._maybe_relieve_process_rss(
        now=1400.0, recycle_callback=requested.append)

    assert first['process_rss_recycle_requested'] is True
    assert len(requested) == 1
    assert 'hard ceiling' in requested[0]
    assert second is not None
    assert len(requested) == 1


def test_effective_process_rss_relief_avoids_recycle(guard, monkeypatch):
    monkeypatch.setenv('TOFU_PROCESS_RSS_RELIEF_MB', '1024')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '2048')
    rss = iter([3 * _GIB, 1 * _GIB])
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: next(rss))
    monkeypatch.setattr(guard, 'relieve_memory', lambda _reason: {})
    requested = []

    result = guard._maybe_relieve_process_rss(
        now=1000.0, recycle_callback=requested.append)

    assert result['process_rss_after'] == 1 * _GIB
    assert requested == []


def test_default_process_rss_thresholds_adapt_to_small_cgroup(
        guard, monkeypatch):
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: 4 * _GIB)

    assert guard._process_rss_relief_limit_bytes() == 2 * _GIB
    assert guard._process_rss_recycle_limit_bytes() == int(2.8 * _GIB)


def test_explicit_process_rss_thresholds_override_cgroup_fraction(
        guard, monkeypatch):
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: 4 * _GIB)
    monkeypatch.setenv('TOFU_PROCESS_RSS_RELIEF_MB', '3072')
    monkeypatch.setenv('TOFU_PROCESS_RSS_RECYCLE_MB', '3584')

    assert guard._process_rss_relief_limit_bytes() == 3 * _GIB
    assert guard._process_rss_recycle_limit_bytes() == int(3.5 * _GIB)


def test_relieve_clears_caches_and_trims(guard, monkeypatch):
    """relieve_memory drops registered TTLCaches and calls malloc_trim."""
    from lib.browser import _resolve
    from lib.mcp import tool_search
    from lib.ttl_cache import TTLCache, clear_all_caches  # noqa: F401
    from lib.token_counter import usage_cache

    c = TTLCache(ttl=60, name='cg_relief_probe')
    c.set('a', 1)
    c.set('b', 2)
    usage_cache._reset_usage_cache_for_tests()
    usage_cache.record_usage(
        'pressure-a', prompt_tokens=1, model='gpt-5.6-sol',
        message_count=1, messages=[{'role': 'user', 'content': 'a'}])
    usage_cache.record_usage(
        'pressure-b', prompt_tokens=1, model='gpt-5.6-sol',
        message_count=1, messages=[{'role': 'user', 'content': 'b'}])
    _resolve.remember_work_tab(('41', 'pressure-a'), 7)
    _resolve.remember_work_tab(('42', 'pressure-b'), 9)
    tool_search.clear_tool_search_caches()
    tool_search.build_catalog_index([])
    tool_search.record_mcp_tool_used(
        'pressure-tool-search', 'mcp__docs__read')
    _set_pressure(guard, monkeypatch, pct=93.0, swap=0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    try:
        stats = guard.relieve_memory('test')
        assert stats['dropped'] >= 8
        assert len(c) == 0
        assert usage_cache.usage_cache_snapshot()['entries'] == 0
        assert _resolve.current_work_tab(('41', 'pressure-a')) is None
        assert _resolve.current_work_tab(('42', 'pressure-b')) is None
        assert tool_search.catalog_cache_stats()['indexes'] == 0
        assert tool_search.catalog_cache_stats()['tasks'] == 0
    finally:
        _resolve.clear_work_tab_cache()
        tool_search.clear_tool_search_caches()
        usage_cache._reset_usage_cache_for_tests()


# ── ③ large-request headroom guard ──

def test_request_guard_refuses_when_critical(guard, monkeypatch):
    # 200 MiB free cannot cover the 320 MiB peak envelope for a 40 MiB body.
    _set_pressure(guard, monkeypatch, pct=99.9, swap=0)
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


def test_request_guard_enforces_process_soft_rss_without_cgroup_pressure(
        guard, monkeypatch):
    monkeypatch.setattr(
        guard, '_process_rss_relief_limit_bytes',
        lambda: 128 * 1024 * 1024)
    monkeypatch.setattr(
        guard, '_self_rss_bytes', lambda: 100 * 1024 * 1024)
    monkeypatch.setattr(guard, '_maybe_relieve_process_rss', lambda: None)
    monkeypatch.setattr(
        guard, 'pressure',
        lambda: pytest.fail(
            'process-local refusal must not need cgroup pressure'))

    ok, reason = guard.check_request_headroom(
        'conv=local-rss', approx_bytes=1000)

    assert ok is False
    assert 'process RSS' in reason
    assert 'soft ceiling' in reason


def test_process_rss_admission_reserves_one_declared_task_envelope(
        guard, monkeypatch):
    monkeypatch.setenv('TOFU_TASK_RSS_RESERVE_MB', '1024')
    monkeypatch.setattr(
        guard, '_process_rss_recycle_limit_bytes',
        lambda: 6 * 1024 * 1024 * 1024)
    monkeypatch.setattr(
        guard, '_self_rss_bytes',
        lambda: int(5.25 * 1024 * 1024 * 1024))

    snapshot = guard.process_rss_admission_snapshot()

    assert snapshot is not None
    assert snapshot['allowed'] is False
    assert snapshot['reserve_bytes'] == 1024 * 1024 * 1024


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
    ok, reason = guard.check_request_headroom('conv=abc', approx_bytes=400 * 1024 * 1024)
    assert ok is True and reason is None


def test_request_guard_allows_small_request_with_large_absolute_headroom(
        guard, monkeypatch):
    """A huge shared cgroup's percentage is not a refusal by itself.

    Production rejected a 2.3 MB request at 99.3% of 220 GiB even though about
    1.5 GiB remained and Tofu itself used under 40 MiB. That cannot protect the
    process from sibling pressure; it only kills an otherwise safe turn.
    """
    limit = 220 * _GIB
    monkeypatch.setattr(guard, 'pressure', lambda: {
        'limit': limit,
        'usage': int(limit * 0.993),
        'pct': 99.3,
        'swap': 0,
    })
    relief_calls = []
    monkeypatch.setattr(
        guard, 'relieve_memory', lambda reason: relief_calls.append(reason))

    ok, reason = guard.check_request_headroom(
        'conv=production-regression', approx_bytes=2_300_000)

    assert ok is True and reason is None
    assert relief_calls == [], 'ample absolute headroom must not trigger trim'


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


def test_drop_files_cache_skips_unchanged_files(guard, tmp_path, monkeypatch):
    """Re-advising a byte-identical file is a guaranteed no-op — skip it.

    Its clean pages were dropped by the first advise, so the kernel has nothing
    left to reclaim. Measured live: re-advising the same 57 logs every 30s moved
    cgroup usage by +0.01 GiB (zero) while burning 57 syscalls a tick.
    """
    monkeypatch.setattr(guard, '_advised_state', {})
    p = tmp_path / 'big.log'
    p.write_bytes(b'x' * 100_000)
    first = guard.drop_files_cache([str(p)], min_bytes=1000)
    assert first['files'] == 1 and first['skipped'] == 0
    second = guard.drop_files_cache([str(p)], min_bytes=1000)
    assert second['files'] == 0 and second['skipped'] == 1
    assert second['bytes'] == 0


def test_drop_files_cache_readvises_after_the_file_grows(guard, tmp_path, monkeypatch):
    """Complement: a file that CHANGED has new cached pages and must be advised.

    Without this, "skip everything after the first pass" would satisfy the skip
    test while disabling the relief entirely — exactly the failure mode a live
    log file (appended to continuously) would hit.
    """
    monkeypatch.setattr(guard, '_advised_state', {})
    p = tmp_path / 'big.log'
    p.write_bytes(b'x' * 100_000)
    assert guard.drop_files_cache([str(p)], min_bytes=1000)['files'] == 1
    p.write_bytes(b'y' * 200_000)          # changed size + mtime
    again = guard.drop_files_cache([str(p)], min_bytes=1000)
    assert again['files'] == 1 and again['skipped'] == 0
    assert again['bytes'] == 200_000


def test_drop_logs_cache_picks_log_files(guard, tmp_path):
    """Only *.log* files above the size floor are dropped."""
    (tmp_path / 'big.log').write_bytes(b'x' * (2 << 20))
    (tmp_path / 'small.log').write_bytes(b'y' * 100)
    (tmp_path / 'notalog.txt').write_bytes(b'w' * (2 << 20))
    stats = guard.drop_logs_cache(str(tmp_path))
    assert stats['files'] == 1 and stats['bytes'] == (2 << 20)


def test_relieve_reports_measured_reclaim_not_apparent_size(guard, monkeypatch):
    """The reported reclaim must be MEASURED cgroup delta, not bytes advised.

    This test previously asserted ``log_pages_bytes == 12_000_000`` and passed
    while the guard was lying: that field is the APPARENT SIZE of the files
    fadvise was called on, i.e. an upper bound on what the kernel *might*
    reclaim, and it was printed in the log as though it were the amount freed.
    Measured live 2026-07-31: 406 reliefs reported a cumulative 4272 GB while
    cgroup usage fell 18.3 GiB in total (234x overstatement), and 367 of them
    moved usage by exactly 0.0%.

    So the load-bearing property is that a relief which frees NOTHING reports
    nothing, no matter how many bytes it advised.
    """
    limit = 200 * _GIB
    usage = int(limit * 0.93)
    # Usage identical before and after → nothing was actually reclaimed.
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: usage)
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: 0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': {'files': 57, 'bytes': 10_775_600_000,
                                                'skipped': 0})
    stats = guard.relieve_memory('test')
    assert stats['reclaimed_bytes'] == 0, (
        'relief freed nothing but reported %r reclaimed' % stats['reclaimed_bytes'])


def test_relieve_reports_real_reclaim_when_usage_actually_drops(guard, monkeypatch):
    """Complement: when usage genuinely falls, the measured delta is reported.

    Without this, "always report 0" would satisfy the test above — the guard
    would swap one lie for another.
    """
    limit = 200 * _GIB
    seq = iter([
        int(limit * 0.93),  # before
        int(limit * 0.92),  # after heap window
        int(limit * 0.90),  # after log window
    ])
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: next(seq))
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: 0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': {'files': 1, 'bytes': 100,
                                                'skipped': 0})
    stats = guard.relieve_memory('test')
    expected = int(limit * 0.93) - int(limit * 0.90)
    assert stats['reclaimed_bytes'] == expected


def test_relief_attributes_process_and_shared_windows_in_metrics(
        guard, monkeypatch):
    """Expose ownership evidence without pretending shared deltas are causal."""
    from lib import observability
    from lib import ttl_cache

    observability.reset_for_tests()
    before = {'limit': 1000, 'usage': 1000, 'pct': 100.0, 'swap': 0}
    after = {'limit': 1000, 'usage': 800, 'pct': 80.0, 'swap': 0}
    snapshots = iter([before, after])
    rss = iter([500, 350, 340])
    memory_stats = iter([
        {'cache': 600}, {'cache': 550}, {'cache': 400},
    ])
    monkeypatch.setattr(guard, 'pressure', lambda: next(snapshots))
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: 850)
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: next(rss))
    monkeypatch.setattr(guard, '_read_memory_stat', lambda: next(memory_stats))
    monkeypatch.setattr(ttl_cache, 'clear_all_caches', lambda: 3)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache', lambda: {
        'files': 2, 'bytes': 400, 'skipped': 1})

    stats = guard.relieve_memory('attribution test')

    assert stats['reclaimed_bytes'] == 200
    assert stats['heap_window_reclaimed_bytes'] == 150
    assert stats['log_window_reclaimed_bytes'] == 50
    assert stats['process_rss_reclaimed_bytes'] == 160
    assert stats['cgroup_cache_reclaimed_bytes'] == 200
    rendered = '\n'.join(observability.prometheus_lines())
    assert 'tofu_cgroup_relief_attempts_total 1.0' in rendered
    assert ('tofu_cgroup_relief_reclaimed_bytes_latest{source="process_rss"} '
            '160.0') in rendered
    assert ('tofu_cgroup_relief_reclaimed_bytes_latest{source="heap_window"} '
            '150.0') in rendered
    assert 'tofu_cgroup_relief_cache_entries_dropped_total 3.0' in rendered
    assert 'tofu_cgroup_relief_log_files_advised_total 2.0' in rendered
    assert 'tofu_cgroup_relief_duration_seconds_count 1' in rendered
    observability.reset_for_tests()


def test_relieve_still_invokes_the_log_page_drop(guard, monkeypatch):
    """relieve_memory must still call drop_logs_cache (the action, not the claim)."""
    _set_pressure(guard, monkeypatch, pct=93.0, swap=0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    called = {'n': 0}

    def fake_drop(log_dir='logs'):
        called['n'] += 1
        return {'files': 3, 'bytes': 12_000_000, 'skipped': 0}
    monkeypatch.setattr(guard, 'drop_logs_cache', fake_drop)
    guard.relieve_memory('test')
    assert called['n'] == 1


def _pin_usage(guard, monkeypatch, pct):
    """Freeze cgroup usage so every relief measures a zero reclaim."""
    limit = 200 * _GIB
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: int(limit * pct / 100.0))
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: 0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': {'files': 57, 'bytes': 10_775_600_000,
                                                'skipped': 0})
    monkeypatch.setattr(guard, '_ineffective_reliefs', 0)
    monkeypatch.setattr(guard, '_ineffective_escalated', False)


def test_persistently_ineffective_relief_escalates_once(guard, monkeypatch):
    """N consecutive zero-reclaim reliefs must escalate to CRITICAL exactly once.

    The observed failure was 341 reliefs emitting the SAME warning while usage
    climbed 92.1% -> 99.9%. Repeating a warning that says "relief ran" makes an
    unmitigated squeeze look handled; the operator needs to be told once, in
    different words, that no in-process action can fix it.

    Once, not every tick: an alert that repeats forever is the noise this epic
    is about.
    """
    _pin_usage(guard, monkeypatch, pct=99.9)
    fired = []
    monkeypatch.setattr(guard, 'audit_log',
                        lambda *a, **k: fired.append((a, k)))
    for _ in range(guard._INEFFECTIVE_LIMIT):
        guard.relieve_memory('monitor')
    assert len(fired) == 1, 'expected exactly one escalation, got %d' % len(fired)
    for _ in range(10):                      # keeps failing → still silent
        guard.relieve_memory('monitor')
    assert len(fired) == 1, 'escalation repeated — it must fire once'


def test_effective_relief_never_escalates(guard, monkeypatch):
    """NEUTER complement: when relief actually frees memory, no CRITICAL fires.

    Guards against "escalate unconditionally", which would pass the test above
    while crying wolf on a perfectly healthy system.
    """
    limit = 200 * _GIB
    pcts = iter([93.0, 92.0, 90.0] * 40)
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(guard, 'mem_usage_bytes',
                        lambda: int(limit * next(pcts) / 100.0))
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: 0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': {'files': 1, 'bytes': 100, 'skipped': 0})
    monkeypatch.setattr(guard, '_ineffective_reliefs', 0)
    monkeypatch.setattr(guard, '_ineffective_escalated', False)
    fired = []
    monkeypatch.setattr(guard, 'audit_log',
                        lambda *a, **k: fired.append((a, k)))
    last = None
    for _ in range(guard._INEFFECTIVE_LIMIT * 2):
        last = guard.relieve_memory('monitor')
    assert fired == [], 'effective relief must never emit the escalation audit'
    assert last is not None and last['reclaimed_bytes'] > 0
    assert guard._ineffective_reliefs == 0
    assert guard._ineffective_escalated is False
    assert guard._ineffective_backoff_exponent == 0


def test_noise_sized_reclaim_does_not_rearm_the_escalation(guard, monkeypatch):
    """A reclaim too small to matter must NOT count as effective relief.

    Found by running the real thing end-to-end, not by review: on the live
    shared cgroup the usage counter jitters, so reliefs returned
    39MB / 344KB / 180KB / 0 / 0 / 180KB… A strict ``reclaimed > 0`` test treats
    each of those crumbs as success and resets the streak, so the escalation
    could NEVER reach its limit — the alarm was structurally unable to fire on
    the exact system it was written for. That is the same defect class this epic
    exists to fix: an instrument that cannot report the condition it monitors.

    Materiality is therefore a fraction of the cgroup limit, not zero.
    """
    limit = 200 * _GIB
    # Alternate 0 and a 200KB crumb — under the 0.1% (=200MB) materiality bar.
    crumb = 200 * 1024
    usages = []
    for i in range(guard._INEFFECTIVE_LIMIT * 2):
        base = int(limit * 0.99)
        usages.append(base)
        usages.append(base)
        usages.append(base - (crumb if i % 2 else 0))
    seq = iter(usages)
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: next(seq))
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: 0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': {'files': 1, 'bytes': 1, 'skipped': 0})
    monkeypatch.setattr(guard, '_ineffective_reliefs', 0)
    monkeypatch.setattr(guard, '_ineffective_escalated', False)
    fired = []
    monkeypatch.setattr(guard, 'audit_log', lambda *a, **k: fired.append(k))
    for _ in range(guard._INEFFECTIVE_LIMIT * 2):
        guard.relieve_memory('monitor')
    assert fired, ('noise-sized reclaims kept resetting the streak — the '
                   'escalation can never fire under real cgroup jitter')


def test_a_single_effective_relief_rearms_the_escalation(guard, monkeypatch):
    """A recovery resets the streak, so a LATER squeeze can escalate again.

    Otherwise the one-shot latch would permanently silence the alarm after the
    first episode, and the second real squeeze would be invisible.
    """
    limit = 200 * _GIB
    state = {'usage': int(limit * 0.999)}
    monkeypatch.setattr(guard, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: state['usage'])
    monkeypatch.setattr(guard, 'swap_total_bytes', lambda: 0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache',
                        lambda log_dir='logs': {'files': 1, 'bytes': 1, 'skipped': 0})
    monkeypatch.setattr(guard, '_ineffective_reliefs', 0)
    monkeypatch.setattr(guard, '_ineffective_escalated', False)
    fired = []
    monkeypatch.setattr(guard, 'audit_log', lambda *a, **k: fired.append(k))
    for _ in range(guard._INEFFECTIVE_LIMIT):
        guard.relieve_memory('monitor')
    assert len(fired) == 1

    # One genuinely effective relief: usage drops during the call.
    seq = iter([
        int(limit * 0.999),
        int(limit * 0.999),
        int(limit * 0.80),
    ])
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: next(seq))
    guard.relieve_memory('monitor')

    # Squeeze returns and stays stuck → must be allowed to escalate again.
    state['usage'] = int(limit * 0.999)
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: state['usage'])
    for _ in range(guard._INEFFECTIVE_LIMIT):
        guard.relieve_memory('monitor')
    assert len(fired) == 2, 'escalation did not re-arm after a recovery'


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
    records = [_json.loads(line) for line in lines]
    rec = records[-1]
    assert rec['pct'] == 50.0
    assert rec['cache_gib'] == 1.0 and rec['kmem_gib'] == 3.0
    assert os.path.getsize(jpath) <= 400 + 200  # bounded (last append may exceed slightly)
    assert (jpath.stat().st_mode & 0o777) == 0o600


def test_pressure_journal_concurrent_appends_are_not_lost(guard, monkeypatch,
                                                          tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import json as _json

    jpath = tmp_path / 'journal.log'
    monkeypatch.setattr(guard, '_JOURNAL_PATH', str(jpath))
    monkeypatch.setattr(guard, '_JOURNAL_MAX_BYTES', 1 << 20)
    monkeypatch.setattr(guard, '_read_memory_stat', lambda: {})
    snap = {'pct': 50.0, 'usage': 100, 'limit': 200}

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(
            lambda _index: guard.write_pressure_journal(snap), range(64)))

    assert all(results)
    lines = jpath.read_text().splitlines()
    assert len(lines) == 64
    assert all(_json.loads(line)['pct'] == 50.0 for line in lines)
    assert not (tmp_path / 'journal.log.tmp').exists()


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
