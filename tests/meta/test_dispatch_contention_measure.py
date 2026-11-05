"""The dispatcher contention benchmark measures real picks and real route groups."""

import pytest

from benchmarks.dispatch_contention import run

pytestmark = pytest.mark.unit


def test_bounded_run_reports_every_operation_and_joined_cleanup(tmp_path):
    observed = run(groups=2, models=3, credentials=1, configured=4,
                   cpu_threads=1, pickers=1, seconds=0.5, stats_dir=tmp_path)
    assert observed['group_size'] == 6
    assert observed['live_slots'] == 4 + 2 * 6
    for name in ('pinned_pick', 'unpinned_pick', 'group_mint', 'group_dispose'):
        summary = observed[name]
        assert summary['count'] > 0, name
        assert 0 <= summary['p50_ms'] <= summary['p90_ms'] <= summary['max_ms']
    assert observed['leftover_request_slots'] == 0
    assert observed['leftover_registered_handles'] == 0
