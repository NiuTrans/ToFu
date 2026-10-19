"""Completion diagnostics project bounded addresses, never whole history copies."""
from collections import OrderedDict
import json
from pathlib import Path
import tracemalloc

import pytest

from lib.project_mod import modifications as mods
from lib.tasks_pkg.workspace_delivery import capture_workspace_evidence

pytestmark = pytest.mark.unit


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'sessions'))
    monkeypatch.setattr(mods, '_mods_cache', OrderedDict())
    monkeypatch.setattr(mods, '_mods_cache_identity', {})
    monkeypatch.setattr(mods, '_mods_cache_bytes', {})
    root = str(tmp_path / 'project')
    Path(root).mkdir()
    session = mods._get_session_dir(root)

    def write(rows):
        mods._atomic_json_write(str(Path(session) / 'modifications.json'),
                                {'modifications': rows})
    return root, session, write


def row(path, task='task', conv='conv', **extra):
    return dict(type='write_file', path=path, taskId=task, convId=conv, **extra)


def test_large_history_projection_has_bounded_warm_allocations(journal, monkeypatch):
    root, session, write = journal
    write([row(str(i), task='historical') for i in range(20000)] +
          [row('selected/' + str(i)) for i in range(1000)])
    # Warm the complete read path, including recovery's lazy imports, before
    # measuring steady-state projection allocations. Cold reads are tested below.
    mods.get_task_modification_paths(root, 'task', conv_id='conv', limit=1)
    monkeypatch.setattr(mods, '_load_from_disk', lambda *_: pytest.fail('warm reread'))
    monkeypatch.setattr(mods.undo_payloads, 'hydrate_record',
                        lambda *_: pytest.fail('diagnostics hydrated content'))
    tracemalloc.start()
    try:
        paths = mods.get_task_modification_paths(root, 'task', conv_id='conv', limit=200)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(paths) == 200
    assert paths[0] == {'path': 'selected/0', 'rootPath': root}
    assert peak < 256 * 1024  # A 21k-row shallow copy alone exceeds this with filtering.


def test_exact_attribution_distinct_addresses_and_cache_replacement(journal):
    root, session, write = journal
    write([row('foreign', task='other'), row('legacy', task=None),
           row('wrong-conv', conv='other'), row('remote', basePath='remote:host'),
           row('same'), row('same'), row('second'), row('third')])
    paths = mods.get_task_modification_paths(root, 'task', conv_id='conv', limit=2,
                                            exclude=[('same', root)])
    assert paths == [{'path': name, 'rootPath': root} for name in ('second', 'third')]
    paths[0]['path'] = 'mutated'
    assert mods.get_task_modification_paths(root, 'task', limit=1)[0]['path'] == 'wrong-conv'
    write([row('replacement')])
    assert mods.get_task_modification_paths(root, 'task', limit=1) == [
        {'path': 'replacement', 'rootPath': root}]
    Path(session, 'modifications.json').write_text('{broken')
    with pytest.raises(ValueError):
        mods.get_task_modification_paths(root, 'task', limit=1)


def test_capture_preserves_evidence_across_release_and_global_limit(journal, monkeypatch):
    root, _, write = journal
    write([row(str(i)) for i in range(500)])
    task = {'id': 'task', 'convId': 'conv', 'config': {'projectPath': root},
            'toolRounds': [{'toolName': 'run_command', 'status': 'done',
                            'toolArgs': json.dumps({'working_dir': root})}]}
    captured = capture_workspace_evidence(task)
    assert len(captured['files']) == 200
    assert captured['workingDirs'] == [root]
    task['toolRounds'] = None
    write([])
    monkeypatch.setattr(mods, 'get_task_modification_paths',
                        lambda *a, **kw: pytest.fail('full evidence reread'))
    repeated = capture_workspace_evidence(task)
    repeated['files'][0]['path'] = 'changed'
    assert captured['files'][0]['path'] == '0'
    assert len(repeated['files']) == 200


@pytest.mark.parametrize('limit', [-1, True, 1.5])
def test_invalid_limit_refuses(journal, limit):
    with pytest.raises(ValueError):
        mods.get_task_modification_paths(journal[0], 'task', limit=limit)


def test_capture_passes_remaining_count_across_roots(monkeypatch):
    calls = []

    def select(root, task_id, *, conv_id, limit, exclude):
        calls.append((root, task_id, conv_id, limit, tuple(exclude)))
        count = min(limit, 150)
        return [{'path': str(i), 'rootPath': root} for i in range(count)]

    monkeypatch.setattr(mods, 'get_task_modification_paths', select)
    task = {'id': 'exact-task', 'convId': 'exact-conv',
            '_workspaceDeliveryEvidence': {
                'files': [{'path': 'prior', 'rootPath': '/one'}]}}
    evidence = capture_workspace_evidence(task, '/one', ['/one', '/two', '/three'])
    assert len(evidence['files']) == 200
    assert [(call[0], call[3]) for call in calls] == [('/one', 199), ('/two', 49)]
    assert all(call[1:3] == ('exact-task', 'exact-conv') for call in calls)
    assert calls[0][4] == (('prior', '/one'),)
    assert len(calls[1][4]) == 151
