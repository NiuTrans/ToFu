"""Offline contracts for the bounded soak measurement tool."""

from __future__ import annotations

import pytest

import scripts.tofudb_soak as soak

pytestmark = pytest.mark.unit


def test_window_summary_reports_counts_errors_and_percentiles() -> None:
    import time
    summary = soak._window_summary(
        [1.0, 2.0, 3.0, 4.0], {'database_busy': 2}, time.monotonic() - 1.0)
    assert summary['operations'] == 4
    assert summary['errors'] == {'database_busy': 2}
    assert summary['p50_ms'] == 2.0
    assert summary['p99_ms'] == 4.0
    assert summary['ops_per_sec'] > 0


def test_window_summary_tolerates_an_error_only_window() -> None:
    summary = soak._window_summary([], {'engine_died': 1}, 0.0)
    assert summary['operations'] == 0
    assert summary['p50_ms'] == 0.0
    assert summary['errors'] == {'engine_died': 1}


def test_report_declares_bounded_scope_and_budget_verdict() -> None:
    report = soak.build_report(
        minutes=10, scale_name='small', engine=None, windows=[],
        totals={'operations': 5, 'errors': {}, 'rounds': 1},
        rss={'engine_peak_rss_observed_bytes': 100, 'client_peak_rss_observed_bytes': 50},
        metadata={'scope': 'test'})
    assert report['format'] == soak.REPORT_FORMAT
    assert report['scope'] == 'bounded-soak-not-the-full-uncovered-claim'
    assert report['budgets']['engine_peak_rss_within_budget'] is True


def test_report_flags_an_over_budget_engine() -> None:
    report = soak.build_report(
        minutes=1, scale_name='small', engine=None, windows=[],
        totals={'operations': 0, 'errors': {}, 'rounds': 0},
        rss={'engine_peak_rss_observed_bytes': 600 * 1024 * 1024},
        metadata={})
    assert report['budgets']['engine_peak_rss_within_budget'] is False


def test_minutes_bound_is_enforced() -> None:
    import subprocess
    result = subprocess.run(
        ['python3', 'scripts/tofudb_soak.py', '--minutes', '0',
         '--binary', '/bin/false'],
        capture_output=True, text=True, cwd=soak.ROOT)
    assert result.returncode != 0
    assert '--minutes' in result.stderr
