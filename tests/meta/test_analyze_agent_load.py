"""Offline load diagnosis must not turn partial observations into capacity proof."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.analyze_agent_load import analyze

pytestmark = pytest.mark.unit


def _rpc(count, seconds, labels='kind="command",operation="turn.event.record",outcome="success"'):
    return (f'tofu_storage_rpc_seconds_count{{{labels}}} {count}\n'
            f'tofu_storage_rpc_seconds_sum{{{labels}}} {seconds}\n')


def test_interval_work_uses_deltas_and_canonical_label_identity():
    before = _rpc(100, 500) + 'tofu_task_event_delivery_accepted_total 1000\n'
    after = _rpc(104, 501, 'outcome="success",operation="turn.event.record",kind="command"')
    after += 'tofu_task_event_delivery_accepted_total 1020\n'
    report = analyze(before, after, 10)
    rpc, = report['storage_rpc']
    assert rpc['calls'] == 4
    assert rpc['mean_wall_ms'] == 250
    assert rpc['wall_seconds_sum'] == 1
    assert rpc['calls_per_second'] == 0.4
    assert report['delivery_counters'][0]['per_second'] == 2


@pytest.mark.parametrize(('before', 'after', 'status'), [
    (_rpc(10, 5), _rpc(9, 6), 'counter_decreased'),
    (_rpc(10, 5), _rpc(12, 4), 'counter_decreased'),
    (_rpc(10, 5), _rpc(10, 6), 'inconsistent_histogram'),
    (_rpc(10, 5), _rpc(10.5, 6), 'inconsistent_histogram'),
    (_rpc(10, 5), 'tofu_tasks_inflight{kind="chat"} 1', 'missing_endpoint'),
    ('tofu_tasks_inflight{kind="chat"} 1', _rpc(10, 5), 'missing_endpoint'),
])
def test_incomplete_or_reset_counters_never_report_latency(before, after, status):
    row, = analyze(before, after, 10)['storage_rpc']
    assert row['status'] == status
    assert row['calls'] is None
    assert row['mean_wall_ms'] is None


def test_quiet_interval_and_gauges_preserve_their_different_meanings():
    before = _rpc(10, 5) + 'tofu_task_event_delivery_pending_bytes 2000000000\n'
    after = _rpc(10, 5) + 'tofu_task_event_delivery_pending_bytes 500000000\n'
    report = analyze(before, after, 10)
    assert report['storage_rpc'][0]['calls'] == 0
    assert report['storage_rpc'][0]['mean_wall_ms'] is None
    assert report['gauges'][0]['before'] == 2000000000
    assert report['gauges'][0]['after'] == 500000000
    assert report['source_sha256'][0] != report['source_sha256'][1]
    assert 'process RSS' in ' '.join(report['interpretation'])


@pytest.mark.parametrize('scrape', [
    '',
    'tofu_event_loop_lag_seconds NaN',
    'tofu_event_loop_lag_seconds -1',
    'tofu_event_loop_lag_seconds 1\ntofu_event_loop_lag_seconds 2',
    'tofu_tasks_inflight{kind="chat",kind="chat"} 1',
    'tofu_tasks_inflight{kind="chat",task_id="private"} 1',
    'tofu_tasks_inflight{kind="unterminated} 1',
])
def test_malformed_or_ambiguous_selected_evidence_fails_closed(scrape):
    with pytest.raises(ValueError):
        analyze(scrape, scrape, 1)


@pytest.mark.parametrize('seconds', [0, -1, float('nan'), float('inf')])
def test_invalid_elapsed_time_is_not_a_rate(seconds):
    with pytest.raises(ValueError, match='elapsed_seconds'):
        analyze(_rpc(1, 1), _rpc(2, 2), seconds)


def test_unknown_series_are_not_copied_into_report():
    scrape = _rpc(1, 1) + 'unrelated_metric{secret="private-value"} 12\n'
    assert 'private-value' not in json.dumps(analyze(scrape, scrape, 1))


def test_cli_reports_saved_evidence_without_application_startup(tmp_path):
    before, after = tmp_path / 'before.prom', tmp_path / 'after.prom'
    before.write_text(_rpc(2, 4))
    after.write_text(_rpc(5, 10))
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(root / 'scripts/analyze_agent_load.py'),
         str(before), str(after), '--elapsed-seconds', '3'],
        cwd=tmp_path, capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(result.stdout)['storage_rpc'][0]['mean_wall_ms'] == 2000
    assert not result.stderr
