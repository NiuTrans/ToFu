"""Evolution gate: no evidence no mutation; activation and rollback chain."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 12


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _settled_episode(store, episode_id='e1', *, gap_summary=''):
    from lib import evolution
    evolution.admit_goal({
        'schema_version': 1, 'goal_id': f'g-{episode_id}',
        'statement': 'goal', 'domain': 'code', 'exploratory': False,
        'criteria': [{'criterion_id': 'c1', 'kind': 'test_command',
                      'params': {'command': 'x'}}],
        'budget_envelope': {'max_tokens': 100, 'max_wall_clock_seconds': 10,
                            'max_tool_calls': 1},
        'status': 'draft', 'created_at': 1}, user_id=USER)
    evolution.save_plan({
        'schema_version': 1, 'plan_id': f'p-{episode_id}',
        'goal_id': f'g-{episode_id}', 'plan_revision': 1,
        'candidates': [{
            'path_id': 'path-a', 'title': 'P',
            'stages': [{'kind': 'execute', 'detail': 'd'}],
            'resource_refs': [],
            'cost_estimate': {
                'expected_tokens': 10, 'expected_wall_clock_seconds': 10,
                'expected_tool_calls': 1, 'failure_rate_prior': 0.5,
                'expected_information_gain': 0.5, 'expected_utility': 0.5,
                'priors_source': 'exploratory_flat'},
            'utility_over_cost': 1.0}],
        'selected_path_id': 'path-a', 'selection_rationale': 'r',
        'requires_confirmation': True, 'status': 'confirmed',
        'created_at': 1}, user_id=USER)
    episode = evolution.open_episode(goal_id=f'g-{episode_id}',
                                     plan_id=f'p-{episode_id}',
                                     selected_path_id='path-a',
                                     task_id=f't-{episode_id}', user_id=USER)
    episode_id = episode['episode_id']
    evolution.settle_episode(
        episode_id, user_id=USER, status='failure',
        actuals={'tokens': 10, 'wall_clock_seconds': 5, 'tool_calls': 1},
        verdict={'outcome': 'failure', 'assessed_by': 'checker',
                 'criterion_results': []},
        learning={'resource_weight_deltas': [], 'cost_actual_vs_estimate': None,
                  'strategy_outcome': None,
                  'capability_gap': ({
                      'gap_id': 'raw', 'summary': gap_summary,
                      'proposed_action': 'add the tool'} if gap_summary else None)})
    return episode_id


def test_mutation_requires_settled_episode_evidence(store):
    from lib import evolution
    with pytest.raises(evolution.EvolutionStateError):
        evolution.propose_mutation('prompt_variant', {'text': 'x'}, [],
                                   user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.propose_mutation('prompt_variant', {'text': 'x'},
                                   ['episode-that-never-ran'], user_id=USER)


def test_mutation_lifecycle_confirm_activate_rollback_restores_parent(store):
    from lib import evolution
    e1 = _settled_episode(store, 'e1')
    e2 = _settled_episode(store, 'e2')
    first = evolution.propose_mutation('routing_change', {'model': 'a'}, [e1],
                                       user_id=USER, now=100)
    assert first['version'] == 1 and first['parent_version'] == 0
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(first['mutation_id'], user_id=USER)
    evolution.confirm_mutation(first['mutation_id'], user_id=USER, now=101)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(first['mutation_id'], user_id=USER)
    evolution.run_regression(first, user_id=USER, now=101)
    evolution.activate_mutation(first['mutation_id'], user_id=USER, now=102,
                                allow_unmeasurable=True)

    second = evolution.propose_mutation('routing_change', {'model': 'b'}, [e2],
                                        user_id=USER, now=103)
    assert second['version'] == 2 and second['parent_version'] == 1
    evolution.confirm_mutation(second['mutation_id'], user_id=USER, now=104)
    evolution.run_regression(second, user_id=USER, now=104)
    evolution.activate_mutation(second['mutation_id'], user_id=USER, now=105,
                                allow_unmeasurable=True)
    by_id = {m['mutation_id']: m
             for m in evolution.list_mutations(user_id=USER)}
    assert by_id[first['mutation_id']]['status'] == 'rolled_back'
    assert by_id[second['mutation_id']]['status'] == 'active'

    evolution.rollback_mutation(second['mutation_id'], user_id=USER, now=106)
    by_id = {m['mutation_id']: m
             for m in evolution.list_mutations(user_id=USER)}
    assert by_id[second['mutation_id']]['status'] == 'rolled_back'
    assert by_id[first['mutation_id']]['status'] == 'active'  # parent restored
    with pytest.raises(evolution.EvolutionStateError):
        evolution.confirm_mutation(second['mutation_id'], user_id=USER)


def test_rejection_is_terminal_and_carries_reason(store):
    from lib import evolution
    e1 = _settled_episode(store, 'e1')
    mutation = evolution.propose_mutation('skill_candidate', {'name': 's'},
                                          [e1], user_id=USER)
    evolution.reject_mutation(mutation['mutation_id'], user_id=USER,
                              reason='holdout regressed')
    stored = evolution.list_mutations(user_id=USER)[0]
    assert stored['status'] == 'rejected'
    assert stored['rejection_reason'] == 'holdout regressed'
    with pytest.raises(evolution.EvolutionStateError):
        evolution.activate_mutation(mutation['mutation_id'], user_id=USER)


def test_confirm_runs_the_structural_sandbox(store):
    from lib import evolution
    e1 = _settled_episode(store, 'e1')
    bad_macro = evolution.propose_mutation(
        'macro_tool', {'sequence': 'not-a-list'}, [e1], user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.confirm_mutation(bad_macro['mutation_id'], user_id=USER)
    bad_prompt = evolution.propose_mutation(
        'prompt_variant', {'no_text': True}, [e1], user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.confirm_mutation(bad_prompt['mutation_id'], user_id=USER)


def test_capability_gaps_dedup_by_summary_with_lineage(store):
    from lib import evolution
    _settled_episode(store, 'e1', gap_summary='cannot parse pdf tables')
    _settled_episode(store, 'e2', gap_summary='cannot parse pdf tables')
    _settled_episode(store, 'e3', gap_summary='no ocr capability')
    queue = evolution.collect_capability_gaps(user_id=USER, now=100)
    assert len(queue) == 2
    tables = next(g for g in queue if 'pdf' in g['summary'])
    assert tables['samples'] == 2
    assert len(tables['lineage']) == 2
    assert tables['status'] == 'proposed'
    again = evolution.collect_capability_gaps(user_id=USER, now=200)
    assert len(again) == 2  # re-collection stays deduplicated
    assert evolution.list_capability_gaps(user_id=99) == []


def test_original_evidence_beyond_prefix_cap_can_support_a_proposal(store):
    from copy import deepcopy
    from lib import evolution
    from lib.evolution import records
    eid=_settled_episode(store,'z-original')
    original=deepcopy(store.rows[records.NAMESPACE_EPISODES,f'{USER}:{eid}'])
    for index in range(205):
        older={**deepcopy(original),'episode_id':f'a-history-{index:03}'}
        store.rows[records.NAMESPACE_EPISODES,f"{USER}:{older['episode_id']}"]=older
    assert eid not in {row['episode_id'] for row in evolution.list_episodes(user_id=USER,limit=200)}
    reads=[];query=store.query
    def measured(operation,payload):
        reads.append((operation,deepcopy(payload)));return query(operation,payload)
    store.query=measured
    proposal=evolution.propose_mutation('prompt_variant',{'text':'Scoped improvement'},[eid],user_id=USER)
    assert proposal['evidence_episode_ids']==[eid] and proposal['status']=='proposed'
    assert proposal['requires_confirmation'] is True
    assert not any(op=='record.list' and payload['namespace']==records.NAMESPACE_EPISODES for op,payload in reads)
    assert sum(op=='record.get' and payload['namespace']==records.NAMESPACE_EPISODES for op,payload in reads)==1
    assert sum(op=='record.get' and payload['namespace']==records.NAMESPACE_GOALS for op,payload in reads)==1


@pytest.mark.parametrize('invalid',['running','aborted','exploratory','missing_parent','foreign_owner','wrong_identity','contradictory_verdict','missing_time','exploratory_parent'])
def test_invalid_original_never_mints_a_mutation_even_with_a_favorable_window(store,invalid):
    from copy import deepcopy
    from lib.evolution import gate,records
    from lib.storage.errors import StorageError
    eid=_settled_episode(store)
    favorable=deepcopy(store.rows[records.NAMESPACE_EPISODES,f'{USER}:{eid}'])
    episode=store.rows[records.NAMESPACE_EPISODES,f'{USER}:{eid}']
    owner=USER
    if invalid in ('running','aborted'):
        episode.update(status=invalid,verdict=None)
        if invalid=='running':episode['settled_at']=None
    elif invalid=='exploratory':episode['exploratory']=True
    elif invalid=='missing_parent':store.rows.pop((records.NAMESPACE_GOALS,f"{USER}:{episode['goal_id']}"))
    elif invalid=='foreign_owner':owner=USER+1
    elif invalid=='wrong_identity':episode['episode_id']='different-original'
    elif invalid=='missing_time':episode['settled_at']=None
    elif invalid=='exploratory_parent':store.rows[records.NAMESPACE_GOALS,f"{USER}:{episode['goal_id']}"]['exploratory']=True
    else:episode['verdict']['outcome']='success'
    before=store.writes
    with pytest.raises((gate.EvolutionStateError,StorageError)):
        gate.propose_mutation('prompt_variant',{'text':'x'},[eid],user_id=owner,
            episode_reader=lambda **kwargs:[favorable])
    assert store.writes==before


def test_oversized_evidence_is_refused_before_original_reads_and_never_truncated(store):
    from lib.evolution import gate
    from lib.evolution.contracts import EvolutionContractError
    store.query=lambda *args:pytest.fail('invalid evidence read storage')
    with pytest.raises(EvolutionContractError):
        gate.propose_mutation('prompt_variant',{'text':'x'},['unknown']*33,user_id=USER)


def test_duplicate_references_preserve_input_without_repeated_original_reads(store):
    from lib import evolution
    from lib.evolution import records
    eid=_settled_episode(store);reads=[];query=store.query
    def measured(operation,payload):
        if operation=='record.get' and payload['namespace']==records.NAMESPACE_EPISODES:reads.append(payload)
        return query(operation,payload)
    store.query=measured
    proposal=evolution.propose_mutation('prompt_variant',{'text':'x'},[eid,eid],user_id=USER)
    assert proposal['evidence_episode_ids']==[eid,eid] and len(reads)==1


from tests.research.test_research_workspace_evidence_guard import storage as storage


def test_exact_mutation_evidence_survives_selected_store_restart(storage, store, monkeypatch):
    from copy import deepcopy
    from lib.evolution import records,gate
    eid=_settled_episode(store)
    original=deepcopy(store.rows[records.NAMESPACE_EPISODES,f'{USER}:{eid}'])
    parent=deepcopy(store.rows[records.NAMESPACE_GOALS,f"{USER}:{original['goal_id']}"])
    supervisor,_=storage
    monkeypatch.setattr(records,'_storage',lambda **kwargs:supervisor.client)
    records.put_record(records.NAMESPACE_EPISODES,f'{USER}:{eid}',original,kind='episode')
    records.put_record(records.NAMESPACE_GOALS,f"{USER}:{parent['goal_id']}",parent,kind='goal')
    supervisor.stop();supervisor.start()
    proposal=gate.propose_mutation('prompt_variant',{'text':'Verified original reference'},[eid],user_id=USER)
    assert proposal['status']=='proposed' and proposal['evidence_episode_ids']==[eid]
    assert proposal['requires_confirmation'] is True
    with pytest.raises(gate.EvolutionStateError):
        gate.propose_mutation('prompt_variant',{'text':'foreign'},[eid],user_id=USER+1)
