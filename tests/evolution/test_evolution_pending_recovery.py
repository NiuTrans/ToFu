"""Unsubmitted cold restoration: real task storage, controlled domain/provider seams."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, finish, review
from tests.evolution.test_evolution_stage_dispatch import created as created, interrupted_binding

pytestmark = pytest.mark.unit


def retire(runtime, created, monkeypatch, *, budget=None):
    from lib.tasks_pkg.manager import chat_task_runtime, discard_task
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.storage import get_storage_client
    runs = runtime[0]
    run = interrupted_binding(runtime, created, monkeypatch, budget=budget or {'max_tokens': 1000}, max_attempts=1)
    task = created[0]
    task['_executionSession'].settle(ExecutionPhase.CANCELLED, cause='simulated_process_loss')
    discard_task(task['id'], '')
    get_storage_client(write=True).command('task_results.recover_running',
        {'interrupted_reason': 'server_restart', 'max_rows': 500, 'scan_limit': 10000}, None)
    run, version = runs._read(run['run_id'], OWNER)
    # Explicit synthetic dead owner, same boot/namespace. Genuine process
    # lifetimes and persistent domain CAS are covered by the separate proof.
    run['stage_owner']['pid'] = 999999997
    run['stage_owner']['started'] = '1'
    runs._save(run, version, OWNER)
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
                        chat_task_runtime.get_owned(tid, user_id=user_id))
    return run, task


def test_cold_restore_preserves_post_hook_input_attempt_episode_budget_and_review(runtime, created, monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import get_episode
    calls = []
    def hook(prompt, task):
        calls.append(task['id'])
        task['config']['preparedHookEvidence'] = {'source': 'owner inspection'}
        return 'Retained original hook evidence 🧪\n' + prompt
    monkeypatch.setattr(tool_hooks, 'run_user_prompt_hooks', hook)
    original, first = retire(runtime, created, monkeypatch)
    episode = get_episode(original['episode_id'], user_id=OWNER)
    resumed = runtime[0].advance_run(original['run_id'], user_id=OWNER)
    assert len(runtime[2]) == 1
    task = runtime[2][0]
    assert task['id'] != first['id'] and calls == [first['id']]
    assert task['messages'] == first['messages'] and task['config'] == first['config']
    assert resumed['episode_id'] == original['episode_id'] and resumed['attempt'] == 1
    assert get_episode(original['episode_id'], user_id=OWNER) == episode
    voided = resumed['receipts'][0]
    assert voided['submission_voided'] and voided['actuals_complete']
    assert voided['actuals']['tokens'] == voided['actuals']['tool_calls'] == 0
    assert 'terminal_verdict' not in voided and not voided.get('outcome_available')
    runtime[0].advance_run(original['run_id'], user_id=OWNER)
    assert runtime[2] == [task]
    reviewing = finish(runtime, task, 'Owned deliverable with original evidence.')
    assert reviewing['status'] == 'reviewing'
    completed = finish(runtime, runtime[2][-1], review())
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 30
    assert get_episode(original['episode_id'], user_id=OWNER)['actuals']['tokens'] == 30


@pytest.mark.parametrize('change', ['digest', 'foreign', 'binding', 'cancel', 'extra_write', 'reason', 'unproven_replacement', 'deadline', 'unknown_usage', 'missing', 'settled_episode'])
def test_cold_restore_refuses_unproven_or_exhausted_state(runtime, created, monkeypatch, change):
    from lib.evolution import stage_inputs, records
    from lib.storage import get_storage_client
    run, first = retire(runtime, created, monkeypatch)
    runs, store, tasks = runtime
    current, version = runs._read(run['run_id'], OWNER)
    if change in ('digest', 'foreign', 'binding', 'missing'):
        key = (stage_inputs.NAMESPACE, records.entity_key(first['id'], OWNER, kind='prepared stage'))
        if change == 'missing': store.rows.pop(key)
        elif change == 'digest': store.rows[key]['messages'][0]['content'] += 'changed'
        elif change == 'foreign': store.rows[key]['user_id'] = OWNER + 1
        else: store.rows[key]['config']['evolution']['goal_id'] = 'foreign'
    elif change in ('cancel', 'extra_write', 'reason'):
        import lib.storage
        original = lib.storage.get_storage_client
        class Client:
            def query(self, operation, payload, **kwargs):
                value = original().query(operation, payload, **kwargs)
                if payload.get('key') == first['id'] and value:
                    value = deepcopy(value)
                    if operation == 'task_results.replay_get' and change == 'extra_write':
                        value['version'] += 1
                    if operation == 'record.get':
                        if change == 'cancel': value['value']['abort_requested_at'] = 1
                        if change == 'reason': value['value']['interruptedReason'] = 'user_cancel'
                return value
        monkeypatch.setattr(lib.storage, 'get_storage_client', lambda **kwargs: Client())
    elif change == 'unproven_replacement': current['stage_dispatch']['replaces_task_id'] = 'older-carrier'
    elif change == 'deadline': current['absolute_deadline_at'] = 1
    elif change == 'settled_episode':
        from lib.evolution import settle_episode
        settle_episode(current['episode_id'], user_id=OWNER, status='aborted',
                       actuals={'tokens': 0, 'tool_calls': 0, 'wall_clock_seconds': 0})
    else: current['actuals_complete'] = False
    runs._save(current, version, OWNER)
    attention = runs.advance_run(run['run_id'], user_id=OWNER)
    assert attention['status'] == 'needs_attention' and not tasks
    assert len(created) == 1 and attention['attempt'] == 1


def test_restoration_binding_ack_loss_reuses_same_prepared_carrier(runtime, created, monkeypatch):
    runs = runtime[0]
    run, first = retire(runtime, created, monkeypatch)
    save = runs._save
    def loss(value, version, owner):
        result = save(value, version, owner)
        if value.get('stage_dispatch', {}).get('replaces_task_id') and value['stage_dispatch']['state'] == 'bound':
            raise SystemExit('lost ACK after replacement binding')
        return result
    monkeypatch.setattr(runs, '_save', loss)
    with pytest.raises(SystemExit): runs.advance_run(run['run_id'], user_id=OWNER)
    monkeypatch.setattr(runs, '_save', save)
    bound = runs.get_run(run['run_id'], user_id=OWNER)
    assert bound['stage_dispatch']['state'] == 'bound' and not runtime[2]
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['task_id'] == bound['task_id'] and len(runtime[2]) == 1
    assert len(resumed['receipts']) == 1 and resumed['receipts'][0]['task_id'] == first['id']


def test_uncommitted_restoration_retry_reuses_the_unbound_carrier(runtime, created, monkeypatch):
    from lib.tasks_pkg import manager
    from lib.storage.errors import StorageError
    runs = runtime[0]; run, _ = retire(runtime, created, monkeypatch)
    restore = manager.restore_prepared_task; save = runs._save; observed = []
    def record(*args, **kwargs):
        task = restore(*args, **kwargs); observed.append(task); return task
    def refuse(value, version, owner):
        if value.get('stage_dispatch', {}).get('replaces_task_id'):
            raise StorageError('database_unavailable', 'before binding commit')
        return save(value, version, owner)
    monkeypatch.setattr(manager, 'restore_prepared_task', record)
    monkeypatch.setattr(runs, '_save', refuse)
    with pytest.raises(StorageError): runs.advance_run(run['run_id'], user_id=OWNER)
    assert not runtime[2]
    monkeypatch.setattr(runs, '_save', save)
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert len(observed) == 2 and observed[0] is observed[1]
    assert runtime[2] == [observed[0]] and len(resumed['receipts']) == 1


def test_cancel_wins_restoration_binding_and_releases_the_unbound_session(runtime, created, monkeypatch):
    from lib.tasks_pkg import manager
    runs = runtime[0]; run, _ = retire(runtime, created, monkeypatch)
    restore = manager.restore_prepared_task; save = runs._save; observed = []
    def record(*args, **kwargs):
        task = restore(*args, **kwargs); observed.append(task); return task
    def cancel_first(value, version, owner):
        if value.get('stage_dispatch', {}).get('replaces_task_id'):
            runs.cancel_run(value['run_id'], user_id=owner)
        return save(value, version, owner)
    monkeypatch.setattr(manager, 'restore_prepared_task', record)
    monkeypatch.setattr(runs, '_save', cancel_first)
    cancelled = runs.advance_run(run['run_id'], user_id=OWNER)
    assert cancelled['status'] == 'cancelled' and not runtime[2]
    assert len(observed) == 1 and observed[0]['_executionSession'].is_terminal
    assert manager.chat_task_runtime.get(observed[0]['id']) is None


def test_large_hook_input_restores_complete_unicode_without_repeating_hooks(runtime, created, monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs, records
    content = '原始实验 🧪 e\u0301\r\n' * 160000
    hooks = []
    def hook(prompt, task):
        hooks.append(task['id'])
        return content
    monkeypatch.setattr(tool_hooks, 'run_user_prompt_hooks', hook)
    run, first = retire(runtime, created, monkeypatch)
    archived = runtime[1].rows[(stage_inputs.NAMESPACE,
        records.entity_key(first['id'], OWNER, kind='prepared stage'))]
    assert archived['schema_version'] == 2 and archived['byte_count'] > 4_000_000
    assert stage_inputs.read_input(run, user_id=OWNER)['messages'] == first['messages']
    resumed = runtime[0].advance_run(run['run_id'], user_id=OWNER)
    assert resumed['stage_dispatch']['state'] == 'admitted'
    assert len(runtime[2]) == 1 and hooks == [first['id']]
    assert runtime[2][0]['messages'] == first['messages']
    assert resumed['episode_id'] == run['episode_id'] and resumed['attempt'] == 1
    assert stage_inputs.read_input(resumed, user_id=OWNER)['messages'] == first['messages']


@pytest.mark.parametrize('change', ['missing', 'frame', 'whole_digest', 'index', 'owner', 'framing'])
def test_multipart_birth_refuses_corruption_before_submission(runtime, created, monkeypatch, change):
    import base64
    import hashlib
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs, records
    monkeypatch.setattr(tool_hooks, 'run_user_prompt_hooks', lambda *args: '🧪' * 270000)
    run, first = retire(runtime, created, monkeypatch)
    header = runtime[1].rows[(stage_inputs.NAMESPACE,
        records.entity_key(first['id'], OWNER, kind='prepared stage'))]
    key = next(key for key in runtime[1].rows if key[0] == stage_inputs.INPUT_PARTS_NAMESPACE)
    part = runtime[1].rows[key]
    if change == 'missing':
        runtime[1].rows.pop(key)
    elif change in ('frame', 'whole_digest'):
        chunk = base64.b64decode(part['data'])
        changed = b'!' + chunk[1:]
        part['data'] = base64.b64encode(changed).decode('ascii')
        if change == 'whole_digest': part['sha256'] = hashlib.sha256(changed).hexdigest()
    elif change == 'index': part['index'] += 1
    elif change == 'owner': header['user_id'] += 1
    else: header['part_count'] += 1
    with pytest.raises((runtime[0].EvolutionStateError, ValueError)):
        stage_inputs.read_input(run, user_id=OWNER)
    refused = runtime[0].advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and not runtime[2]


@pytest.mark.parametrize('ack_phase', ['frame', 'manifest'])
def test_multipart_birth_ack_loss_never_publishes_partial_input(runtime, created, monkeypatch, ack_phase):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs, records
    from lib.storage.errors import StorageError
    monkeypatch.setattr(tool_hooks, 'run_user_prompt_hooks', lambda *args: '🧪' * 270000)
    put = records.put_record
    failed = []; carriers = []
    def lose(namespace, key, value, **kwargs):
        result = put(namespace, key, value, **kwargs)
        target = stage_inputs.INPUT_PARTS_NAMESPACE if ack_phase == 'frame' else stage_inputs.NAMESPACE
        if namespace == target and not failed:
            failed.append(key)
            carrier = dict(created[0])
            carrier['messages'] = deepcopy(carrier['messages'])
            carrier['config'] = deepcopy(carrier['config'])
            carriers.append(carrier)
            raise StorageError('database_timeout', 'frame acknowledgement lost')
        return result
    monkeypatch.setattr(records, 'put_record', lose)
    with pytest.raises(StorageError):
        runtime[0].submit_run({'objective': 'Archive complete large input before launch.'}, user_id=OWNER)
    assert failed and not runtime[2]
    assert any(ns == stage_inputs.NAMESPACE for ns, key in runtime[1].rows) == (ack_phase == 'manifest')
    run = next(value for (namespace, key), value in runtime[1].rows.items()
               if namespace == 'evolution.runs.v1')
    task = carriers[0]
    # Retrying the identical carrier acknowledges the previously committed frame.
    reference = stage_inputs.freeze_input(run, task, 'executing', user_id=OWNER)
    inspection = deepcopy(run)
    inspection['status'] = 'executing'
    inspection['stage_dispatch'] = {'prepared_input': reference, 'intent_id': run['stage_launch']['intent_id']}
    assert stage_inputs.read_input(inspection, user_id=OWNER)['messages'] == task['messages']
    versions = dict(runtime[1].versions)
    assert stage_inputs.freeze_input(run, task, 'executing', user_id=OWNER) == reference
    assert runtime[1].versions == versions
    task['messages'][0]['content'] += 'changed'
    with pytest.raises(runtime[0].EvolutionStateError):
        stage_inputs.freeze_input(run, task, 'executing', user_id=OWNER)
    assert runtime[1].versions == versions


def test_two_cold_reapers_share_one_carrier_and_one_submission(runtime, created, monkeypatch):
    runs = runtime[0]
    run, _ = retire(runtime, created, monkeypatch)
    from lib.tasks_pkg import manager
    restore = manager.restore_prepared_task
    barrier = Barrier(2)
    def together(*args, **kwargs):
        task = restore(*args, **kwargs)
        barrier.wait(timeout=10)
        return task
    monkeypatch.setattr(manager, 'restore_prepared_task', together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: runs.advance_run(run['run_id'], user_id=OWNER), range(2)))
    assert len(runtime[2]) == 1 and len({item['task_id'] for item in values}) == 1
    assert len(runs.get_run(run['run_id'], user_id=OWNER)['receipts']) == 1


def test_cancellation_is_persisted_before_cold_restore_can_be_admitted(runtime, created, monkeypatch):
    from lib.tasks_pkg.manager.cancellation import cancel_task
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import discard_task
    run = interrupted_binding(runtime, created, monkeypatch)
    first = created[0]
    receipt = cancel_task(first['id'], user_id=OWNER, source='owner.stop')
    assert receipt['signalled']
    assert get_storage_client().query('task_results.abort_requested',
        {'task_id': first['id'], 'user_id': OWNER}) == {'requested': True}
    discard_task(first['id'], '')
    from lib.tasks_pkg.manager import chat_task_runtime
    monkeypatch.setattr(runtime[0], 'default_task_loader', lambda tid, user_id:
                        chat_task_runtime.get_owned(tid, user_id=user_id))
    get_storage_client(write=True).command('task_results.recover_running',
        {'interrupted_reason': 'server_restart', 'max_rows': 500, 'scan_limit': 10000}, None)
    current, version = runtime[0]._read(run['run_id'], OWNER)
    current['stage_owner']['pid'] = 999999997
    current['stage_owner']['started'] = '1'
    runtime[0]._save(current, version, OWNER)
    attention = runtime[0].advance_run(run['run_id'], user_id=OWNER)
    assert attention['status'] == 'needs_attention' and not runtime[2]


def test_cancellation_storage_failure_still_signals_resources_but_never_acknowledges_durability(runtime, created, monkeypatch):
    from lib.tasks_pkg.manager.cancellation import cancel_task
    from lib.tasks_pkg.manager import prepared
    from lib.storage.errors import StorageError
    interrupted_binding(runtime, created, monkeypatch)
    first = created[0]; observed = []
    def fail(*args, **kwargs): raise StorageError('database_unavailable', 'fault')
    monkeypatch.setattr(prepared, 'persist_prepared_cancellation', fail)
    monkeypatch.setattr('lib.tasks_pkg.manager.cancellation.cancel_task_contexts',
                        lambda task: (observed.append(task['id']) or 1, []))
    with pytest.raises(StorageError): cancel_task(first['id'], user_id=OWNER)
    assert first['aborted'] and observed == [first['id']]


def test_restorable_cancellation_flag_exists_at_registry_publication_before_input_archive(runtime, monkeypatch):
    from lib.tasks_pkg.manager import chat_task_runtime
    from lib.evolution import stage_inputs
    seen = []; original = stage_inputs.freeze_input
    def before_archive(run, task, stage, **kwargs):
        assert chat_task_runtime.get(task['id']) is task
        seen.append(task['meta'].get('requireDurableCancellation'))
        return original(run, task, stage, **kwargs)
    monkeypatch.setattr(stage_inputs, 'freeze_input', before_archive)
    runtime[0].submit_run({'objective': 'Retain a cancellation signal during stage admission.'}, user_id=OWNER)
    assert seen == [True]


def test_missing_hook_mutated_config_portability_refuses_submission(runtime, created, monkeypatch):
    from lib.tasks_pkg import tool_hooks
    def secret(prompt, task):
        task['config']['apiKey'] = 'synthetic-do-not-archive'
        return prompt
    monkeypatch.setattr(tool_hooks, 'run_user_prompt_hooks', secret)
    with pytest.raises(runtime[0].EvolutionStateError):
        runtime[0].submit_run({'objective': 'Use logical references only.'}, user_id=OWNER)
    assert not runtime[2]


def test_cold_pending_review_keeps_consumed_executor_and_exact_acceptance(runtime, created, monkeypatch):
    from lib.tasks_pkg.manager import discard_task, chat_task_runtime
    from lib.storage import get_storage_client
    runs = runtime[0]
    first = runs.submit_run({'objective': 'Explain binary search.', 'max_attempts': 1,
                            'budget': {'max_tokens': 1000}}, user_id=OWNER)
    save = runs._save
    def fault(value, version, owner):
        result = save(value, version, owner)
        if value['status'] == 'reviewing' and value.get('stage_dispatch', {}).get('state') == 'bound':
            raise SystemExit('review binding before admission')
        return result
    monkeypatch.setattr(runs, '_save', fault)
    with pytest.raises(SystemExit): finish(runtime, runtime[2][0], 'Each step halves the interval.')
    monkeypatch.setattr(runs, '_save', save)
    pending = created[-1]; messages = deepcopy(pending['messages'])
    discard_task(pending['id'], '')
    get_storage_client(write=True).command('task_results.recover_running',
        {'interrupted_reason': 'server_restart', 'max_rows': 500}, None)
    run, version = runs._read(first['run_id'], OWNER)
    run['stage_owner']['pid'] = 999999997; run['stage_owner']['started'] = '1'
    runs._save(run, version, OWNER)
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id:
                        chat_task_runtime.get_owned(tid, user_id=user_id))
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['attempt'] == 1 and resumed['episode_id'] == first['episode_id']
    assert len(runtime[2]) == 2 and runtime[2][-1]['messages'] == messages
    completed = finish(runtime, runtime[2][-1], review())
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 30


def retire_current_bound(runtime,monkeypatch):
    from lib.tasks_pkg.manager import discard_task,chat_task_runtime
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.storage import get_storage_client
    runs,store,_=runtime
    run=next(value for (namespace,_),value in store.rows.items() if namespace=='evolution.runs.v1')
    task=chat_task_runtime.get(run['task_id'])
    assert run['stage_dispatch']['state']=='bound' and task['status']=='pending'
    task['_executionSession'].settle(ExecutionPhase.CANCELLED,cause='simulated_process_loss')
    discard_task(task['id'],'')
    get_storage_client(write=True).command('task_results.recover_running',
        {'interrupted_reason':'server_restart','max_rows':500,'scan_limit':10000},None)
    run,version=runs._read(run['run_id'],OWNER)
    run['stage_owner']['pid']=999999997;run['stage_owner']['started']='1'
    runs._save(run,version,OWNER)
    return run,task


def test_repeated_proven_pending_restarts_preserve_attempt_and_dispatch_once(runtime,created,monkeypatch):
    from lib.tasks_pkg.manager import chat_task_runtime
    from lib.evolution import get_episode,stage_inputs
    runs=runtime[0];original,first=retire(runtime,created,monkeypatch,
        budget={'max_tokens':1000,'max_wall_clock_seconds':60})
    save=runs._save;retired=[first];references=[original['stage_dispatch']['prepared_input']]
    def crash(value,version,owner):
        result=save(value,version,owner)
        if value.get('stage_dispatch',{}).get('replaces_task_id') and value['stage_dispatch']['state']=='bound':
            raise SystemExit('crash before replacement submission')
        return result
    for _ in range(3):
        monkeypatch.setattr(runs,'_save',crash)
        with pytest.raises(SystemExit):runs.advance_run(original['run_id'],user_id=OWNER)
        monkeypatch.setattr(runs,'_save',save)
        bound=runs.get_run(original['run_id'],user_id=OWNER)
        references.append(bound['stage_dispatch']['prepared_input'])
        assert bound['stage_dispatch']['prepared_input']['task_id']==bound['task_id']
        assert stage_inputs.read_input(bound,user_id=OWNER)['messages']==first['messages']
        assert chat_task_runtime.get(bound['task_id'])['config']==first['config']
        bound,task=retire_current_bound(runtime,monkeypatch);retired.append(task)
        assert not runtime[2]
    resumed=runs.advance_run(original['run_id'],user_id=OWNER)
    assert len(runtime[2])==1 and resumed['attempt']==1
    assert resumed['episode_id']==original['episode_id']
    assert runtime[2][0]['config']['executionLimits']['deadline']==first['config']['executionLimits']['deadline']
    assert runtime[2][0]['config']['executionLimits']['deadline'] is not None
    assert get_episode(resumed['episode_id'],user_id=OWNER)['task_id']==first['id']
    assert len({row['task_id'] for row in references})==4
    assert [row['task_id'] for row in resumed['receipts']]==[task['id'] for task in retired]
    assert all(row['submission_voided'] and row['actuals']['tokens']==row['actuals']['tool_calls']==0 for row in resumed['receipts'])
    runs.advance_run(original['run_id'],user_id=OWNER)
    assert len(runtime[2])==1
    reviewing=finish(runtime,runtime[2][0],'Preserved work after repeated pre-submission restarts.')
    completed=finish(runtime,runtime[2][-1],review())
    assert reviewing['status']=='reviewing' and completed['status']=='completed'
    assert completed['actuals']['tokens']==30


@pytest.mark.parametrize('change',['cancel','version','lineage','digest','admitted'])
def test_second_restoration_requires_fresh_unsubmitted_proof(runtime,created,monkeypatch,change):
    runs=runtime[0];original,_=retire(runtime,created,monkeypatch);save=runs._save
    def crash(value,version,owner):
        result=save(value,version,owner)
        if value.get('stage_dispatch',{}).get('replaces_task_id') and value['stage_dispatch']['state']=='bound':
            raise SystemExit('crash before replacement submission')
        return result
    monkeypatch.setattr(runs,'_save',crash)
    with pytest.raises(SystemExit):runs.advance_run(original['run_id'],user_id=OWNER)
    monkeypatch.setattr(runs,'_save',save)
    current,task=retire_current_bound(runtime,monkeypatch)
    value,version=runs._read(current['run_id'],OWNER)
    if change=='lineage':value['stage_dispatch']['replaces_task_id']='unrelated'
    elif change=='digest':value['stage_dispatch']['prepared_input']['sha256']='0'*64
    elif change=='admitted':value['stage_dispatch']['state']='admitted'
    else:
        from lib.storage import get_storage_client
        original_client=get_storage_client
        class Client:
            def query(self,operation,payload,**kwargs):
                result=original_client().query(operation,payload,**kwargs)
                if result and operation=='task_results.replay_get' and payload.get('key')==task['id'] and change=='version':
                    result={**result,'version':result['version']+1}
                if result and operation=='record.get' and payload.get('key')==task['id'] and change=='cancel':
                    result=deepcopy(result);result['value']['abort_requested_at']=1
                return result
        monkeypatch.setattr('lib.storage.get_storage_client',lambda **_:Client())
    runs._save(value,version,OWNER)
    attention=runs.advance_run(current['run_id'],user_id=OWNER)
    assert attention['status']=='needs_attention' and not runtime[2]


def test_unbound_replacement_rejects_a_conflicting_archived_input(runtime,created,monkeypatch):
    from lib.tasks_pkg import manager
    from lib.evolution import records,stage_inputs
    from lib.storage.errors import StorageError
    runs,store,tasks=runtime;run,_=retire(runtime,created,monkeypatch)
    restore=manager.restore_prepared_task;save=runs._save;observed=[]
    def record(*args,**kwargs):
        task=restore(*args,**kwargs);observed.append(task);return task
    def refuse(value,version,owner):
        if value.get('stage_dispatch',{}).get('replaces_task_id'):
            raise StorageError('database_unavailable','binding unavailable')
        return save(value,version,owner)
    monkeypatch.setattr(manager,'restore_prepared_task',record)
    monkeypatch.setattr(runs,'_save',refuse)
    with pytest.raises(StorageError):runs.advance_run(run['run_id'],user_id=OWNER)
    key=(stage_inputs.NAMESPACE,records.entity_key(observed[0]['id'],OWNER,kind='prepared stage'))
    store.rows[key]['messages'][0]['content']+=' altered'
    monkeypatch.setattr(runs,'_save',save)
    attention=runs.advance_run(run['run_id'],user_id=OWNER)
    assert attention['status']=='needs_attention' and not tasks
    assert 'archive differs' in attention['error']


def test_large_messages_do_not_relax_configuration_or_credentials(runtime, created, monkeypatch):
    from lib.tasks_pkg import tool_hooks
    from lib.evolution import stage_inputs
    def hook(prompt, task):
        task['config']['oversizedHookSetting'] = 'x' * (stage_inputs.MAX_INPUT_BYTES + 1)
        return '🧪' * 270000
    monkeypatch.setattr(tool_hooks, 'run_user_prompt_hooks', hook)
    with pytest.raises(runtime[0].EvolutionStateError):
        runtime[0].submit_run({'objective': 'Preserve configuration admission limits.'}, user_id=OWNER)
    assert not runtime[2]
