"""Research-domain runs: the pipeline is the executing stage, its record the review.

The automated research cycle is a first-class evolution capability domain:
intake maps the direction onto a falsification-criterion goal, execution
binds ``produce_research`` as the stage task, and the terminal research run
record supplies the verdict — there is no generic judge task and no
auto-repair replay of an expensive harvest.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit
OWNER = 321


@pytest.mark.parametrize('cancelled', [False, True])
def test_real_research_worker_hands_committed_terminal_to_owning_run(runtime, tmp_path, monkeypatch, cancelled):
    from lib.research import api, engine, records as research_records, runtime as research_runtime, recipe
    from lib.evolution import get_episode
    runs, store, _, _ = runtime
    monkeypatch.setattr(api, 'produce_research', engine.produce_research)
    monkeypatch.setattr(api, 'get_research_task', store.real_research_task_reader)
    monkeypatch.setattr(research_records, '_storage', lambda **kwargs: store)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))
    spawned=[];monkeypatch.setattr(research_runtime._research_runtime, 'spawn', lambda *args: spawned.append(args))
    monkeypatch.setattr(recipe, 'build_research_from_direction', lambda *a, **k:
        {'accepted': [], 'rejected': [], 'survey_md': 'Unverified synthetic proposal.'})
    run=runs.submit_run({'objective': 'Committed production research terminal', 'domain': 'research'},user_id=OWNER)
    task=research_runtime._research_runtime.get_owned(run['task_id'],user_id=OWNER)
    try:
        if cancelled:runs.cancel_run(run['run_id'],user_id=OWNER)
        engine.run_research_task(task)
        settled=runs.get_run(run['run_id'],user_id=OWNER)
        assert settled['status']==('cancelled' if cancelled else 'needs_attention')
        episode=get_episode(run['episode_id'],user_id=OWNER)
        assert episode['status']==('aborted' if cancelled else 'partial')
        assert len(settled['receipts'])==1 and len(spawned)==1
        if not cancelled:assert settled['review']['criterion_results'][0]['outcome']=='unknown'
    finally:research_runtime._research_runtime.remove_owned(run['task_id'],user_id=OWNER)


def test_cancelled_research_sweep_consumes_lost_terminal_handoff_without_replay(runtime, tmp_path, monkeypatch):
    from lib.research import api, engine, records as research_records, runtime as research_runtime
    from lib.evolution import get_episode, automation
    runs,store,_,_=runtime
    monkeypatch.setattr(api,'produce_research',engine.produce_research)
    monkeypatch.setattr(api,'get_research_task',store.real_research_task_reader)
    monkeypatch.setattr(research_records,'_storage',lambda **kwargs:store)
    monkeypatch.setattr(engine,'research_root',lambda:str(tmp_path))
    spawned=[];monkeypatch.setattr(research_runtime._research_runtime,'spawn',lambda *args:spawned.append(args))
    monkeypatch.setattr(automation,'enqueue_task_terminal',lambda *args:(_ for _ in ()).throw(RuntimeError('lost terminal callback')))
    run=runs.submit_run({'objective':'Cancelled terminal handoff debt','domain':'research'},user_id=OWNER)
    task=research_runtime._research_runtime.get_owned(run['task_id'],user_id=OWNER)
    try:
        runs.cancel_run(run['run_id'],user_id=OWNER);engine.run_research_task(task)
        assert get_episode(run['episode_id'],user_id=OWNER)['status']=='running'
        runs.sweep_stale_runs(user_id=OWNER,grace_seconds=0)
        settled=runs.get_run(run['run_id'],user_id=OWNER)
        assert settled['status']=='cancelled' and len(settled['receipts'])==1
        assert get_episode(run['episode_id'],user_id=OWNER)['status']=='aborted'
        writes=store.writes;runs.sweep_stale_runs(user_id=OWNER,grace_seconds=0)
        assert store.writes==writes and len(spawned)==1
    finally:research_runtime._research_runtime.remove_owned(run['task_id'],user_id=OWNER)


@pytest.fixture
def runtime(monkeypatch):
    from tests.support.evolution_store import install_store
    store = install_store(monkeypatch)
    from lib.evolution import runs
    monkeypatch.setattr('lib.evolution.runs.resolve_run_model', lambda requested, **kw: requested)
    research_tasks = []
    calls = []

    def fake_produce(direction, *, lang, n_ideas, seed_arxiv_ids, harvest_n,
                     model, evolution, user_id, execution_limits=None):
        calls.append(dict(direction=direction, lang=lang, n_ideas=n_ideas,
                          seed_arxiv_ids=seed_arxiv_ids, harvest_n=harvest_n,
                          model=model, evolution=evolution, user_id=user_id, execution_limits=execution_limits))
        task = {'id': f"research-task-{len(calls)}", '_userId': user_id,
                'status': 'running', 'created_at': 1,
                'config': {'evolution': dict(evolution)}}
        research_tasks.append(task)
        return {'task_id': task['id'], 'deduped': False}

    import lib.research.api as research_api
    store.real_research_task_reader = research_api.get_research_task
    monkeypatch.setattr(research_api, 'produce_research', fake_produce)
    monkeypatch.setattr(research_api, 'get_research_task', lambda tid, user_id:
                        next((t for t in research_tasks
                              if t['id'] == tid and t['_userId'] == user_id), None))
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
                        next((t for t in research_tasks
                              if t['id'] == tid and t['_userId'] == user_id), None))
    yield runs, store, research_tasks, calls


def advance(runtime, task, record, monkeypatch):
    """Terminal-deliver the research task with ``record`` as the run record."""
    runs, _, _, _ = runtime
    import lib.research.records as research_records
    monkeypatch.setattr(research_records, 'load_record',
                        lambda task_id, *, user_id, internal=False: record)
    task.update(status='done', finished_at=task['created_at'] + 1,
                usage={'input_tokens': 10, 'output_tokens': 5})
    # Drive the run state machine the way the sweep/API does: advance_run is
    # the only consumer of a terminal task for a run record (the terminal
    # listener seam settles episodes via automation, never the run record).
    return runs.advance_run(
        task['config']['evolution']['run_id'], user_id=OWNER, terminal_task=task)


def test_research_pipeline_delivery_does_not_prove_scientific_acceptance(runtime, monkeypatch):
    runs, store, research_tasks, calls = runtime
    run = runs.submit_run(
        {'objective': 'long-context KV cache compression', 'domain': 'research',
         'research': {'lang': 'zh', 'n_ideas': 4, 'harvest_n': 40}},
        user_id=OWNER, idempotency_key='research-1')
    assert run['status'] == 'executing'
    assert run['domain'] == 'research'
    assert run['research'] == {'lang': 'zh', 'n_ideas': 4, 'harvest_n': 40}
    assert run['task_id'] == research_tasks[0]['id']
    assert len(calls) == 1
    call = calls[0]
    assert call['lang'] == 'zh' and call['n_ideas'] == 4 and call['harvest_n'] == 40
    assert call['evolution']['stage'] == 'executing'
    assert call['evolution']['run_id'] == run['run_id']

    from lib.evolution.goals import get_goal
    goal = get_goal(run['goal_id'], user_id=OWNER)
    assert goal['domain'] == 'research'
    assert goal['criteria'][0]['kind'] == 'falsification_protocol'
    assert goal['exploratory'] is True
    assert run['research_review_version'] == 1

    record = {'task_id': run['task_id'], 'direction': run['objective'],
              'status': 'done',
              'result': {'survey_md': 'Gap map over 40 papers.',
                         'accepted': [{'title': 'Evict-aware KV reuse'}], 'rejected': [1, 2]}}
    settled = advance(runtime, research_tasks[0], record, monkeypatch)
    assert settled['status'] == 'needs_attention'
    assert settled['review']['outcome'] == 'partial'
    assert settled['review']['criterion_results'][0]['outcome'] == 'unknown'
    assert settled['review']['assessed_by'] == 'checker'
    assert settled['review']['criterion_results'][0]['criterion_id'] == 'falsification'
    assert settled['review']['evidence_refs'] == [f"research-run:{run['task_id']}"]
    assert 'Evict-aware KV reuse' in settled['result']
    # Exactly one executing receipt: no generic judge task was ever launched.
    assert len(settled['receipts']) == 1
    assert settled['receipts'][0]['stage'] == 'executing'
    assert len(calls) == 1

    from lib.evolution import get_episode
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'partial'
    assert episode['exploratory'] is True
    assert episode['actuals']['tokens'] == 15
    assert settled['learning']['steps']['strategies']['result']['templates'] == 0
    assert episode['verdict']['outcome'] == 'partial'


def test_failed_pipeline_never_replays_itself(runtime, monkeypatch):
    runs, _, research_tasks, calls = runtime
    run = runs.submit_run(
        {'objective': 'speculative decoding for diffusion', 'domain': 'research'},
        user_id=OWNER)
    record = {'task_id': run['task_id'], 'direction': run['objective'],
              'status': 'error', 'error': 'harvest backend unavailable',
              'result': {'accepted': [], 'rejected': []}}
    settled = advance(runtime, research_tasks[0], record, monkeypatch)
    assert settled['review']['outcome'] == 'failure'
    assert settled['status'] in ('needs_attention', 'failed')
    # A failure verdict is terminal for the run: the expensive pipeline is
    # not silently re-minted; resubmission is a new explicit authorization.
    assert len(calls) == 1
    assert len(research_tasks) == 1


def test_missing_record_is_an_honest_outcome_not_a_fabricated_verdict(runtime, monkeypatch):
    runs, _, research_tasks, _ = runtime
    run = runs.submit_run(
        {'objective': 'sparse attention routing', 'domain': 'research'},
        user_id=OWNER)
    settled = advance(runtime, research_tasks[0], None, monkeypatch)
    assert settled['review'] is None
    assert settled['error'] == 'research_record_unavailable_or_not_terminal'
    assert settled['status'] in ('needs_attention', 'failed')


def test_research_request_is_contract_validated(runtime):
    runs, _, _, calls = runtime
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'x', 'domain': 'research',
                         'research': {'n_ideas': 99}}, user_id=OWNER)
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'x', 'domain': 'alchemy'}, user_id=OWNER)
    assert calls == []


def test_caller_cannot_disable_scientific_acceptance_boundary(runtime):
    runs, _, tasks, calls = runtime
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'scientific discovery', 'domain': 'research',
                         'research_review_version': 0}, user_id=OWNER)
    assert not tasks and not calls


def test_new_research_policy_is_frozen_and_read_only_after_settlement(runtime, monkeypatch):
    runs, store, tasks, calls = runtime
    payload = {'objective': 'future memory mechanism', 'domain': 'research'}
    run = runs.submit_run(payload, user_id=OWNER, idempotency_key='frozen-research')
    record = {'task_id': run['task_id'], 'status': 'done',
              'result': {'accepted': [{'title': 'Future mechanism',
                                       'validated': True, 'score': 10}],
                         'evaluation': {'scientific_validity': 'proven'}}}
    settled = advance(runtime, tasks[0], record, monkeypatch)
    writes = store.writes
    assert runs.get_run(run['run_id'], user_id=OWNER) == settled
    assert runs.advance_run(run['run_id'], user_id=OWNER) == settled
    assert runs.submit_run(payload, user_id=OWNER, idempotency_key='frozen-research') == settled
    assert store.writes == writes and len(calls) == 1
    assert settled['status'] == 'needs_attention'
    assert settled['review']['criterion_results'][0]['outcome'] == 'unknown'


def test_legacy_inflight_run_keeps_its_frozen_acceptance_policy(runtime, monkeypatch):
    from lib.evolution import records, get_episode
    runs, store, tasks, calls = runtime
    payload = {'objective': 'frozen historical research', 'domain': 'research'}
    run = runs.submit_run(payload, user_id=OWNER, idempotency_key='legacy-research')
    key = records.entity_key(run['run_id'], OWNER, kind='run')
    saved, version = records.get_versioned_record(records.NAMESPACE_RUNS, key)
    saved.pop('research_review_version')
    records.put_record(records.NAMESPACE_RUNS, key, saved, kind='run', expected_version=version)
    # A legacy snapshot has the legacy goal's learning rights too.
    gkey = records.entity_key(run['goal_id'], OWNER, kind='goal')
    goal, gv = records.get_versioned_record(records.NAMESPACE_GOALS, gkey)
    goal['exploratory'] = False
    records.put_record(records.NAMESPACE_GOALS, gkey, goal, kind='goal', expected_version=gv)
    ekey = records.entity_key(run['episode_id'], OWNER, kind='episode')
    episode, ev = records.get_versioned_record(records.NAMESPACE_EPISODES, ekey)
    episode['exploratory'] = False
    records.put_record(records.NAMESPACE_EPISODES, ekey, episode, kind='episode', expected_version=ev)
    replay = runs.submit_run(payload, user_id=OWNER, idempotency_key='legacy-research')
    assert 'research_review_version' not in replay and len(calls) == 1
    settled = advance(runtime, tasks[0], {'task_id': run['task_id'],
                      'status': 'done', 'result': {'accepted': []}}, monkeypatch)
    assert settled['status'] == 'completed'
    assert settled['review']['outcome'] == 'success'
    assert get_episode(run['episode_id'], user_id=OWNER)['status'] == 'success'


@pytest.fixture
def production(runtime, monkeypatch):
    import threading
    from lib.research.runtime import _research_runtime
    from lib.research import records as research_records
    runs, store, _, _ = runtime
    # Undo the unit fixture's fake research launcher/loader for this boundary.
    from lib.research.engine import produce_research
    from lib.evolution.bridge import default_task_loader
    monkeypatch.setattr('lib.research.api.produce_research', produce_research)
    monkeypatch.setattr('lib.research.api.get_research_task', store.real_research_task_reader)
    monkeypatch.setattr(runs, 'default_task_loader', default_task_loader)
    monkeypatch.setattr(research_records, '_storage', lambda **kw: store)
    before = {t['id'] for t in _research_runtime.snapshot()}
    try:
        yield runs, store, _research_runtime
    finally:
        for task in _research_runtime.snapshot():
            if task['id'] not in before:
                _research_runtime.abort_owned(task['id'], user_id=OWNER)
                for thread in threading.enumerate():
                    if thread.name == f"research-{task['id'][:8]}":
                        thread.join(10)
                        assert not thread.is_alive()
                assert _research_runtime.remove_owned(task['id'], user_id=OWNER)


def test_real_research_registry_is_reconciled_without_injecting_a_terminal_task(production, monkeypatch):
    import threading
    runs, store, runtime = production
    entered, release = threading.Event(), threading.Event()
    def recipe(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return {'accepted': [{'title': 'Untested future mechanism'}],
                'rejected': [], 'survey_md': 'Synthetic proposal fixture',
                'corpus_size': 0, 'gate_reached': 'accepted'}
    monkeypatch.setattr('lib.research.recipe.build_research_from_direction', recipe)
    try:
        run = runs.submit_run({'objective': 'owned registry probe', 'domain': 'research'}, user_id=OWNER)
        assert entered.wait(5)
        assert runs.advance_run(run['run_id'], user_id=OWNER)['status'] == 'executing'
        release.set()
        for thread in threading.enumerate():
            if thread.name == f"research-{run['task_id'][:8]}":
                thread.join(10)
                assert not thread.is_alive()
        settled = runs.advance_run(run['run_id'], user_id=OWNER)
        assert settled['status'] == 'needs_attention'
        assert settled['review']['outcome'] == 'partial'
        assert settled['review']['criterion_results'][0]['outcome'] == 'unknown'
        assert len(settled['receipts']) == 1
        assert settled['learning']['steps']['strategies']['result']['templates'] == 0
    finally:
        release.set()


def test_real_research_stop_reaches_the_owned_production_worker(production, monkeypatch):
    import threading
    runs, store, runtime = production
    entered, release = threading.Event(), threading.Event()
    def recipe(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return {'accepted': [], 'rejected': []}
    monkeypatch.setattr('lib.research.recipe.build_research_from_direction', recipe)
    try:
        run = runs.submit_run({'objective': 'owned stop probe', 'domain': 'research'}, user_id=OWNER)
        assert entered.wait(5)
        task = runtime.get_owned(run['task_id'], user_id=OWNER)
        with pytest.raises(runs.EvolutionStateError):
            runs.cancel_run(run['run_id'], user_id=OWNER + 1)
        from lib.research.api import get_research_task, abort_research_task
        assert get_research_task(run['task_id'], user_id=OWNER + 1) is None
        assert abort_research_task(run['task_id'], user_id=OWNER + 1) is False
        assert not task['abort_event'].is_set()
        cancelled = runs.cancel_run(run['run_id'], user_id=OWNER)
        assert cancelled['status'] == 'cancelled'
        assert task['abort_event'].is_set()
        release.set()
        for thread in threading.enumerate():
            if thread.name == f"research-{run['task_id'][:8]}":
                thread.join(10)
                assert not thread.is_alive()
        from lib.evolution import get_episode
        runs.advance_run(run['run_id'], user_id=OWNER)
        assert task['status'] == 'aborted'
        assert get_episode(run['episode_id'], user_id=OWNER)['status'] == 'aborted'
    finally:
        release.set()


@pytest.mark.parametrize('lost_ack', [False, True])
def test_research_launch_save_failure_distinguishes_orphan_from_committed_task(runtime, monkeypatch, lost_ack):
    from lib.research import api
    runs, store, tasks, calls = runtime
    save = runs._save
    aborted = []
    monkeypatch.setattr(api, 'abort_research_task', lambda task_id, **kwargs: aborted.append(task_id))
    def fail_launch_save(run, version, user_id):
        if run.get('domain') == 'research' and run.get('task_id') == 'research-task-1':
            if lost_ack:
                save(run, version, user_id)
            raise RuntimeError('launch commit acknowledgement failed')
        return save(run, version, user_id)
    monkeypatch.setattr(runs, '_save', fail_launch_save)
    request = {'objective': 'Research launch rollback authority', 'domain': 'research'}
    if lost_ack:
        result = runs.submit_run(request, user_id=OWNER)
        assert result['task_id'] == 'research-task-1'
        assert result['status'] == 'executing'
        assert aborted == []
    else:
        with pytest.raises(RuntimeError, match='launch commit acknowledgement failed'):
            runs.submit_run(request, user_id=OWNER)
        assert aborted == ['research-task-1']
    assert len(calls) == 1


@pytest.mark.parametrize('failure', ['deduped', 'settlement', 'authority_read'])
def test_research_launch_rollback_preserves_authority_and_cancels_fresh_orphans(runtime, monkeypatch, failure):
    from lib.research import api
    runs, _, _, _ = runtime
    save, produce = runs._save, api.produce_research
    aborted = []
    monkeypatch.setattr(api, 'abort_research_task', lambda task_id, **kwargs: aborted.append(task_id))
    if failure == 'deduped':
        def deduped(*args, **kwargs):
            result = produce(*args, **kwargs)
            result['deduped'] = True
            return result
        monkeypatch.setattr(api, 'produce_research', deduped)
    def fail_save(run, version, user_id):
        if run.get('task_id') == 'research-task-1':
            if failure == 'authority_read':
                monkeypatch.setattr(runs, 'get_run', lambda *a, **k:
                    (_ for _ in ()).throw(RuntimeError('authority unavailable')))
            raise RuntimeError('launch failed')
        return save(run, version, user_id)
    monkeypatch.setattr(runs, '_save', fail_save)
    if failure == 'settlement':
        monkeypatch.setattr(runs, 'settle_episode', lambda *a, **k:
            (_ for _ in ()).throw(RuntimeError('settlement unavailable')))
    with pytest.raises(RuntimeError):
        runs.submit_run({'objective': 'Research cleanup failure', 'domain': 'research'}, user_id=OWNER)
    assert aborted == (['research-task-1'] if failure == 'settlement' else [])


@pytest.mark.parametrize('budget', [{'max_tokens': 1000}, {'max_wall_clock_seconds': 60},
                                  {'max_tokens': 1000, 'max_wall_clock_seconds': 60}])
def test_research_goal_forwards_model_budget_and_original_deadline(runtime, budget):
    runs, _, _, calls = runtime
    run = runs.submit_run({'objective': 'Bound research account', 'domain': 'research',
                           'budget': budget}, user_id=OWNER)
    limits = calls[-1]['execution_limits']
    assert limits['max_tokens'] == (budget.get('max_tokens') or None)
    assert limits['max_tool_calls'] is None
    assert limits['deadline'] == (run['created_at'] + 60 if budget.get('max_wall_clock_seconds') else None)


def test_research_goal_refuses_unenforceable_tool_limit_before_launch(runtime):
    runs, _, _, calls = runtime
    with pytest.raises(ValueError, match='tool-call limits'):
        runs.submit_run({'objective': 'Unsupported tool account', 'domain': 'research',
                         'budget': {'max_tool_calls': 1}}, user_id=OWNER)
    assert calls == []


def test_research_goal_absolute_deadline_binds_without_relative_budget(runtime):
    runs, _, _, calls = runtime
    runs.submit_run({'objective': 'Absolute research deadline', 'domain': 'research'},
                    user_id=OWNER, deadline_at=2000000000)
    assert calls[-1]['execution_limits'] == {'max_tokens': None, 'max_tool_calls': None,
                                            'deadline': 2000000000}


def test_real_research_launch_binds_goal_budget_before_model_admission(runtime, tmp_path, monkeypatch):
    from lib.research import api, engine, records, runtime as research_runtime, budget_binding
    from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
    runs, store, _, _ = runtime
    monkeypatch.setattr(api, 'produce_research', engine.produce_research)
    monkeypatch.setattr(api, 'get_research_task', store.real_research_task_reader)
    monkeypatch.setattr(records, '_storage', lambda **kwargs: store)
    monkeypatch.setattr(engine, 'research_root', lambda: str(tmp_path))
    dispatched = []
    def spawn(task_id, worker, task):
        policy = budget_binding.read_model_budget(task_id, user_id=OWNER)
        assert policy['execution_limits']['max_tokens'] == 100
        assert policy['execution_limits']['max_tool_calls'] is None
        admit = engine._prepare_model_budget(task)
        lease = admit(input_tokens=90, max_output_tokens=20)
        assert lease.max_output_tokens == 10
        lease.settle(unsent=True)
        with pytest.raises(ModelBudgetAdmissionDenied):
            admit(input_tokens=100, max_output_tokens=20)
        dispatched.append(task_id)
    monkeypatch.setattr(research_runtime._research_runtime, 'spawn', spawn)
    run = runs.submit_run({'objective': 'Actual research budget admission', 'domain': 'research',
                           'budget': {'max_tokens': 100}}, user_id=OWNER)
    try:
        assert dispatched == [run['task_id']]
        assert run['status'] == 'executing'
    finally:
        research_runtime._research_runtime.remove_owned(run['task_id'], user_id=OWNER)


@pytest.mark.parametrize('reason', ['execution_token_budget_exhausted', 'execution_deadline_exceeded'])
def test_research_budget_stop_is_attention_without_replay(runtime, monkeypatch, reason):
    runs, _, tasks, calls = runtime
    run = runs.submit_run({'objective': 'Explain research authorization stop', 'domain': 'research'}, user_id=OWNER)
    task = tasks[-1]
    task['_executionLimitReason'] = reason
    settled = advance(runtime, task, {'status': 'error', 'error': reason}, monkeypatch)
    assert settled['status'] == 'needs_attention'
    assert settled['error'] == reason
    assert len(calls) == 1
    assert len(settled['receipts']) == 1
