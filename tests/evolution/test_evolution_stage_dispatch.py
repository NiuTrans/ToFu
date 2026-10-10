"""Committed binding is distinct from dispatch; resume the original live carrier only."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from lib.storage.errors import StorageError
from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, finish, review

pytestmark = pytest.mark.unit


@pytest.fixture
def created(runtime, monkeypatch):
    from lib.tasks_pkg import manager
    original = manager.create_task
    admitted = []; identities = {}
    def record(*args, **kwargs):
        task = original(*args, **kwargs)
        admitted.append(task); identities[id(task)] = task['id']; return task
    monkeypatch.setattr(manager, 'create_task', record)
    monkeypatch.setattr(runtime[0], 'default_task_loader', lambda tid, user_id: next(
        (task for task in admitted if task['id'] == tid and task['_userId'] == user_id), None))
    yield admitted
    from lib.agent_core.execution_session import ExecutionPhase
    for task in admitted:
        if task not in runtime[2]:
            task['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='test_cleanup')
            manager.discard_task(identities[id(task)], '')


def interrupted_binding(runtime, created, monkeypatch, **options):
    runs = runtime[0]; save = runs._save; stopped = False
    def loss(value, version, owner):
        nonlocal stopped
        result = save(value, version, owner)
        if not stopped and value['status'] == 'executing':
            stopped = True; raise SystemExit('fault after owned task binding before dispatch')
        return result
    monkeypatch.setattr(runs, '_save', loss)
    with pytest.raises(SystemExit):
        runs.submit_run({'objective':'Complete the original long experiment.', **options},
                        user_id=OWNER, idempotency_key='bound-fault')
    monkeypatch.setattr(runs, '_save', save)
    assert len(created) == 1 and not runtime[2]
    run = next(value for (namespace, _), value in runtime[1].rows.items() if namespace == 'evolution.runs.v1')
    return deepcopy(run)


def test_live_bound_task_resumes_once_without_new_task_episode_attempt_or_prompt_hooks(runtime, created, monkeypatch):
    from lib.evolution import get_episode
    from lib.tasks_pkg import tool_hooks
    hooked=[];original=tool_hooks.run_user_prompt_hooks
    def hook(prompt, task):
        hooked.append(task['id']);return 'Owned hook evidence 🧪\n'+original(prompt, task)
    monkeypatch.setattr(tool_hooks,'run_user_prompt_hooks',hook)
    run=interrupted_binding(runtime,created,monkeypatch,budget={'max_tokens':1000},max_attempts=1)
    assert run['stage_dispatch']['state']=='bound'
    first=created[0];episode=get_episode(run['episode_id'],user_id=OWNER)
    messages=deepcopy(first['messages'])
    assert messages[0]['content'].startswith('Owned hook evidence 🧪\n')
    resumed=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert resumed['stage_dispatch']['state']=='admitted' and runtime[2]==[first]
    assert len(created)==1 and hooked==[first['id']]
    assert first['messages']==messages
    assert resumed['episode_id']==run['episode_id'] and resumed['attempt']==1
    assert get_episode(run['episode_id'],user_id=OWNER)==episode
    assert resumed.get('actuals_complete') is not False
    runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert runtime[2]==[first]
    reviewing=finish(runtime,first,'Observed preserved evidence.')
    assert reviewing['stage_dispatch']['task_id']==runtime[2][-1]['id']
    assert reviewing['stage_dispatch']['state']=='admitted'
    completed=finish(runtime,runtime[2][-1],review())
    assert completed['status']=='completed' and completed['actuals']['tokens']==30


def test_cancel_between_binding_and_dispatch_never_submits_the_bound_task(runtime, created, monkeypatch):
    run=interrupted_binding(runtime,created,monkeypatch)
    cancelled=runtime[0].cancel_run(run['run_id'],user_id=OWNER)
    assert cancelled['status']=='cancelled'
    runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert not runtime[2]


@pytest.mark.parametrize('change',['foreign_owner','foreign_task','running','wrong_intent','wrong_stage','birth_changed'])
def test_resume_refuses_changed_live_carrier_or_dispatch_evidence(runtime,created,monkeypatch,change):
    run=interrupted_binding(runtime,created,monkeypatch)
    task=created[0]
    if change=='foreign_owner':task['_userId']=OWNER+1
    elif change=='foreign_task':task['id']='foreign-task'
    elif change=='running':task['status']='running'
    elif change=='wrong_stage':task['config']['evolution']['stage']='reviewing'
    elif change=='wrong_intent':
        value,version=runtime[0]._read(run['run_id'],OWNER)
        value['stage_dispatch']['intent_id']='wrong-intent';runtime[0]._save(value,version,OWNER)
    elif change=='birth_changed':
        import lib.storage
        original=lib.storage.get_storage_client
        class Client:
            def query(self,operation,payload,**kwargs):
                value=original().query(operation,payload,**kwargs)
                if operation=='task_results.replay_get' and value:
                    value={**value,'version':value['version']+1}
                return value
        monkeypatch.setattr(lib.storage,'get_storage_client',lambda **kwargs:Client())
    try:runtime[0].advance_run(run['run_id'],user_id=OWNER)
    except runtime[0].EvolutionStateError:pass
    assert not runtime[2]


@pytest.mark.parametrize('state',['dead','unknown'])
def test_missing_original_carrier_never_recreates_executor_or_replays_prompt_hooks(runtime,created,monkeypatch,state):
    from lib.evolution import review_recovery
    run=interrupted_binding(runtime,created,monkeypatch)
    value,version=runtime[0]._read(run['run_id'],OWNER)
    value['stage_dispatch'].pop('prepared_input',None)
    runtime[0]._save(value,version,OWNER)
    monkeypatch.setattr(runtime[0],'default_task_loader',lambda *args,**kwargs:None)
    monkeypatch.setattr(review_recovery,'owner_state',lambda owner:state)
    attention=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert not runtime[2] and len(created)==1
    assert attention['status']=='needs_attention'


def test_two_reapers_only_one_wins_dispatch_admission_for_the_same_live_task(runtime,created,monkeypatch):
    run=interrupted_binding(runtime,created,monkeypatch)
    read=runtime[0]._read;barrier=Barrier(2)
    def synced(rid,owner):
        result=read(rid,owner)
        if (result[0].get('stage_dispatch') or {}).get('state')=='bound':barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(runtime[0],'_read',synced)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _:runtime[0].advance_run(run['run_id'],user_id=OWNER),range(2)))
    assert runtime[2]==[created[0]] and len(created)==1


def test_cancel_wins_between_validation_and_dispatch_cas(runtime,created,monkeypatch):
    run=interrupted_binding(runtime,created,monkeypatch)
    save=runtime[0]._save
    def race(value,version,owner):
        if (value.get('stage_dispatch') or {}).get('state')=='admitted':
            runtime[0].cancel_run(run['run_id'],user_id=OWNER)
        return save(value,version,owner)
    monkeypatch.setattr(runtime[0],'_save',race)
    result=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert result['status']=='cancelled' and not runtime[2] and len(created)==1


@pytest.mark.parametrize('committed',[False,True])
def test_dispatch_storage_failure_distinguishes_uncommitted_retry_from_lost_ack(runtime,created,monkeypatch,committed):
    run=interrupted_binding(runtime,created,monkeypatch)
    save=runtime[0]._save
    def loss(value,version,owner):
        if (value.get('stage_dispatch') or {}).get('state')=='admitted':
            if committed:save(value,version,owner)
            raise StorageError('unavailable','controlled dispatch acknowledgement loss')
        return save(value,version,owner)
    monkeypatch.setattr(runtime[0],'_save',loss)
    with pytest.raises(StorageError):runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert not runtime[2] and created[0]['status']=='pending'
    monkeypatch.setattr(runtime[0],'_save',save)
    result=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert result['stage_dispatch']['state']=='admitted'
    assert runtime[2]==([] if committed else [created[0]]) and len(created)==1


@pytest.mark.parametrize('reason',['deadline','unknown_usage','tokens'])
def test_budget_changed_after_binding_refuses_submission(runtime,created,monkeypatch,reason):
    run=interrupted_binding(runtime,created,monkeypatch,budget={'max_tokens':1000})
    current,version=runtime[0]._read(run['run_id'],OWNER)
    if reason=='deadline':current['absolute_deadline_at']=1
    elif reason=='unknown_usage':current['actuals_complete']=False
    else:current['actuals']['tokens']=1000
    runtime[0]._save(current,version,OWNER)
    result=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert result['stage_dispatch']['state']=='refused'
    assert not runtime[2] and len(created)==1
    assert created[0]['status'] in ('error','aborted')
    runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert not runtime[2]


def test_legacy_pending_row_without_marker_never_authorizes_resubmission(runtime,created,monkeypatch):
    run=interrupted_binding(runtime,created,monkeypatch)
    current,version=runtime[0]._read(run['run_id'],OWNER)
    current.pop('stage_dispatch');runtime[0]._save(current,version,OWNER)
    result=runtime[0].advance_run(run['run_id'],user_id=OWNER)
    assert result['status']=='executing' and not runtime[2] and len(created)==1


@pytest.mark.parametrize('preparation,checkpoint,limit,refused', [
    (1.25, 0.25, None, False), (6.0, 0.25, None, False),
    (0.0, 5.01, None, True), (0.0, 1.25, 3, False), (4.0, 0.1, 3, True),
])
def test_birth_and_confirmation_share_storage_budget_without_charging_local_preparation(
        runtime, created, monkeypatch, preparation, checkpoint, limit, refused):
    import time
    from lib.tasks_pkg import manager
    from lib.tasks_pkg.manager import _registry
    from lib.evolution import stage_dispatch
    original_create = manager.create_task; original_confirm = stage_dispatch.confirm_birth
    original_write = _registry._upsert_task_row
    monotonic = time.monotonic; wall = time.time; offset = [0.0]
    budgets = []; confirmations = []
    monkeypatch.setattr(time, 'monotonic', lambda: monotonic() + offset[0])
    monkeypatch.setattr(time, 'time', lambda: wall() + offset[0])
    def write(*args, **kwargs):
        result = original_write(*args, **kwargs)
        offset[0] += checkpoint
        return result
    def create(*args, **kwargs):
        budgets.append(kwargs['birth_checkpoint_budget_seconds'])
        offset[0] += preparation
        return original_create(*args, **kwargs)
    def confirm(*args, **kwargs):
        if kwargs.get('value') is None:
            confirmations.append(kwargs['deadline_seconds'])
        return original_confirm(*args, **kwargs)
    monkeypatch.setattr(_registry, '_upsert_task_row', write)
    monkeypatch.setattr(manager, 'create_task', create)
    monkeypatch.setattr(stage_dispatch, 'confirm_birth', confirm)
    options = {'budget': {'max_wall_clock_seconds': limit}} if limit else {}
    if refused:
        with pytest.raises(runtime[0].EvolutionStateError, match='admission budget exhausted'):
            runtime[0].submit_run({'objective': 'Check durable admission.', **options}, user_id=OWNER)
        assert not confirmations and not runtime[2]
    else:
        run = runtime[0].submit_run({'objective': 'Check durable admission.', **options}, user_id=OWNER)
        assert run['status'] == 'executing' and len(runtime[2]) == 1
        assert 0 < confirmations[0] <= budgets[0] - checkpoint
        assert budgets[0] <= (limit or 5)
    assert len(created) == 1 and len(budgets) == 1


@pytest.mark.parametrize('timing', [None, False, -1, float('nan'), float('inf')])
def test_unavailable_or_invalid_birth_checkpoint_timing_refuses_dispatch(runtime, created, monkeypatch, timing):
    from lib.tasks_pkg import manager
    original = manager.create_task
    def create(*args, **kwargs):
        task = original(*args, **kwargs)
        task['_birthCheckpointElapsedSeconds'] = timing
        return task
    monkeypatch.setattr(manager, 'create_task', create)
    with pytest.raises(runtime[0].EvolutionStateError, match='checkpoint timing unavailable'):
        runtime[0].submit_run({'objective':'Confirm storage admission before execution.'}, user_id=OWNER)
    assert len(created) == 1 and not runtime[2]
    assert created[0]['status'] in ('error', 'aborted')
