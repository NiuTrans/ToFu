"""Oversized science recovers only from exact owner/basis-bound original bytes."""
from copy import deepcopy
import json
import pytest

from lib.research.original_science import original_science_loader
from lib.research.action_observations import freeze_action_observation_basis, PART_NAMESPACE
from lib.research.action import capture_research_result, _tool_receipts
from lib.research.persistence import research_direction_hash
from lib.research.program import dependency_digests
from lib.research.science_reduction import reduce_scientific_receipts
from lib.storage.errors import StorageError
from tests.research.test_research_paged_success import initial_science, complete, commit
from tests.research.test_research_pending_paging import save, DIRECTION
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage
from tests.support.research_observations import observation_payload

pytestmark = pytest.mark.unit


def original(client, monkeypatch, *, outcome='supported', task_id='large-source', bound_source=False):
    import lib.storage
    monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kwargs: client)
    workspace = initial_science(outcome)
    if bound_source:
        workspace.update(selected_idea_id='idea-1', selected_idea_source_task_id='discovery-original')
    workspace['protocol']['dataset'] = 'dataset-' + 'x' * 1500
    workspace['runs'][0].update(dependency_digests(workspace))
    payload = dict(observation_payload(workspace['protocol'], 'artifact:1', outcome),
                   log='x'*20000, job={'id':'job-B','status':'completed'})
    content = json.dumps(payload)
    row = {'toolName':'poll','status':'done','toolCallId':'science','roundNum':1}
    capture_research_result(row, content)
    receipt = _tool_receipts([row])[0]
    assert receipt['observation_incomplete'] is True
    task = {'user_id':1,'task_id':task_id,'direction':DIRECTION}
    repository = freeze_action_observation_basis(task, workspace)
    repository.capture(ordinal=1, call_id='science', tool='poll', content=content, receipt=row['_researchReceipt'])
    receipt.update(source_task_id=task_id, artifact_ref='artifact:1')
    receipt['execution_observation']['provider_scope'] = 'provider:cluster'
    workspace['runs'][0]['tool_receipts'][1] = receipt
    return workspace, receipt, repository


def loader(client, workspace):
    return original_science_loader(client, user_id=1, paper_hash=research_direction_hash(DIRECTION),
                                 lang='en', run=workspace['runs'][0])


def test_verified_original_restores_large_science_without_rewriting_receipt(monkeypatch):
    client = MemoryRecords(); workspace, receipt, _ = original(client, monkeypatch)
    before = deepcopy(receipt)
    summary = reduce_scientific_receipts(iter([receipt]), expected_outcome='supported',
        protocol=workspace['protocol'], evidence_refs=['artifact:1'], original_loader=loader(client, workspace))
    assert summary['outcome_consistent'] and all(summary['checks'].values())
    assert receipt == before and receipt['observation_incomplete'] is True


@pytest.mark.parametrize('mutation', ['hash','call','tool','basis','language'])
def test_conflicting_or_foreign_original_cannot_supply_science(monkeypatch, mutation):
    client = MemoryRecords(); workspace, receipt, _ = original(client, monkeypatch)
    if mutation == 'hash': receipt['result_digest'] = 'f'*64
    elif mutation == 'call': receipt['call_id'] = 'other'
    elif mutation == 'tool': receipt['tool'] = 'other'
    elif mutation == 'basis': workspace['runs'][0]['hypothesis_digest'] = 'f'*64
    if mutation == 'language':
        read = original_science_loader(client, user_id=1, paper_hash=research_direction_hash(DIRECTION), lang='zh', run=workspace['runs'][0])
    else: read = loader(client, workspace)
    with pytest.raises(StorageError): read(receipt)


def test_corrupt_original_part_is_rejected_before_scientific_payload_is_exposed(monkeypatch):
    client = MemoryRecords(); workspace, receipt, _ = original(client, monkeypatch)
    key = next(key for namespace,key in client.rows if namespace == PART_NAMESPACE)
    client.rows[(PART_NAMESPACE,key)]['value']['content'] = 'corrupted'
    with pytest.raises(StorageError, match='part is missing or invalid'):
        loader(client, workspace)(receipt)


def test_missing_legacy_original_stays_unverified_without_external_replay(monkeypatch):
    client = MemoryRecords(); workspace, receipt, _ = original(client, monkeypatch)
    receipt['source_task_id'] = 'missing-original'
    summary = reduce_scientific_receipts([receipt], expected_outcome='supported', protocol=workspace['protocol'],
        evidence_refs=['artifact:1'], original_loader=loader(client, workspace))
    assert not summary['outcome_consistent'] and not any(summary['checks'].values())


@pytest.mark.parametrize('bound_source', [False, True])
def test_oversized_science_survives_selected_storage_restart_and_success_settlement(storage, monkeypatch, bound_source):
    supervisor, _ = storage
    if bound_source:
        from lib.research import records
        monkeypatch.setattr(records, '_storage', lambda **kwargs: supervisor.client)
        records.persist_record({'task_id':'discovery-original', 'user_id':1, 'direction':DIRECTION,
            'lang':'en', 'status':'done', 'result':{'accepted':[{'id':'idea-1'}]}})
    workspace, receipt, _ = original(supervisor.client, monkeypatch, bound_source=bound_source)
    prior = save(workspace)
    supervisor.stop(); supervisor.start()
    saved = commit(supervisor.client, complete(supervisor.client, prior), prior)
    assert saved['runs'][0]['scientific_outcome'] == 'supported'
    assert saved['decision']['status'] == 'retain' and all(saved['runs'][0]['receipt_evidence']['checks'].values())
    assert receipt['observation_incomplete'] is True
    from lib.research.workspace import read_run_scientific_state
    inspected = read_run_scientific_state(DIRECTION, user_id=1, run_id='run')
    assert inspected['outcome_consistent'] and all(inspected['checks'].values())


def test_large_original_refutation_is_not_overridden_by_later_large_support(monkeypatch):
    client = MemoryRecords()
    workspace, early, _ = original(client, monkeypatch, outcome='refuted')
    _, late, _ = original(client, monkeypatch, task_id='late-source')
    summary = reduce_scientific_receipts([early, late], expected_outcome='supported', protocol=workspace['protocol'],
        evidence_refs=['artifact:1'], original_loader=loader(client, workspace))
    assert summary['scientific_outcome'] == 'inconclusive' and not summary['outcome_consistent']


def test_ambiguous_call_id_cannot_choose_an_arbitrary_original(monkeypatch):
    client = MemoryRecords(); workspace, receipt, repository = original(client, monkeypatch)
    manifest, content = repository.read(1)
    repository.capture(ordinal=2, call_id=manifest['call_id'], tool=manifest['tool'], content=content, receipt=manifest['receipt'])
    with pytest.raises(StorageError, match='ambiguous or changed'): loader(client, workspace)(receipt)


def test_missing_legacy_frozen_basis_stays_unverified(monkeypatch):
    from lib.research.action_observations import BASIS_NAMESPACE
    client = MemoryRecords(); workspace, receipt, repository = original(client, monkeypatch)
    del client.rows[(BASIS_NAMESPACE, repository.prefix)]
    assert loader(client, workspace)(receipt) is None


def test_bound_large_science_restores_from_same_frozen_selection(monkeypatch):
    client = MemoryRecords()
    workspace, receipt, repository = original(client, monkeypatch, bound_source=True)
    basis = repository.load_basis()
    assert basis['selected_idea_id'] == 'idea-1'
    assert basis['selected_idea_source_task_id'] == 'discovery-original'
    restored = loader(client, workspace)(receipt)
    assert restored['scientific_observations']['comparison']
    assert receipt['observation_incomplete']


@pytest.mark.parametrize('field,value', [('selected_idea_id','other-idea'),
    ('selected_idea_source_task_id','other-discovery')])
def test_bound_original_cannot_rebind_to_different_selection(monkeypatch, field, value):
    client = MemoryRecords()
    workspace, receipt, _ = original(client, monkeypatch, bound_source=True)
    workspace[field] = value
    workspace['runs'][0].update(dependency_digests(workspace))
    with pytest.raises(StorageError, match='basis'):
        loader(client, workspace)(receipt)


def test_unbound_legacy_original_does_not_acquire_current_source_identity(monkeypatch):
    client = MemoryRecords()
    workspace, receipt, _ = original(client, monkeypatch)
    workspace.update(selected_idea_id='idea-1', selected_idea_source_task_id='new-discovery')
    workspace['runs'][0].update(dependency_digests(workspace))
    with pytest.raises(StorageError, match='basis'):
        loader(client, workspace)(receipt)
