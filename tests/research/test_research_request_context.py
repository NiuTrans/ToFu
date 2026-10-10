"""Exact private request snapshots, chunk sharing and post-restart verification."""
from copy import deepcopy
import pytest

from lib.research.request_context import RequestContexts, NAMESPACE, PART_NAMESPACE
from lib.research.model_accounting import ModelCalls
from lib.storage.errors import StorageError
from tests.research.test_research_model_accounting import repository, snapshot
from tests.research.test_research_action_observations import MemoryRecords
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def contexts(client):
    repository(client)
    return RequestContexts.open(client, user_id=1, task_id='research_action_observed')


def admit(client, digest, ordinal=1, snapshot_expected=True):
    before = snapshot(ordinal - 1); before['agent_dispatches'] = ordinal
    return ModelCalls.open(client, user_id=1, task_id='research_action_observed').admit_model_call(
        ordinal=ordinal, round_index=ordinal, requested_model='controlled', request_digest=digest,
        max_output=8000, usage_before=before, request_snapshot_version=1 if snapshot_expected else None)


def material():
    return {'messages': [{'role': 'system', 'content': 'frozen system instruction'},
            {'role': 'user', 'content': '汉字🧪' * 30000}], 'tools': [{'type': 'function'}], 'max_output': 8000}


def test_snapshot_requires_admission_and_restores_exact_material_with_no_workspace_substitution():
    client = MemoryRecords(); repo = contexts(client); original = material()
    digest = repo.save_request(original)
    assert repo.load_request(1) is None
    admit(client, digest)
    original['messages'][1]['content'] = 'changed workspace'
    reopened = RequestContexts.open(client, user_id=1, task_id='research_action_observed')
    assert reopened.load_request(1) == material()
    assert RequestContexts.open(client, user_id=2, task_id='research_action_observed') is None


def test_prefix_chunks_are_shared_across_rounds_but_intents_remain_distinct():
    client = MemoryRecords(); repo = contexts(client); first = material()
    digest = repo.save_request(first); intent = admit(client, digest)
    ModelCalls.open(client, user_id=1, task_id='research_action_observed').record_model_usage(
        intent, snapshot(), usage_mapping_received=False)
    first_count = sum(namespace == PART_NAMESPACE for namespace, _ in client.rows)
    second = deepcopy(first); second['messages'].append({'role': 'assistant', 'content': 'new tool history'})
    next_digest = repo.save_request(second); admit(client, next_digest, 2)
    second_count = sum(namespace == PART_NAMESPACE for namespace, _ in client.rows)
    assert first_count > 1 and second_count < first_count * 2
    assert repo.load_request(1) == first and repo.load_request(2) == second


@pytest.mark.parametrize('fault', ['content', 'missing', 'manifest', 'order', 'binding'])
def test_corrupt_request_never_returns_partial_material(fault):
    client = MemoryRecords(); repo = contexts(client); digest = repo.save_request(material()); admit(client, digest)
    manifest = client.rows[(NAMESPACE, repo.prefix + digest)]['value']
    key = (PART_NAMESPACE, repo.prefix + manifest['parts'][0])
    if fault == 'content': client.rows[key]['value']['content'] = 'z' * len(client.rows[key]['value']['content'])
    elif fault == 'missing': del client.rows[key]
    elif fault == 'manifest': del client.rows[(NAMESPACE, repo.prefix + digest)]
    elif fault == 'order': manifest['parts'].reverse()
    else: client.rows[key]['value']['binding']['user_id'] = 2
    with pytest.raises(StorageError): repo.load_request(1)


def test_request_snapshot_survives_selected_storage_restart(storage):
    supervisor, _ = storage
    repo = contexts(supervisor.client); digest = repo.save_request(material()); admit(supervisor.client, digest)
    supervisor.stop(); supervisor.start()
    reopened = RequestContexts.open(supervisor.client, user_id=1, task_id='research_action_observed')
    assert reopened.load_request(1) == material()


def test_legacy_digest_without_snapshot_stays_missing():
    client = MemoryRecords(); repo = contexts(client); admit(client, 'd' * 64, snapshot_expected=False)
    assert repo.load_request(1) is None


def defined_material():
    return {'messages': [], 'tools': [], 'max_output':8000, 'action_definition':{
        'direction':'Frozen basis','lang':'en','action':'experiment','expected_revision':7,
        'confirm_external_writes':True,'model_override':'controlled',
        'logical_budget':{'token_budget':320000,'dispatch_budget':10},'tool_epoch_hash':'declared-epoch'}}


@pytest.mark.parametrize('field,value', [('action','analyze'),('confirm_external_writes',False),
    ('model_override','different'),('tool_epoch_hash','new-epoch'),('expected_revision',8),
    ('direction','Other goal'),('lang','zh'),('logical_budget',{'token_budget':400000,'dispatch_budget':10})])
def test_action_definition_cannot_change_after_first_admission(field, value):
    client = MemoryRecords(); repo = contexts(client); original = defined_material()
    digest = repo.save_request(original); admit(client, digest)
    changed = deepcopy(original); changed['action_definition'][field] = value
    before = deepcopy(client.rows)
    with pytest.raises(StorageError): repo.save_request(changed)
    assert client.rows == before and repo.load_request(1) == original


@pytest.mark.parametrize('change', ['remove','retrofit'])
def test_definition_presence_cannot_be_retrofitted_mid_action(change):
    client = MemoryRecords(); repo = contexts(client); original = defined_material()
    if change == 'retrofit':original.pop('action_definition')
    digest = repo.save_request(original); admit(client, digest)
    changed = defined_material()
    if change == 'remove':changed.pop('action_definition')
    with pytest.raises(StorageError, match='definition changed'):repo.save_request(changed)


@pytest.mark.parametrize('field,value', [('token_budget',400000),('dispatch_budget',20),('model_override','wrong')])
def test_model_admission_checks_declared_action_policy_before_intent(field, value):
    client = MemoryRecords(); repo = contexts(client); original = defined_material()
    if field == 'model_override':original['action_definition'][field] = value
    else:original['action_definition']['logical_budget'][field] = value
    digest = repo.save_request(original)
    with pytest.raises(StorageError, match='original action policy'):admit(client,digest)
    assert ModelCalls.open(client,user_id=1,task_id='research_action_observed').load_model_intent(1) is None


def test_retained_definition_is_verified_against_original_payload():
    client = MemoryRecords(); repo = contexts(client); original = defined_material()
    digest = repo.save_request(original); admit(client,digest)
    client.rows[NAMESPACE,repo.prefix+digest]['value']['action_definition']['action'] = 'analyze'
    with pytest.raises(StorageError,match='acknowledged material'):repo.load_request(1)


def test_original_definition_survives_selected_store_restart(storage):
    supervisor, backend = storage; repo = contexts(supervisor.client); original = defined_material()
    digest = repo.save_request(original); admit(supervisor.client,digest)
    supervisor.stop();supervisor.start()
    restored = RequestContexts.open(supervisor.client,user_id=1,task_id='research_action_observed')
    assert restored.load_request(1) == original


def test_definition_payload_growth_is_measured(record_property):
    import json
    original = defined_material(); legacy = dict(original); legacy.pop('action_definition')
    encode = lambda value: json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    extra_bytes = len(encode(original)) - len(encode(legacy))
    assert extra_bytes < 1024
    record_property('action_definition_extra_bytes',extra_bytes)


@pytest.mark.parametrize('change',['action','confirmation','epoch','remove','retrofit'])
def test_prestaged_requests_cannot_change_definition_at_actual_admission(change):
    client = MemoryRecords(); repo = contexts(client); first = defined_material(); second = deepcopy(first)
    if change == 'action':second['action_definition']['action'] = 'analyze'
    elif change == 'confirmation':second['action_definition']['confirm_external_writes'] = False
    elif change == 'epoch':second['action_definition']['tool_epoch_hash'] = 'another-epoch'
    elif change == 'remove':second.pop('action_definition')
    else:first.pop('action_definition')
    first_digest = repo.save_request(first); second_digest = repo.save_request(second)
    intent = admit(client,first_digest)
    calls = ModelCalls.open(client,user_id=1,task_id='research_action_observed')
    calls.record_model_usage(intent,snapshot(),usage_mapping_received=False)
    with pytest.raises(StorageError,match='original action definition'):
        admit(client,second_digest,ordinal=2)
    assert calls.load_model_intent(2) is None


def test_prestaged_same_definition_allows_new_context_without_resetting_usage():
    client = MemoryRecords(); repo = contexts(client); first = defined_material(); second = deepcopy(first)
    second['messages'].append({'role':'tool','content':'New observed evidence'})
    first_digest = repo.save_request(first); second_digest = repo.save_request(second)
    intent = admit(client,first_digest)
    calls = ModelCalls.open(client,user_id=1,task_id='research_action_observed')
    calls.record_model_usage(intent,snapshot(),usage_mapping_received=False)
    next_intent = admit(client,second_digest,ordinal=2)
    assert next_intent['usage_before']['calls'] == 1
    assert repo.load_request(2) == second
