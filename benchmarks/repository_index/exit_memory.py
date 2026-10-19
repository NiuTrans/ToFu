"""Join repository worker exits to independent post-exec memory observations.

The worker's wait4 and raw output receipts remain intact. This authority refuses
missing, foreign, duplicated or mismatched process evidence before selecting RSS.
"""
import os

MEMORY_SCOPE = ('parent post-exec VmHWM estimate + maximum matching direct-child '
                'post-exec VmHWM/stopped-exit RSS estimate + sealed image bytes; '
                'includes fixture setup; independent peaks and shared pages may '
                'overlap; exit PSS is not peak PSS')


def qualify(worker, observer):
    if not __debug__:
        raise RuntimeError('exit memory qualification requires validation checks')
    assert worker['mode'] == 'memory' and worker['joined_cleanup']
    assert worker['memory_peak_scope'] == 'post_exec_vm_hwm_estimate'
    assert worker['clock_records_materialized_after_memory_counters']
    assert worker['child_records_materialized_after_worker']
    assert observer['format'] == 'tofu.process-exit-memory/v1'
    assert observer['observation_valid'] and observer['all_tracees_joined']
    assert observer['root_returncode'] == 0 and observer['child_memory_complete']
    assert observer['memory_observation_only'] and not observer['performance_admission']
    identities = worker['process_identity'].split(':')
    assert len(identities) == 3 and int(identities[1]) == observer['root_pid']
    assert identities[0] == observer['boot_id']
    root_pid = observer['root_pid']
    rows = observer['tracees']
    assert len({row['pid'] for row in rows}) == len(rows)
    root = next(row for row in rows if row['pid'] == root_pid)
    assert root['start_ticks'] == identities[2] and root['tgid'] == root_pid and root['execs']
    assert all(row['joined'] for row in rows)
    children = {row['pid']: row for row in rows if row['tgid'] != root_pid}
    waited = worker['child_memory_receipts']
    assert len(waited) == len(children) and children
    assert {row['pid'] for row in waited} == children.keys()
    peaks = []
    for receipt in waited:
        child = children[receipt['pid']]
        assert child['pid'] == child['tgid'] and child['creator_tgid'] == root_pid
        assert len(child['execs']) == 1
        assert child['returncode'] == receipt['returncode'] == 0
        assert os.waitstatus_to_exitcode(child['exit_event_status']) == child['returncode']
        assert child['wait4_peak_rss_bytes'] == receipt['peak_rss_bytes']
        memory = child['memory']
        status, stopped = memory['status'], memory['exit_smaps']
        assert status['Pid'] == child['pid'] and status['PPid'] == root_pid and status['Threads'] == 1
        assert all(type(value) is int and value > 0 for value in (status['VmHWM'], stopped['Rss']))
        assert type(stopped['Pss']) is int and 0 <= stopped['Pss'] <= stopped['Rss']
        assert memory['peak_rss_estimate'] == max(status['VmHWM'], stopped['Rss'])
        peaks.append(memory['peak_rss_estimate'])
    parent = worker['process_image_counters']
    assert parent['Pid'] == parent['Tgid'] == root_pid
    assert worker['parent_peak_rss_bytes'] == parent['VmHWM'] > 0
    image = worker['retained_executable_image_bytes']
    assert type(image) is int and image >= 0
    result = dict(worker)
    result.update(memory_observer='ptrace_exit', memory_scope=MEMORY_SCOPE,
                  child_post_exec_observed=True,
                  legacy_child_peak_rss_bytes=worker['discovery_child_peak_rss_bytes'],
                  legacy_inherited_peak_rss_bytes=worker['process_lifetime_peak_rss_bytes']+
                      worker['discovery_child_peak_rss_bytes']+image,
                  discovery_child_peak_rss_bytes=max(peaks),
                  peak_rss_bytes=parent['VmHWM']+max(peaks)+image,
                  exit_memory_observation=observer)
    return result
