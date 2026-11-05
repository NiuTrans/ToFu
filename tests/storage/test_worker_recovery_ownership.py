"""API restart recovery cannot settle work owned by another execution process."""
import uuid

import pytest

from tests.storage.test_conversation_worker_publication import (
    command, execution as execution, proof, snapshot, start,
)
from tests.support.worker_job_time import wall_ms

pytest_plugins = ('tests.support.semantic_authorities',)
pytestmark = pytest.mark.api


@pytest.mark.parametrize('entered', [False, True])
def test_legacy_recovery_preserves_worker_root_phases_and_checkpoint(execution, entered):
    from lib import turn_lifecycle as turns
    factory, owner, pair, job = execution
    owned = command(execution, 'worker_job.claim_next', worker_id='live-worker',
        task_kinds=['conversation-attempt'], now_ms=wall_ms(), lease_ms=300_000)
    if entered:
        start(execution, worker_claim=proof(owned))
        command(execution, 'turn.visible.sync', conversation_id=pair['turn']['conversationId'],
            attempt_id=pair['attempt']['attemptId'], root_turn_id=pair['turn']['turnId'],
            messages=[{'role': 'assistant', 'content': 'worker phase'}], start_index=1,
            live=True, worker_claim=proof(owned))
    status = 'running' if entered else 'pending'
    value = {'task_id': job['taskId'], 'user_id': owner,
        'conv_id': pair['turn']['conversationId'], 'status': status,
        'content': 'received evidence', 'thinking': ''}
    command(execution, 'task_results.checkpoint', key=job['taskId'], value=value,
            expected_version=0, worker_claim=proof(owned))
    before = snapshot(execution)
    checkpoint = factory().query('task_results.replay_get', {'key': job['taskId'], 'user_id': owner})
    assert factory().query('task_results.recovery_pending', {}) == {'pending': False}

    # Legacy work in the same authority must still recover; exclusions are not
    # a blanket disabling of restart recovery or a caller-supplied registry list.
    legacy_id = uuid.uuid4().hex
    legacy = turns.create_turn_pair(legacy_id, command_id=legacy_id,
        input_projection={'content': 'legacy'}, config={'model': 'controlled'}, user_id=owner,
        conversation_defaults={'allowCreate': True, 'title': 'Legacy', 'settings': {}})
    command(execution, 'task_results.checkpoint', key=legacy_id, expected_version=0,
        value={**value, 'task_id': legacy_id, 'conv_id': legacy_id})
    assert factory().query('task_results.recovery_pending', {}) == {'pending': True}
    command(execution, 'turn.recover')
    command(execution, 'task_results.recover_running', interrupted_reason='server_restart')
    assert snapshot(execution) == before
    assert factory().query('task_results.replay_get', {'key': job['taskId'], 'user_id': owner}) == checkpoint
    assert turns.get_attempt(legacy['attempt']['attemptId'], user_id=owner)['status'] == 'interrupted'
    assert factory().query('task_results.replay_get', {'key': legacy_id, 'user_id': owner})['status'] == 'interrupted'
    assert factory().query('task_results.recovery_pending', {}) == {'pending': False}


def test_queued_worker_attempt_is_not_an_api_orphan(execution):
    before = snapshot(execution)
    command(execution, 'turn.recover')
    assert snapshot(execution) == before
