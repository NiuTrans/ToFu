"""Executed fixture, canonical publication, refusal and cleanup evidence.

Used in each independent package environment before timing. No measurement
worker is reused. The trace must agree across the two backend environments.
"""
import json
from pathlib import Path
import tempfile

from native_domain_worker import CASES, Workload, digest, require_weighted_ranking, settle_audit
from lib.repo_map import outline, _native_rank
from lib.tasks_pkg.manager.runtime import chat_task_runtime
from publication_port import TransientChatRuntime


def check():
    traces = {}; counts = {}
    with require_weighted_ranking():
        for case in CASES:
            with tempfile.TemporaryDirectory(prefix='tofu-repository-domain-check-') as directory:
                work = Workload(directory, case)
                try:
                    scanned = outline.scan(directory)
                    assert set(scanned.files) == set(work.files)
                    for name in work.files:
                        index = int(Path(name).stem.split('_')[1])
                        assert f'symbol_{index}' in scanned.definitions
                        assert name in scanned.references[f'symbol_{(index-1)%len(work.files)}']
                    rows = [work.operation(index) for index in range(2)]
                    if case == 'incremental_edit':
                        edited = outline.scan(directory)
                        assert 'edited_1' in edited.definitions and 'edited_0' not in edited.definitions
                    if case != 'cold_scan':
                        events = chat_task_runtime.poll(work.task['id'])['events']
                        events = [e for e in events if e['type'] == 'tool_result']
                        assert len(events) == 2 and all(e['status'] == 'done' and e['toolName'] == 'repo_map' for e in events)
                        counts[case] = len(events)
                    else:
                        assert work.runtime.task_count == 0
                        counts[case] = 'two separately settled tasks'
                    traces[case] = digest(rows)
                finally:
                    work.close()
                assert work.closed and work.runtime.task_count == 0
        sentinel_runtime = TransientChatRuntime()
        sentinel = sentinel_runtime.create(user_id=900002)
        try:
            with tempfile.TemporaryDirectory(prefix='tofu-repository-domain-refusal-') as directory:
                root = Path(directory); (root/'owned').mkdir(); (root/'foreign').mkdir()
                work = Workload(root/'owned', 'python_extraction')
                try:
                    work.operation(0)
                    before = len(chat_task_runtime.poll(work.task['id'])['events']); statuses=[]
                    result = work.port.invoke(work.task, {}, lambda *_args, **options: statuses.append(options['status']), project_root=root/'foreign')
                    assert result[2] and statuses == ['error']
                    events = chat_task_runtime.poll(work.task['id'])['events']
                    assert len(events) == before+1 and events[-1]['type'] == 'tool_result' and events[-1]['status'] == 'error'
                finally:
                    work.close(True)
                assert chat_task_runtime.get(sentinel['id']) is sentinel
        finally:
            sentinel_runtime.finish(sentinel['id'], result='owned check complete')
            sentinel_runtime.cleanup_stale(max_age=0)
            settle_audit()
    return dict(fixture_output_sha256=traces, publications=counts,
                foreign_root_refused_once=True, unrelated_task_preserved=True,
                joined_cleanup=True, weighted_ranking_required=True,
                native_backend_selected=_native_rank.available_kernel() is not None)


if __name__ == '__main__':
    print(json.dumps(check(), sort_keys=True))
