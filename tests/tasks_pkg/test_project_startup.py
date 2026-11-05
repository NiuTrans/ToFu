"""Shared startup preserves task-owned roots, optional services and edit races."""
from __future__ import annotations

import pytest

from lib.tasks_pkg.orchestrator import _startup

pytestmark = pytest.mark.unit


def test_project_roots_remain_task_owned_when_optional_presence_fails(monkeypatch):
    calls = []
    monkeypatch.setattr('lib.project_mod.ensure_project_state',
                        lambda path, **kw: calls.append((path, kw)))
    monkeypatch.setattr('lib.tasks_pkg.manager.task_user_id', lambda task: 7)

    def fail_presence(*args, **kwargs):
        assert kwargs['user_id'] == 7
        raise RuntimeError('presence unavailable')

    monkeypatch.setattr('lib.presence.announce', fail_presence)
    monkeypatch.setattr('lib.tasks_pkg.manager._presence_keepalive.ensure_started',
                        lambda: calls.append('keepalive'))
    monkeypatch.setattr('lib.file_history.is_enabled', lambda: True)
    monkeypatch.setattr('lib.file_history.probe_enabled', lambda: True)
    monkeypatch.setattr(_startup, 'start_external_edit_probe',
                        lambda task, path: calls.append(('probe', task['id'], path)))
    _startup.setup_project_context(
        {'id': 'task-a', 'convId': 'conversation-a'},
        {'projectPaths': ['/project', '/reference', '', '/project'],
         'readOnlyPaths': ['', '/reference']},
        '/project', True,
    )
    assert calls == [
        ('/project', {'extra_paths': ['/reference'], 'conv_id': 'task-a',
                      'readonly_paths': ['/reference']}),
        'keepalive', ('probe', 'task-a', '/project'),
    ]


def test_project_root_failure_propagates_before_optional_work(monkeypatch):
    failure = RuntimeError('cannot establish task roots')

    def fail_roots(*args, **kwargs):
        raise failure

    monkeypatch.setattr('lib.project_mod.ensure_project_state', fail_roots)
    with pytest.raises(RuntimeError) as caught:
        _startup.setup_project_context({'id': 'task-a'}, {}, '/project', True)
    assert caught.value is failure


@pytest.mark.parametrize('change', [
    {'aborted': True}, {'status': 'done'}, {'modifiedFiles': True},
    {'modifiedFileList': ['written.txt']}, {},
])
def test_external_probe_never_publishes_stale_edits(monkeypatch, change):
    task = {'id': 'task-a', 'status': 'running'}
    events = []

    def detect(path, **kwargs):
        task.update(change)
        return {'committed': True, 'files': ['edited.txt'], 'snapshotId': 'snapshot'}

    monkeypatch.setattr('lib.file_history.detect_external_edits', detect)
    monkeypatch.setattr(_startup, 'append_event',
                        lambda task, event: events.append(event))
    _startup._probe_external_edits(task, '/project')
    assert len(events) == (0 if change else 1)
    if events:
        assert events[0]['files'] == ['edited.txt']
        assert events[0]['sha'] == 'snapshot'
