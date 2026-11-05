"""Durable entry binds the physical writer once, independently of API lifetime."""
import copy

import pytest

from lib.process_owner import process_owner
from lib.storage.errors import StorageError
from tests.storage import test_conversation_worker_publication as root

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api
execution = root.execution
KEY = '_conversationWorkerExecution'


def private_attempt(execution):
    from lib.turn_lifecycle import get_attempt
    return get_attempt(execution[2]['attempt']['attemptId'], user_id=execution[1],
                       include_execution_config=True)


def start(execution, job, process):
    return root.command(execution, 'turn.attempt.start',
        attempt_id=execution[2]['attempt']['attemptId'], task_id=job['taskId'],
        worker_claim=root.proof(job), worker_process=process)


def test_entry_witness_is_atomic_immutable_and_private(execution):
    job, process = root.claim(execution), process_owner()
    assert process is not None
    first = start(execution, job, process)
    assert first == start(execution, job, process)
    assert KEY not in first and 'config' not in first
    witness = private_attempt(execution)['config'][KEY]
    assert witness == {'format': 'tofu.conversation-worker-execution/v1',
                       'claim': root.proof(job), 'process': process}
    changed = {**process, 'started': str(int(process['started']) + 1)}
    with pytest.raises(StorageError):
        start(execution, job, changed)
    assert private_attempt(execution)['config'][KEY] == witness
    # Read-only authority probes do not replace or need to repeat the witness.
    assert root.start(execution, worker_claim=root.proof(job)) == first


def test_missing_entry_witness_cannot_be_backfilled_after_effects(execution):
    job = root.claim(execution)
    root.start(execution, worker_claim=root.proof(job))
    with pytest.raises(StorageError):
        start(execution, job, process_owner())
    assert KEY not in private_attempt(execution)['config']


def test_malformed_witness_refuses_without_entering(execution):
    job, process = root.claim(execution), process_owner()
    for bad in ({}, {**process, 'pid': True}, {**process, 'started': '-1'},
                {**process, 'pid_namespace': 7}, {**process, 'extra': 'untrusted'}):
        with pytest.raises(StorageError):
            start(execution, job, bad)
        assert private_attempt(execution)['status'] == 'pending'
    before = copy.deepcopy(private_attempt(execution))
    root.command(execution, 'worker_job.request_cancel', task_id=job['taskId'],
                 now_ms=1, reason='Stop')
    with pytest.raises(StorageError):
        start(execution, job, process)
    assert private_attempt(execution) == before
