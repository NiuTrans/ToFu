"""Swarm acknowledgement is atomic with replacement of the durable session."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time
from uuid import uuid4

import pytest

from lib.storage_authority_sql.operations_pkg import _runs
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend
from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient

pytestmark = pytest.mark.unit


def save(client, task_id):
    return client.command('swarm.session.save', dict(swarm_key='swarm', task_id=task_id,
        specs=[], config={'user_id': 7}, now_ms=1), str(uuid4()))


def seed(client):
    save(client, 'old')
    client.command('swarm.agent.save', dict(swarm_key='swarm', agent_id='worker',
        status='completed', messages=[], result={'final_answer': 'evidence'},
        delivered=False, now_ms=2), str(uuid4()))


def acknowledge(client, task_id):
    return client.command('swarm.agents.acknowledge_results',
        dict(swarm_key='swarm', task_id=task_id, agent_ids=['worker', 'worker']), str(uuid4()))


@pytest.mark.parametrize('current_task', ['new', 'n' * 600], ids=['ordinary', 'long'])
def test_replacement_refuses_stale_ack_and_current_ack_is_repeatable(backend, current_task):
    client = TransactionClient(backend)
    seed(client)
    save(client, current_task)
    assert acknowledge(client, 'old') == {'acknowledged': False, 'changed': 0}
    row = client.query('swarm.session.get', {'swarm_key': 'swarm'})
    assert not row['agents'][0]['delivered']
    assert acknowledge(client, current_task) == {'acknowledged': True, 'changed': 1}
    assert acknowledge(client, current_task) == {'acknowledged': True, 'changed': 1}
    assert client.query('swarm.session.get', {'swarm_key': 'swarm'})['agents'][0]['delivered']


def test_replacement_commit_fences_an_ack_already_waiting_on_storage(backend):
    client = TransactionClient(backend)
    seed(client)
    entered, release, submitted = threading.Event(), threading.Event(), threading.Event()
    def replace(session):
        _runs._swarm_session_save(session, dict(swarm_key='swarm', task_id='new',
            specs=[], config={'user_id': 7}, now_ms=3))
        entered.set()
        assert release.wait(10)
    def ack():
        submitted.set()
        return acknowledge(client, 'old')
    with ThreadPoolExecutor(max_workers=2) as executor:
        replacing = executor.submit(backend.command, 'test.swarm.replace', '', None,
            'user', replace, time.monotonic()+20, receipt_required=False)
        assert entered.wait(5)
        acknowledging = executor.submit(ack)
        try:
            assert submitted.wait(5)
            with pytest.raises(TimeoutError):
                acknowledging.result(timeout=0.1)
        finally:
            release.set()
        replacing.result(timeout=10)
        assert acknowledging.result(timeout=10) == {'acknowledged': False, 'changed': 0}
    assert not client.query('swarm.session.get', {'swarm_key': 'swarm'})['agents'][0]['delivered']
