"""Workspace delivery extends existing receipts, not task/turn completion."""
import pytest

from lib.tasks_pkg import workspace_delivery as delivery

pytestmark = pytest.mark.unit


def test_round_committed_retains_files_and_reports_delivery_separately(monkeypatch):
    from lib import file_history as history
    from lib import linear_git_checkpoint
    from lib.tasks_pkg.commit_round import _commit, _derive
    import lib.project_mod as project_mod

    task = {'id': 'task', 'convId': 'conv', '_userId': 7, 'status': 'done',
            'config': {'projectPath': '/project'}, 'toolRounds': [],
            'modifiedFileList': [{'path': 'owned.py', 'rootPath': '/project'}]}
    receipt = {'schema': 'tofu.workspace-delivery/v1', 'repositories': [{
        'canonicalRoot': '/project', 'workspacePath': '/isolated',
        'workId': 'task', 'deliveryStatus': 'pending', 'outcome': 'completed'}]}
    events, checkpoint_calls = [], []
    monkeypatch.setattr(delivery, 'settle_task_workspaces', lambda *a, **kw: receipt)
    monkeypatch.setattr(delivery, 'checkpoint_roots_without_workspaces', lambda *a: [])
    monkeypatch.setattr(_derive, 'derive_round_modified_files', lambda *a, **kw: (
        list(task['modifiedFileList']), 1, False))
    monkeypatch.setattr(history, 'is_enabled', lambda: False)
    monkeypatch.setattr(project_mod, 'get_modifications', lambda *a, **kw: [])
    monkeypatch.setattr(_commit, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(_commit, '_patch_turn_projection_with_file_list', lambda *a, **kw: None)
    monkeypatch.setattr(linear_git_checkpoint, 'settle_task_checkpoint',
                        lambda *a, **kw: checkpoint_calls.append(kw))
    _commit._run_commit_round_async(task, '/project', None, False)
    assert checkpoint_calls == []
    event = next(event for event in events if event['type'] == 'round_committed')
    assert event['modifiedFileList'] == [{'path': 'owned.py', 'rootPath': '/project'}]
    assert event['workspaceDelivery']['repositories'][0]['deliveryStatus'] == 'pending'
    assert task['status'] == 'done'


def test_project_brain_settles_with_canonical_key_and_distinct_delivery_summary(monkeypatch):
    from lib.conversations import project_brain as brain
    receipt = {'repositories': [{'canonicalRoot': '/project', 'workspacePath': '/isolated',
                                 'workId': 'task', 'deliveryStatus': 'pending'}]}
    task = {'id': 'task', 'convId': 'conv', '_userId': 7, 'status': 'done',
            'finishReason': 'stop',
            'config': {'projectPath': '/isolated'}, '_projectWorkId': 'work',
            '_projectWorkPath': '/project', '_workspaceDelivery': receipt,
            'partial_content': 'Implemented change.'}
    commands, settled = [], []
    monkeypatch.setattr(delivery, 'settle_task_workspaces', lambda *a, **kw: settled.append(a))
    monkeypatch.setattr(brain, '_command', lambda operation, path, **kw: commands.append((operation, path, kw)))
    monkeypatch.setattr(brain, 'run_matching_checkers', lambda *a, **kw: None)
    assert brain.settle_work_item(task, '/isolated') == 'completed'
    assert settled
    finish = commands[-1][2]
    assert commands[-1][1] == '/project'
    assert finish['status'] == 'completed'
    assert 'pending' in finish['result_summary']
    assert 'not delivery confirmation' in finish['result_summary']


@pytest.mark.parametrize('stored,expected', [
    ({'status': 'done', 'finishReason': 'stop'}, 'completed'),
    ({'status': 'done'}, 'failed'),
    ({'status': 'done', 'finishReason': 'length'}, 'failed'),
    ({'status': 'done', 'finishReason': 'stop', 'streamState': 'premature_close'}, 'failed'),
    ({'status': 'done', 'error': 'fatal'}, 'failed'),
    ({'status': 'done', 'aborted': True}, 'cancelled'),
    ({'status': 'running'}, 'failed'),
])
def test_startup_terminal_outcomes_preserve_owner_and_do_not_authorize_failed_release(
        monkeypatch, stored, expected):
    from lib.conversations import project_brain_startup as startup
    calls, finishes = [], []
    class Client:
        def maintenance(self, *args, **kwargs):
            return {'projects': [{'ownerUserId': 7, 'projectKey': '/project',
                                  'workItems': [{'id': 'work', 'taskId': 'task', 'status': 'active'}]}],
                    'capped': False}
        def query(self, operation, payload, **kwargs):
            if operation == 'record.list':
                assert kwargs['deadline'] > 0
                return []
            assert payload['user_id'] == 7
            return dict(stored)
        def command(self, operation, payload, command_id):
            finishes.append(payload)
    def settle(task, root):
        calls.append((task, root, delivery.task_outcome(task)))
    monkeypatch.setattr(startup, 'get_storage_client', lambda **kw: Client())
    monkeypatch.setattr(delivery, 'settle_task_workspaces', settle)
    assert startup.recover_active_work_items() == 1
    assert calls[0][0]['_userId'] == 7
    assert calls[0][1:] == ('/project', expected)
    assert finishes[0]['status'] == expected
