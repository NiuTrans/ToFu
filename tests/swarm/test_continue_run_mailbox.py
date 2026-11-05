"""Continue keeps the launch run mailbox; snapshot read failures stay bounded.

Restart Continue rebuilds a session with the original launch ``task_id`` while
the accepted Continue task is its live execution parent. Mailbox identity must
follow the run, not that parent, or every receipt read and child acquisition
fails (incident 2026-10-23: messageReceiptsAvailable=false, thousands/hour).
"""
from copy import deepcopy
import logging
from types import SimpleNamespace

import pytest

from lib.storage.errors import StorageError
from lib.swarm import message_receipts, message_service
from lib.swarm.master import MasterOrchestrator
from lib.swarm.protocol import SubTaskSpec
from lib.swarm.run_messages import RunMessageError, binding_for, inject_messages
from tests.swarm.test_run_messages import _message_snapshot_master, args, context as context  # noqa: F401

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_failure_log(monkeypatch):
    monkeypatch.setattr(message_receipts, '_snapshot_failures', type(message_receipts._snapshot_failures)())


def continued_master(context, *specs):
    resume = {**deepcopy(context.task), 'id': 'resume-task'}
    master = MasterOrchestrator('launch', 'conv-a', [context.spec, *specs], user_id=7,
                                parent_task=resume, parent_config=resume['config'],
                                inbox_key='conv-a')
    # The production Continue shape: live parent identity, stable run identity.
    assert master._parent_task_proxy['id'] == 'resume-task'
    assert master.task_id == 'launch'
    return master, resume


def test_continue_keeps_launch_mailbox_for_snapshot_children_and_root(context, caplog):
    before_restart = message_service.send(context.task, args(), context.session)
    master, resume = continued_master(context)

    with caplog.at_level(logging.WARNING, logger='lib.swarm.message_receipts'):
        snapshot = master._build_agent_snapshot()
    assert snapshot['messageReceiptsAvailable'] is True
    assert [row['messageId'] for row in snapshot['messageReceipts']] == [before_restart['message_id']]
    assert set(snapshot['messageReceiptsVersions']) == {'child-a', 'launch'}
    assert 'Swarm receipt snapshot read failed' not in caplog.text

    binding = master._make_agent(context.spec)._message_binding()
    assert binding == context.binding
    child = {'id': 'runtime-child-2', '_userId': 7, 'config': {}, '_parentTaskId': 'resume-task',
             '_swarmMessageBinding': binding}
    messages = [{'role': 'user', 'content': 'Objective'}]
    inject_messages(child, messages, 0)
    assert 'Extra evidence' in messages[-1]['content']

    after = message_service.send(resume, args(idempotency_key='after-continue'), master)
    assert after['state'] == 'submitted'
    assert len(context.repo.load()[0]['messages']) == 2


def test_continued_worker_keeps_team_identity_only_as_the_live_child(context, monkeypatch):
    sibling = SubTaskSpec(id='child-b', role='coder', objective='Review')
    master, _ = continued_master(context, sibling)
    monkeypatch.setattr('lib.swarm.integration._get_session',
                        lambda key: master if key == 'conv-a' else None)
    worker = {'id': 'runtime-child-2', '_userId': 7, 'config': {}, '_parentTaskId': 'resume-task',
              '_swarmMessageBinding': master._make_agent(context.spec)._message_binding()}
    master._agents['child-a'] = SimpleNamespace(_child=worker)

    receipt = message_service.send(worker, args(agent_id='child-b'), master)
    assert receipt['state'] == 'submitted'
    read = message_service.read(worker, {'agent_id': 'child-b', 'swarm_run_id': 'launch',
                                         'message_id': receipt['message_id']})
    assert read['messages'][0]['state'] == 'submitted'

    foreign = {**deepcopy(worker), '_parentTaskId': 'other-root'}
    with pytest.raises(RunMessageError, match='original owned team identity'):
        message_service.send(foreign, args(agent_id='child-b', idempotency_key='foreign'), master)
    monkeypatch.setattr('lib.swarm.integration._get_session', lambda key: None)
    with pytest.raises(RunMessageError, match='original owned team identity'):
        message_service.send(worker, args(agent_id='child-b', idempotency_key='late'), master)


def test_snapshot_read_failure_logs_bounded_code_once_per_run_interval(context, monkeypatch, caplog):
    now = [1000.0]
    monkeypatch.setattr(message_receipts, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    master = _message_snapshot_master(context)
    other = MasterOrchestrator('other-run', 'conv-a', [context.spec], user_id=7)
    other._parent_task_proxy, other.inbox_key = context.task, 'conv-a'
    query = context.client.query

    def offline(*a, **kw):
        raise StorageError('database_unavailable', 'synthetic-private-detail')

    def failures():
        return [record for record in caplog.records
                if record.getMessage().startswith('Swarm receipt snapshot read failed')]

    monkeypatch.setattr(context.client, 'query', offline)
    with caplog.at_level(logging.INFO, logger='lib.swarm.message_receipts'):
        for _ in range(5):
            assert master._build_agent_snapshot()['messageReceiptsAvailable'] is False
        [first] = failures()
        message = first.getMessage()
        assert first.levelno == logging.WARNING
        assert 'run=launch' in message and 'failed_targets=2/2' in message
        assert 'error=StorageError:database_unavailable@' in message
        assert 'suppressed_repeats=0' in message
        assert 'synthetic-private-detail' not in caplog.text

        other._build_agent_snapshot()
        assert [r.getMessage().split()[5] for r in failures()] == ['run=launch', 'run=other-run']

        now[0] += message_receipts._SNAPSHOT_FAILURE_LOG_INTERVAL_S
        master._build_agent_snapshot()
        assert 'suppressed_repeats=4' in failures()[-1].getMessage()
        master._build_agent_snapshot()
        master._build_agent_snapshot()
        assert len(failures()) == 3

        monkeypatch.setattr(context.client, 'query', query)
        assert master._build_agent_snapshot()['messageReceiptsAvailable'] is True
        assert 'recovered' not in caplog.text  # A flapping run stays inside its window.
        now[0] += message_receipts._SNAPSHOT_FAILURE_LOG_INTERVAL_S
        assert master._build_agent_snapshot()['messageReceiptsAvailable'] is True
        assert 'Swarm receipt snapshot read recovered run=launch suppressed_repeats=2' in caplog.text

        monkeypatch.setattr(context.client, 'query', offline)
        master._build_agent_snapshot()
        assert len(failures()) == 4 and 'suppressed_repeats=0' in failures()[-1].getMessage()


def test_snapshot_failure_log_state_is_bounded(context, monkeypatch):
    monkeypatch.setattr(message_receipts, '_SNAPSHOT_FAILURE_LOG_RUNS', 3)
    for index in range(10):
        message_receipts._report_snapshot_failure(f'run-{index}', 'StorageError:x@f.py:g', 1, 1)
    assert list(message_receipts._snapshot_failures) == ['run-7', 'run-8', 'run-9']


def test_binding_identity_requires_the_run_not_the_live_parent(context):
    resume = {**deepcopy(context.task), 'id': 'resume-task'}
    assert binding_for(resume, context.spec, 'conv-a', run_id='launch') == context.binding
