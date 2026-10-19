"""Qualify owned ingestion producer trees from exact exit and original-wait facts.

Entry child_tree_peak. Dependencies: scalar worker/ptrace receipts only. Threads
share a process peak; parents and children may overlap, so their independent
peaks add along each path. Siblings must be witnessed sequential. Unsupported
descendants, missing waits or snapshots refuse; there is no sampling fallback.
"""
import os


def indexed(rows, key):
    result = {row[key]: row for row in rows}
    assert len(result) == len(rows), 'duplicate process/thread receipt'
    return result


def child_tree_peak(worker, observer):
    assert observer['allow_threaded_children'] is True
    root = worker['pid']
    tracees = indexed(observer['tracees'], 'pid')
    groups = indexed(observer['child_process_groups'], 'tgid')
    producers = indexed(worker['producer_receipts'], 'producer_pid')
    assert groups and producers
    expected_groups = {row['tgid'] for row in tracees.values() if row['tgid'] != root}
    assert groups.keys() == expected_groups
    waited = {}
    def record_waits(parent, rows):
        assert isinstance(rows, list) and rows
        for row in rows:
            pid = row['pid']
            assert pid not in waited
            waited[pid] = (parent, row)
    record_waits(root, worker['child_memory_receipts'])
    for pid, producer in producers.items():
        assert pid in groups and pid in waited and waited[pid][0] == root
        assert producer['producer_returncode'] == -9
        assert waited[pid][1]['returncode'] == producer['producer_returncode']
        identity = producer['producer_identity'].split(':')
        assert identity == [observer['boot_id'], str(pid), tracees[pid]['start_ticks']]
        record_waits(pid, producer['producer_child_memory_receipts'])
    assert groups.keys() == waited.keys(), 'every descendant requires its original parent wait'

    children = {}
    for pid, group in groups.items():
        parent, receipt = waited[pid]
        leader = tracees[pid]
        assert leader['pid'] == leader['tgid'] == pid and leader['creator_tgid'] == parent
        assert len(leader['execs']) == 1 and leader['joined']
        assert leader['returncode'] == receipt['returncode']
        assert leader['wait4_peak_rss_bytes'] == receipt['peak_rss_bytes']
        assert leader['born_event'] < leader['execs'][0]['event'] < leader['exit_event'] < leader['joined_event']
        if parent != root:
            assert tracees[parent]['execs'][0]['event'] < leader['born_event']
            assert leader['joined_event'] < tracees[parent]['exit_event'], 'producer must join descendants before its commit barrier'
        tids = group['tids']
        assert len(tids) == len(set(tids))
        assert set(tids) == {row['pid'] for row in tracees.values() if row['tgid'] == pid}
        assert group['complete'] and pid in tids
        peaks = []
        for tid in tids:
            row = tracees[tid]
            assert row['joined'] and row['born_event'] < row['exit_event'] < row['joined_event']
            assert row['joined_event'] <= leader['joined_event']
            assert os.waitstatus_to_exitcode(row['exit_event_status']) == row['returncode']
            if tid != pid:
                assert not row['execs'] and row['creator_tgid'] == pid
                assert leader['execs'][0]['event'] < row['born_event']
            memory = row['memory']; status = memory['status']
            assert status['Pid'] == tid and status['Tgid'] == pid and status['PPid'] == parent
            assert status['Threads'] >= 1
            peak = max(status['VmHWM'], memory['exit_smaps']['Rss'])
            assert memory['peak_rss_estimate'] == peak > 0
            peaks.append(peak)
        assert group['peak_rss_estimate'] == max(peaks)
        children.setdefault(parent, []).append(pid)

    for siblings in children.values():
        ordered = sorted(siblings, key=lambda pid: tracees[pid]['born_event'])
        for before, after in zip(ordered, ordered[1:]):
            assert tracees[before]['joined_event'] < tracees[after]['born_event'], 'overlapping sibling processes need concurrent accounting'
    # The supported ingestion topology has one producer level and its waited
    # CLI children. This also refuses cycles or detached/unattributed owners.
    assert all(parent == root or parent in producers for parent in children)
    assert all(not children.get(pid) for pid in groups if pid not in producers)
    def peak_below(parent):
        return max((groups[pid]['peak_rss_estimate'] + peak_below(pid)
                    for pid in children.get(parent, [])), default=0)
    return peak_below(root)
