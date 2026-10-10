"""Failed candidates remain owner/control scoped, bounded and CAS-published."""
from copy import deepcopy
import hashlib
import json

import pytest

from lib.evolution import programs, records
from lib.evolution.contracts import EvolutionContractError
from lib.evolution.validation_candidate_index import FailedCandidates, FAILED, NAMESPACE
from lib.research.source_index import empty_reference
from lib.storage.errors import StorageError
from tests.support.evolution_store import EvolutionRecordStore
from tests.evolution.test_evolution_validation_generation import failed_validation as failed_validation, accept_diagnosis
from tests.evolution.test_evolution_runs import runtime as runtime, OWNER
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


@pytest.fixture
def frozen_step():
    # Pure membership tests do not launch tasks or borrow application storage.
    from tests.evolution.test_evolution_scientific_observations import protocol
    from tests.support.research_observations import synthetic_frozen_scope
    frozen = protocol();frozen['measurement_scope'] = synthetic_frozen_scope(frozen)
    return {'kind':'experiment', 'title':'Validate frozen candidate', 'instruction':'Measure the frozen candidate',
        'checks':['Frozen target is met.'], 'requires_scientific_protocol':True,
        'requires_frozen_measurement_scope':True, 'scientific_protocol':frozen,
        'scientific_required_outcome':'supported'}


def scoped(client, step, **overrides):
    options = dict(user_id=OWNER, program_id='index-program', step=step, cache_entries=65)
    return FailedCandidates(client, **(options | overrides))


def test_many_candidates_use_bounded_cold_lookup_and_preserve_old_roots(frozen_step):
    step = frozen_step
    store = EvolutionRecordStore()
    index = scoped(store, step)
    reference = empty_reference()
    keys = [hashlib.sha256(str(number).encode()).hexdigest() for number in range(1200)]
    first = None
    for key in keys:
        reference = index.insert(reference, key, FAILED)
        if first is None:
            first = deepcopy(reference)
    assert reference['count'] == len(keys) and len(index.cache) <= index.capacity
    original = store.query
    calls = []
    def counted(operation, payload):
        calls.append(payload)
        return original(operation, payload)
    store.query = counted
    lookup_counts = []
    for key in (keys[0], keys[600], keys[-1], 'f'*64):
        calls.clear()
        cold = scoped(store, step)
        assert cold.contains(reference, key) == (key in keys)
        assert len(calls) <= 65
        lookup_counts.append(len(calls))
    assert scoped(store, step).contains(first, keys[0])
    assert not scoped(store, step).contains(first, keys[-1])
    node_bytes = sum(len(json.dumps(row, separators=(',', ':')).encode()) for row in store.rows.values())
    print('failed candidate index measurement:', json.dumps({'candidates': len(keys),
        'prepared_nodes': len(store.rows), 'node_json_bytes': node_bytes, 'cold_lookup_reads': lookup_counts}))


@pytest.mark.parametrize('change', ['owner', 'program', 'dataset', 'baseline', 'budget', 'outcome', 'checks'])
def test_foreign_controls_cannot_read_published_root(frozen_step, change):
    step = deepcopy(frozen_step);store = EvolutionRecordStore()
    reference = scoped(store, step).insert(empty_reference(), 'c'*64, FAILED)
    override = {}
    if change == 'owner': override['user_id'] = OWNER + 1
    elif change == 'program': override['program_id'] = 'other-program'
    elif change == 'dataset': step['scientific_protocol']['measurement_scope']['dataset_digest'] = 'a'*64
    elif change == 'baseline': step['scientific_protocol']['measurement_scope']['baseline_digest'] = 'd'*64
    elif change == 'budget': step['scientific_protocol']['measurement_scope']['budget'] += 1
    elif change == 'outcome': step['scientific_required_outcome'] = 'refuted'
    else: step['checks'].append('Another requirement.')
    with pytest.raises(StorageError, match='missing or invalid'):
        scoped(store, step, **override).contains(reference, 'c'*64)


def test_equal_budget_notation_shares_control_binding(frozen_step):
    step = deepcopy(frozen_step);store = EvolutionRecordStore()
    reference = scoped(store, step).insert(empty_reference(), 'c'*64, FAILED)
    step['scientific_protocol']['measurement_scope']['budget'] = float(step['scientific_protocol']['measurement_scope']['budget'])
    assert scoped(store, step).contains(reference, 'c'*64)


@pytest.mark.parametrize('damage', ['missing', 'changed', 'false_ack', 'wrong_fact'])
def test_corrupt_or_unacknowledged_nodes_refuse(frozen_step, damage):
    store = EvolutionRecordStore();step = frozen_step;index = scoped(store, step)
    if damage == 'false_ack':
        store.command = lambda *args: {'version': 1}
        with pytest.raises(StorageError, match='missing or invalid'):
            index.insert(empty_reference(), 'c'*64, FAILED)
        return
    reference = index.insert(empty_reference(), 'c'*64, FAILED if damage != 'wrong_fact' else 'd'*64)
    key = (NAMESPACE, index.prefix + reference['root'])
    if damage == 'missing': store.rows.pop(key)
    elif damage == 'changed': store.rows[key]['facts'] = 'e'*64
    with pytest.raises(StorageError): scoped(store, step).contains(reference, 'c'*64)


def test_lost_ack_recovers_only_identical_node(frozen_step):
    store = EvolutionRecordStore();original = store.command
    def lost(operation, payload, command_id):
        original(operation, payload, command_id)
        raise StorageError('database_internal', 'ack lost')
    store.command = lost
    index = scoped(store, frozen_step)
    reference = index.insert(empty_reference(), 'c'*64, FAILED)
    assert index.contains(reference, 'c'*64)
    assert index.insert(reference, 'c'*64, FAILED) == reference


def test_program_cas_failure_does_not_publish_prepared_index(runtime, failed_validation, monkeypatch):
    active, step, protocol, task, _ = failed_validation
    prepare = programs._prepare_failed_candidate_index
    store = runtime[1];command = store.command
    def prepared_then_conflict(*args, **kwargs):
        reference = prepare(*args, **kwargs)
        def conflict(operation, payload, command_id):
            if payload['namespace'] == programs.PROGRAMS:
                raise StorageError('database_conflict', 'concurrent frontier writer')
            return command(operation, payload, command_id)
        monkeypatch.setattr(store, 'command', conflict)
        return reference
    monkeypatch.setattr(programs, '_prepare_failed_candidate_index', prepared_then_conflict)
    with pytest.raises(StorageError, match='concurrent frontier writer'):
        accept_diagnosis(runtime, active, task)
    published = programs.get_program(active['program_id'], user_id=OWNER)
    assert published['queue'][1] == step
    assert 'failed_candidate_index' not in published['queue'][1]
    assert any(namespace == NAMESPACE for namespace, _ in store.rows)
    monkeypatch.setattr(store, 'command', command)
    monkeypatch.setattr(programs, '_prepare_failed_candidate_index', prepare)
    admitted = programs.advance_program(active['program_id'], user_id=OWNER)
    successor = admitted['queue'][1]
    assert successor['failed_candidate_index']['count'] == 1
    assert successor['validation_parent_sequence'] == 1


def test_clients_cannot_supply_index_reference(frozen_step):
    step = deepcopy(frozen_step)
    step['failed_candidate_index'] = {'version': 1, 'root': 'a'*64, 'count': 1}
    step['validation_parent_sequence'] = 1
    with pytest.raises(EvolutionContractError, match='lineage'):
        programs.create_program({'objective': 'Forge a trusted index', 'steps': [step,
            {'kind':'final', 'title':'Audit', 'instruction':'Audit', 'checks':['Done.']}]},
            user_id=OWNER, idempotency_key='forged-index')


def test_membership_survives_selected_storage_restart(storage, frozen_step):
    supervisor, _ = storage
    step = frozen_step
    index = scoped(supervisor.client, step)
    first = index.insert(empty_reference(), 'c'*64, FAILED)
    latest = index.insert(first, 'd'*64, FAILED)
    supervisor.stop();supervisor.start()
    reopened = scoped(supervisor.client, step)
    assert reopened.contains(latest, 'c'*64) and reopened.contains(latest, 'd'*64)
    assert not reopened.contains(first, 'd'*64)


@pytest.mark.parametrize('damage', ['missing_root', 'omitted_parent'])
def test_preregistration_refuses_damaged_index_without_legacy_fallback(runtime, failed_validation, damage):
    active, step, protocol, task, _ = failed_validation
    accept_diagnosis(runtime, active, task)
    programs.advance_program(active['program_id'], user_id=OWNER)
    synthesis = programs.advance_program(active['program_id'], user_id=OWNER)
    index = FailedCandidates(runtime[1], user_id=OWNER, program_id=active['program_id'], step=step)
    if damage == 'missing_root':
        root = synthesis['queue'][1]['failed_candidate_index']['root']
        runtime[1].rows.pop((NAMESPACE, index.prefix + root))
    else:
        synthesis['queue'][1]['failed_candidate_index'] = index.insert(empty_reference(), 'e'*64, FAILED)
        records.put_record(programs.PROGRAMS,
            records.entity_key(active['program_id'], OWNER, kind='program'), synthesis, kind='program')
    before = programs.get_program(active['program_id'], user_id=OWNER)
    candidate = deepcopy(protocol);candidate['measurement_scope']['candidate_digest'] = 'f'*64
    writes = runtime[1].writes
    with pytest.raises(StorageError):
        programs.preregister_validation(active['program_id'],
            programs.pending_validations(before)[0]['target_step_sha256'], candidate, 'supported',
            user_id=OWNER, executor_run_id=before['current']['run_id'])
    assert runtime[1].writes == writes
    assert programs.get_program(active['program_id'], user_id=OWNER) == before
