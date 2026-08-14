"""Smoke the deterministic task replay workload benchmark."""

from __future__ import annotations

import pytest

from benchmarks.task_replay_bench import run_benchmarks


pytestmark = pytest.mark.unit


def test_task_replay_workloads_cover_normal_trimmed_and_terminal_pages():
    report = run_benchmarks(scale=0.02, iterations=1)
    assert report['format'] == 'tofu.task-replay-benchmark/v1'
    rows = {row['name']: row for row in report['workloads']}
    assert set(rows) == {
        'short_conversation', 'long_tool_loop',
        'disconnect_recovery', 'paper_flow',
    }
    assert rows['short_conversation']['cursor_reset'] is False
    assert rows['long_tool_loop']['replayed_events'] >= 32
    assert rows['disconnect_recovery']['cursor_reset'] is True
    assert rows['disconnect_recovery']['replayed_events'] \
        < rows['disconnect_recovery']['event_count']
    assert rows['paper_flow']['replayed_events'] \
        == rows['paper_flow']['event_count'] + 1
    assert all(row['peak_kib'] > 0 for row in rows.values())
