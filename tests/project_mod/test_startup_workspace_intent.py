"""Boot acknowledges durable delivery debt without waiting for checkout capture."""
import sys

import pytest

from lib import integration_control as ic
from lib import integration_state_repository as state
from lib.conversations import project_brain_startup as startup
from lib.project_mod import delivery
from lib.tasks_pkg import workspace_delivery
from tests.storage.test_integration_delivery import (
    automatic_workspace, git, repo as repo,
)

pytestmark = pytest.mark.unit


def recovery_client(root, owner, task, active, finishes):
    class Client:
        def maintenance(self, operation, payload, **kwargs):
            assert operation == 'project_brain.recovery.snapshot'
            return {'projects': [{'ownerUserId': owner, 'projectKey': str(root),
                                  'workItems': list(active)}], 'capped': False}

        def query(self, operation, payload, **kwargs):
            if operation == 'record.list':
                return []
            assert operation == 'task_results.replay_get'
            assert payload['user_id'] == owner
            return dict(task)

        def command(self, operation, payload, command_id):
            assert operation == 'project_brain.work.finish'
            finishes.append(payload)
            active.clear()

    return Client()


@pytest.mark.parametrize('task,outcome', [
    ({'status': 'done', 'metadata': {'finishReason': 'stop'}}, 'completed'),
    ({'status': 'error', 'error': 'interrupted'}, 'failed'),
    ({'status': 'done', 'aborted': True}, 'cancelled'),
])
def test_boot_hands_off_capture_and_worker_recovers_exact_bytes(repo, monkeypatch, task, outcome):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'recovered.txt').write_text('durable pending change\n')
    before_head = git(root, 'rev-parse', 'HEAD')
    active = [{'id': 'work', 'taskId': 'automatic', 'status': 'active'}]
    finishes, captures = [], []
    monkeypatch.setattr(startup, 'get_storage_client', lambda **kw:
                        recovery_client(root, owner, task, active, finishes))

    def unavailable_capture(*args, **kwargs):
        captures.append(args)
        raise RuntimeError('checkout capture is unavailable during boot')

    with monkeypatch.context() as boot:
        boot.setattr(ic, 'checkpoint_workspace', unavailable_capture)
        assert startup.recover_active_work_items() == 1
        assert captures == []
        assert startup.recover_active_work_items() == 0
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert row['origin']['delivery']['state'] == 'pending'
    assert row['origin']['delivery']['taskOutcome'] == outcome
    assert finishes[0]['status'] == outcome
    assert 'pending' in finishes[0]['result_summary']
    assert git(root, 'rev-parse', 'HEAD') == before_head
    assert not (root / 'recovered.txt').exists()
    assert (work / 'recovered.txt').read_text() == 'durable pending change\n'

    # A fresh worker pass consumes only the persisted receipt after boot.
    assert delivery.resume_pending_workspace_deliveries()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    if outcome == 'completed':
        assert row['origin']['delivery']['state'] == 'delivered'
        assert (root / 'recovered.txt').read_text() == 'durable pending change\n'
    else:
        assert row['origin']['delivery']['state'] == 'preserved'
        assert not (root / 'recovered.txt').exists()
        assert git(root, 'show', row['checkpoint_sha'] + ':recovered.txt') == 'durable pending change'


def test_failed_intent_commit_keeps_active_work_retryable(repo, monkeypatch):
    root, _, owner = repo
    automatic_workspace(repo, monkeypatch)
    active = [{'id': 'work', 'taskId': 'automatic', 'status': 'active'}]
    finishes = []
    task = {'status': 'done', 'metadata': {'finishReason': 'stop'}}
    monkeypatch.setattr(startup, 'get_storage_client', lambda **kw:
                        recovery_client(root, owner, task, active, finishes))
    save = delivery._save

    def unavailable_commit(row, receipt, **changes):
        if changes.get('taskOutcome'):
            raise RuntimeError('intent commit unavailable')
        return save(row, receipt, **changes)

    with monkeypatch.context() as outage:
        outage.setattr(delivery, '_save', unavailable_commit)
        assert startup.recover_active_work_items() == 0
    assert active and finishes == []
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert row['origin']['delivery']['state'] == 'working'
    assert startup.recover_active_work_items() == 1
    assert active == []


def test_intent_ack_does_not_suppress_later_live_delivery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'later.txt').write_text('deliver after intent acknowledgement\n')
    task = {'id': 'automatic', '_userId': owner, 'status': 'done',
            'finishReason': 'stop', 'config': {'projectPath': str(root)}}
    pending = workspace_delivery.record_task_workspace_intents(task)
    assert pending['repositories'][0]['deliveryStatus'] == 'pending'
    completed = workspace_delivery.settle_task_workspaces(task)
    assert completed['repositories'][0]['deliveryStatus'] == 'delivered'
    assert (root / 'later.txt').read_text() == 'deliver after intent acknowledgement\n'


def test_recorded_revocation_cannot_be_upgraded_during_recovery(repo, monkeypatch):
    root, _, owner = repo
    work = automatic_workspace(repo, monkeypatch)
    (work / 'preserved.txt').write_text('cancelled task change\n')
    first = ic.record_task_workspace_intent(
        str(root), 'automatic', user_id=owner, outcome='cancelled')
    second = ic.record_task_workspace_intent(
        str(root), 'automatic', user_id=owner, outcome='completed')
    assert second == first
    assert second['taskOutcome'] == 'cancelled'
    assert delivery.resume_pending_workspace_deliveries()
    assert not (root / 'preserved.txt').exists()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    assert row['origin']['delivery']['state'] == 'preserved'


def test_crash_after_intent_ack_before_work_finish_retries_without_capture(repo, monkeypatch):
    root, _, owner = repo
    automatic_workspace(repo, monkeypatch)
    active = [{'id': 'work', 'taskId': 'automatic', 'status': 'active'}]
    finishes = []
    client = recovery_client(root, owner,
                             {'status': 'done', 'metadata': {'finishReason': 'stop'}},
                             active, finishes)
    monkeypatch.setattr(startup, 'get_storage_client', lambda **kw: client)

    def unavailable_finish(*args, **kwargs):
        raise RuntimeError('process lost after intent acknowledgement')

    with monkeypatch.context() as crash:
        crash.setattr(client, 'command', unavailable_finish)
        with pytest.raises(RuntimeError, match='process lost'):
            startup.recover_active_work_items()
    row = state.get_workspace(str(root), 'automatic', user_id=owner)
    acknowledged = row['origin']['delivery']
    assert acknowledged['state'] == 'pending'
    assert acknowledged['taskOutcome'] == 'completed'
    assert active and not finishes
    assert startup.recover_active_work_items() == 1
    assert not active and len(finishes) == 1
    assert state.get_workspace(str(root), 'automatic', user_id=owner)['origin']['delivery'] == acknowledged


def test_tasks_without_workspace_work_do_not_load_integration(monkeypatch):
    monkeypatch.setitem(sys.modules, 'lib.integration_control', None)
    for handler in (workspace_delivery.settle_task_workspaces,
                    workspace_delivery.record_task_workspace_intents):
        for task in ({}, {'id': 'transient', '_transientRuntime': True},
                     {'id': 'rejected', '_workspaceDeliveryOwnershipRejected': True},
                     {'id': 'no-roots'}):
            assert handler(task) is None
