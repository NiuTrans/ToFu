#!/usr/bin/env python3
"""Tests for lib.cgroup_guard — the shared-cgroup memory-pressure defenses.

Covers the defenses (① startup self-check, ② runtime relief monitor,
③ pressure journal + OOM-kill witness), the graceful NO-OP when the cgroup is
unreadable (bare metal / restricted sandbox), and a NEUTER reverse-control per
defense proving the pressure signal is load-bearing. None of them refuses,
shrinks, recycles or throttles work (owner decision 2026-10-24).

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/cgroup_guard/test_cgroup_guard.py -v
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

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
              'TOFU_CGROUP_POLL_SEC',
              'TOFU_CGROUP_RELIEF_COOLDOWN_SEC', 'TOFU_CGROUP_MATERIAL_PCT'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(cg, '_ineffective_reliefs', 0)
    monkeypatch.setattr(cg, '_ineffective_escalated', False)
    monkeypatch.setattr(cg, '_relief_suppressed_until', 0.0)
    monkeypatch.setattr(cg, '_relief_rss_baseline', None)
    yield cg
    reset_for_tests()


def _set_pressure(cg, monkeypatch, pct, swap=0, reclaimable=None):
    limit = 200 * _GIB
    monkeypatch.setattr(cg, 'mem_limit_bytes', lambda: limit)
    monkeypatch.setattr(cg, 'mem_usage_bytes', lambda: int(limit * pct / 100.0))
    monkeypatch.setattr(cg, 'swap_total_bytes', lambda: swap)
    # Hermetic reclaim signal: without this stub the REAL host memory.stat
    # leaks into pressure() and relief tests depend on the dev machine's
    # live page cache. ``reclaimable=None`` means "stat unreadable" → the
    # raw, conservative measurement applies (pre-2026-09-18 semantics).
    stat = {} if reclaimable is None else {'inactive_file': int(reclaimable)}
    monkeypatch.setattr(cg, '_read_memory_stat', lambda: dict(stat))


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


# ── ① startup self-check ──

def test_self_check_warns_when_full_and_no_swap(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=99.7, swap=0)
    snap = guard.startup_self_check()
    assert snap is not None
    assert snap['pct'] >= 90.0


def test_self_check_silent_when_roomy(guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=40.0, swap=0)
    assert guard.startup_self_check() is None


def test_self_check_does_not_report_reclaimable_cache_as_oom_pressure(
        guard, monkeypatch, caplog):
    _set_pressure(guard, monkeypatch, pct=99.0, swap=0,
                  reclaimable=150 * _GIB)
    assert guard.startup_self_check() is None
    assert not any(record.levelno >= 50 for record in caplog.records)


def test_self_check_retains_alarm_when_working_set_is_near_full(
        guard, monkeypatch, caplog):
    _set_pressure(guard, monkeypatch, pct=99.0, swap=0,
                  reclaimable=2 * _GIB)
    snap = guard.startup_self_check()
    assert snap is not None and snap['effective_pct'] == 98.0
    assert any(record.levelno >= 50 for record in caplog.records)


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


def _isolate_aggregate_monitor(guard, monkeypatch):
    """Drive run_monitor_once without journal or OOM-witness side effects."""
    monkeypatch.setattr(guard, 'write_pressure_journal', lambda _snap: True)
    monkeypatch.setattr(guard, 'check_oom_kill_count', lambda: False)


def test_monitor_triggers_on_reclaim_aware_pressure_not_raw_cache(
        guard, monkeypatch):
    """Other tenants' reclaimable page cache is not work relief can do.

    Live 2026-10-23: a shared 220 GiB cgroup sat at raw 92-96% with ~97 GiB
    inactive_file (effective ~50%); relief fired 118 times an hour, dropped
    Tofu's own caches each time and could not move the number.
    """
    _isolate_aggregate_monitor(guard, monkeypatch)
    calls = []
    monkeypatch.setattr(guard, 'relieve_memory',
                        lambda reason: calls.append(reason) or {'reason': reason})

    _set_pressure(guard, monkeypatch, pct=96.0, reclaimable=88 * _GIB)
    assert guard.run_monitor_once() is None
    assert calls == []

    # NEUTER: the same raw usage with a near-full working set is real pressure.
    _set_pressure(guard, monkeypatch, pct=96.0, reclaimable=2 * _GIB)
    assert guard.run_monitor_once() is not None
    assert len(calls) == 1 and 'effective 95.0%' in calls[0]


def test_no_progress_relief_does_not_repeat_every_monitor_tick(
        guard, monkeypatch):
    """Each pass that reclaims ~nothing doubles the wait before the next one."""
    import time

    _isolate_aggregate_monitor(guard, monkeypatch)
    _set_pressure(guard, monkeypatch, pct=99.0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache', lambda: {
        'files': 0, 'bytes': 0, 'skipped': 4})
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: 512 << 20)
    escalations = []
    monkeypatch.setattr(guard, 'audit_log',
                        lambda *a, **k: escalations.append(a))
    now = [1000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])

    passes = []
    for tick in range(240):  # two hours of 30-second monitor ticks
        now[0] = 1000.0 + 30.0 * tick
        if guard.run_monitor_once() is not None:
            passes.append(30 * tick)

    assert passes == [0, 60, 150, 300, 600, 1200, 2400, 4800]
    assert len(escalations) == 1
    assert guard._relief_rss_baseline == 512 << 20


def test_aggregate_backoff_survives_noise_around_the_trigger(
        guard, monkeypatch):
    """Only a real recovery re-arms relief; a dip just under the line does not.

    Live 2026-10-24: back-off re-armed at 91.1/91.5/91.9/92.0% against the
    92% trigger and every re-arm restarted per-tick relief.
    """
    _isolate_aggregate_monitor(guard, monkeypatch)
    monkeypatch.setattr(guard, '_ineffective_reliefs', 3)
    monkeypatch.setattr(guard, '_relief_suppressed_until', float('inf'))

    _set_pressure(guard, monkeypatch, pct=91.0)
    assert guard.run_monitor_once() is None
    assert guard._ineffective_reliefs == 3
    assert guard._relief_suppressed_until == float('inf')

    _set_pressure(guard, monkeypatch, pct=89.0)
    assert guard.run_monitor_once() is None
    assert guard._ineffective_reliefs == 0
    assert guard._relief_suppressed_until == 0.0


def test_tofu_rss_growth_reopens_deferred_relief(guard, monkeypatch):
    """Back-off must not hide Tofu memory that grew since the last pass."""
    import math

    _isolate_aggregate_monitor(guard, monkeypatch)
    _set_pressure(guard, monkeypatch, pct=99.0)
    monkeypatch.setattr(guard, '_relief_suppressed_until', float('inf'))
    monkeypatch.setattr(guard, '_relief_rss_baseline', 1 * _GIB)
    calls = []
    monkeypatch.setattr(guard, 'relieve_memory',
                        lambda reason: calls.append(reason) or {'reason': reason})
    floor = math.ceil(guard._material_reclaim_bytes(200 * _GIB))

    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: 1 * _GIB + floor - 1)
    assert guard.run_monitor_once() is None

    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: 1 * _GIB + floor)
    assert guard.run_monitor_once() is not None
    assert len(calls) == 1 and 'Tofu RSS +' in calls[0]


def test_repeated_no_progress_relief_is_demoted_below_warning(
        guard, monkeypatch, caplog):
    """The first no-progress pass and state changes stay WARNING+; repeats do not."""
    import logging

    _set_pressure(guard, monkeypatch, pct=99.0)
    monkeypatch.setattr(guard, 'malloc_trim', lambda: True)
    monkeypatch.setattr(guard, 'drop_logs_cache', lambda: {
        'files': 0, 'bytes': 0, 'skipped': 4})
    monkeypatch.setattr(guard, 'audit_log', lambda *a, **k: None)
    caplog.set_level(logging.INFO, logger='lib.cgroup_guard')

    def relief_levels():
        return [record.levelno for record in caplog.records
                if record.getMessage().startswith('[cgroup] relief (')]

    for _ in range(guard._INEFFECTIVE_LIMIT + 2):
        guard.relieve_memory('monitor')

    assert relief_levels() == (
        [logging.WARNING] + [logging.INFO] * (guard._INEFFECTIVE_LIMIT + 1))
    assert len([record for record in caplog.records
                if record.levelno >= logging.CRITICAL]) == 1

    limit = 200 * _GIB
    usage = iter([int(limit * 0.99), int(limit * 0.99), int(limit * 0.80)])
    monkeypatch.setattr(guard, 'mem_usage_bytes', lambda: next(usage))
    caplog.clear()
    guard.relieve_memory('monitor')

    assert relief_levels() == [logging.WARNING]
    assert guard._ineffective_reliefs == 0


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


def test_no_progress_deferral_doubles_through_escalation_to_one_hour(
        guard, monkeypatch):
    """The configured cooldown is the deferral at the escalating pass.

    Earlier no-progress passes wait 1/16..1/2 of it, later ones double to the
    one-hour ceiling: a shared-cgroup squeeze must not flush hot caches on
    every monitor tick.
    """
    import time

    _set_pressure(guard, monkeypatch, pct=99.0, swap=0)
    monkeypatch.setenv('TOFU_CGROUP_RELIEF_COOLDOWN_SEC', '600')
    monkeypatch.setattr(guard, 'malloc_trim', lambda: False)
    monkeypatch.setattr(guard, 'drop_logs_cache', lambda: {
        'files': 0, 'bytes': 0, 'skipped': 0})
    escalations = []
    monkeypatch.setattr(guard, 'audit_log',
                        lambda *a, **k: escalations.append(len(observed) + 1))
    now = [1000.0]
    monkeypatch.setattr(time, 'monotonic', lambda: now[0])

    observed = []
    for _ in range(9):
        guard.relieve_memory('adaptive probe')
        observed.append(guard._relief_suppressed_until - now[0])
        now[0] += observed[-1]

    assert observed == [37.5, 75.0, 150.0, 300.0, 600.0,
                        1200.0, 2400.0, 3600.0, 3600.0]
    assert escalations == [guard._INEFFECTIVE_LIMIT]


def test_monitor_rearms_backoff_after_pressure_episode_ends(
        guard, monkeypatch):
    _set_pressure(guard, monkeypatch, pct=80.0, swap=0)
    monkeypatch.setattr(guard, '_ineffective_reliefs', 8)
    monkeypatch.setattr(guard, '_ineffective_escalated', True)
    monkeypatch.setattr(guard, '_relief_suppressed_until', float('inf'))

    assert guard.run_monitor_once() is None
    assert guard._ineffective_reliefs == 0
    assert guard._ineffective_escalated is False
    assert guard._relief_suppressed_until == 0.0


def test_large_process_rss_alone_never_triggers_relief(guard, monkeypatch):
    """Process RSS is not a trigger; only aggregate pressure drives relief.

    Owner decision 2026-10-24 removed process-RSS relief and the worker
    recycle it could request: a large Tofu in a roomy cgroup is left alone.
    """
    _isolate_aggregate_monitor(guard, monkeypatch)
    _set_pressure(guard, monkeypatch, pct=40.0, swap=0)
    monkeypatch.setattr(guard, '_self_rss_bytes', lambda: 64 * _GIB)
    monkeypatch.setattr(
        guard, 'relieve_memory',
        lambda _reason: pytest.fail('process RSS alone must not relieve'))

    assert guard.run_monitor_once() is None


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
        assert stats['dropped'] >= 7
        assert len(c) == 0
        assert usage_cache.usage_cache_snapshot()['entries'] == 0
        assert _resolve.current_work_tab(('41', 'pressure-a')) is None
        assert _resolve.current_work_tab(('42', 'pressure-b')) is None
        assert tool_search.catalog_cache_stats()['indexes'] == 0
        assert tool_search.catalog_cache_stats()['tasks'] == 1
        assert tool_search._SELECTION_STATE['pressure-tool-search']['used'] == ['mcp__docs__read']
    finally:
        _resolve.clear_work_tab_cache()
        tool_search.clear_tool_search_caches()
        usage_cache._reset_usage_cache_for_tests()


# ── reclaim-aware measurement (2026-09-18 cache-dominated-cgroup incident) ──

def test_pressure_snapshot_is_reclaim_aware(guard, monkeypatch):
    """effective_* subtracts only the immediately-reclaimable page cache."""
    _set_pressure(guard, monkeypatch, pct=97.0,
                  reclaimable=120 * _GIB)  # of 200 GiB
    snap = guard.pressure()
    assert snap['pct'] == pytest.approx(97.0, abs=0.1)
    assert snap['reclaimable'] == 120 * _GIB
    assert snap['effective_usage'] == int(200 * _GIB * 0.97) - 120 * _GIB
    assert snap['effective_pct'] == pytest.approx(37.0, abs=0.1)


def test_pressure_snapshot_without_stat_stays_raw_only(guard, monkeypatch):
    """Unreadable memory.stat → no effective keys → callers fall back to raw."""
    _set_pressure(guard, monkeypatch, pct=97.0, reclaimable=None)
    snap = guard.pressure()
    assert 'effective_pct' not in snap
    assert 'reclaimable' not in snap


def test_reclaimable_file_bytes_prefers_hierarchical_total(guard):
    assert guard.reclaimable_file_bytes(
        {'total_inactive_file': 300, 'inactive_file': 100}) == 300
    # v2 memory.stat only has inactive_file (already hierarchical).
    assert guard.reclaimable_file_bytes({'inactive_file': 100}) == 100
    assert guard.reclaimable_file_bytes({}) is None
    assert guard.reclaimable_file_bytes({'cache': 5}) is None


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
    assert guard._relief_suppressed_until == 0.0


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
