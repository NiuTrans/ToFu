"""Research execution claims fence duplicate workers before model/tool effects."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.research.execution_owner import ActionExecutionOwners, NAMESPACE
from lib.storage.errors import StorageError
from tests.research.test_research_action_observations import MemoryRecords, intent_repository
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def repository(client):
    intent_repository(client)
    return ActionExecutionOwners.open(client, user_id=1, task_id='research_action_observed')


def test_unique_worker_nonce_and_lost_ack_do_not_authorize_another_executor():
    client = MemoryRecords(); repo = repository(client); client.lose_ack = True
    claim = repo.claim_initial(); claim.assert_current()
    with pytest.raises(StorageError): repo.claim_initial()
    assert repo.inspect_owner()['replay_allowed'] is False
    assert ActionExecutionOwners.open(client, user_id=2, task_id='research_action_observed') is None


def test_changed_owner_fences_old_handle():
    client = MemoryRecords(); repo = repository(client); claim = repo.claim_initial()
    client.rows[NAMESPACE, repo.prefix]['value']['nonce'] = '0' * 32
    with pytest.raises(StorageError, match='no longer owns'): claim.assert_current()


def test_inherited_handle_cannot_execute_in_another_process(monkeypatch):
    import lib.research.execution_owner as owners
    client = MemoryRecords(); repo = repository(client); claim = repo.claim_initial()
    if claim.value['process'] is None: pytest.skip('local process evidence unavailable')
    changed = deepcopy(claim.value['process']); changed['pid'] += 1
    monkeypatch.setattr(owners, 'process_owner', lambda: changed)
    with pytest.raises(StorageError): claim.assert_current()


def test_unknown_process_evidence_does_not_block_initial_execution_or_grant_replay(monkeypatch):
    import lib.research.execution_owner as owners
    monkeypatch.setattr(owners, 'process_owner', lambda: None)
    repo = repository(MemoryRecords()); claim = repo.claim_initial(); claim.assert_current()
    assert repo.inspect_owner() == {'found': True, 'process_state': 'unknown', 'replay_allowed': False}
    with pytest.raises(StorageError): repo.claim_initial()


def test_selected_storage_restart_preserves_claim_and_blocks_duplicate(storage):
    supervisor, _ = storage
    repo = repository(supervisor.client); claim = repo.claim_initial()
    supervisor.stop(); supervisor.start()
    reopened = ActionExecutionOwners.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.inspect_owner()['found']
    with pytest.raises(StorageError): reopened.claim_initial()
    assert reopened._get(NAMESPACE, reopened.prefix, 'executionOwner') == claim.value
    with pytest.raises(StorageError): claim.assert_current()  # old transport was explicitly closed


def test_concurrent_selected_store_claim_has_exactly_one_winner(storage):
    supervisor, _ = storage; repo = repository(supervisor.client)
    def attempt(_):
        try: return repo.claim_initial().value['nonce']
        except StorageError: return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        winners = [value for value in executor.map(attempt, range(2)) if value is not None]
    assert len(winners) == 1


@pytest.mark.parametrize('started', ['', 'broken', '-1', '²'])
def test_malformed_process_start_is_unknown_not_proof_of_loss(started):
    from lib.process_owner import process_owner, owner_state
    current = process_owner()
    if current is None: pytest.skip('local process evidence unavailable')
    assert owner_state({**current, 'started': started}) == 'unknown'
