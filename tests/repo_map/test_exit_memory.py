"""Reject incomplete or mismatched external memory evidence before selecting RSS."""
from copy import deepcopy

import pytest

from benchmarks.repository_index.exit_memory import qualify

pytestmark = pytest.mark.api


def receipts():
    worker = dict(mode='memory', joined_cleanup=True,
        memory_peak_scope='post_exec_vm_hwm_estimate',
        clock_records_materialized_after_memory_counters=True,
        child_records_materialized_after_worker=True,
        process_identity='boot:100:123', parent_peak_rss_bytes=1000,
        process_image_counters=dict(Pid=100, Tgid=100, VmHWM=1000),
        process_lifetime_peak_rss_bytes=9000, discovery_child_peak_rss_bytes=8000,
        retained_executable_image_bytes=200,
        child_memory_receipts=[dict(pid=101, returncode=0, peak_rss_bytes=8000)])
    child = dict(pid=101, tgid=101, creator_tgid=100, joined=True, execs=[{}],
        returncode=0, exit_event_status=0, wait4_peak_rss_bytes=8000,
        memory=dict(status=dict(Pid=101, PPid=100, Threads=1, VmHWM=600),
                    exit_smaps=dict(Rss=500, Pss=300), peak_rss_estimate=600))
    observer = dict(format='tofu.process-exit-memory/v1', observation_valid=True,
        all_tracees_joined=True, root_returncode=0, child_memory_complete=True,
        memory_observation_only=True, performance_admission=False, root_pid=100,
        boot_id='boot', tracees=[dict(pid=100, tgid=100, start_ticks='123',
                                     execs=[{}], joined=True), child])
    return worker, observer


def test_selection_preserves_legacy_and_original_observations():
    worker, observer = receipts()
    original = deepcopy((worker, observer))
    result = qualify(worker, observer)
    assert result['peak_rss_bytes'] == 1800
    assert result['legacy_inherited_peak_rss_bytes'] == 17200
    assert result['legacy_child_peak_rss_bytes'] == 8000
    assert result['child_memory_receipts'] == worker['child_memory_receipts']
    assert (worker, observer) == original


@pytest.mark.parametrize('damage', [
    lambda w, o: o.update(boot_id='foreign-boot'),
    lambda w, o: o.update(observation_valid=False),
    lambda w, o: o.update(all_tracees_joined=False),
    lambda w, o: o.update(child_memory_complete=False),
    lambda w, o: o.update(root_returncode=1),
    lambda w, o: o['tracees'][0].update(start_ticks='124'),
    lambda w, o: o['tracees'].pop(),
    lambda w, o: o['tracees'].append(deepcopy(o['tracees'][1])),
    lambda w, o: o['tracees'][1].update(creator_tgid=999),
    lambda w, o: o['tracees'][1].update(returncode=1),
    lambda w, o: o['tracees'][1].update(exit_event_status=256),
    lambda w, o: o['tracees'][1].update(wait4_peak_rss_bytes=7999),
    lambda w, o: o['tracees'][1]['execs'].append({}),
    lambda w, o: o['tracees'][1]['memory']['status'].update(PPid=999),
    lambda w, o: o['tracees'][1]['memory']['status'].update(Threads=2),
    lambda w, o: o['tracees'][1]['memory']['exit_smaps'].update(Pss=501),
    lambda w, o: o['tracees'][1]['memory'].update(peak_rss_estimate=601),
    lambda w, o: w.update(parent_peak_rss_bytes=1001),
    lambda w, o: w.update(memory_peak_scope='process_lifetime_rusage'),
    lambda w, o: w.update(clock_records_materialized_after_memory_counters=False),
    lambda w, o: w['child_memory_receipts'].append(deepcopy(w['child_memory_receipts'][0])),
])
def test_invalid_observation_is_refused(damage):
    worker, observer = receipts()
    damage(worker, observer)
    with pytest.raises((AssertionError, KeyError, StopIteration)):
        qualify(worker, observer)
