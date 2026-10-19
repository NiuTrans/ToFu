"""Actual owned result commits with synthetic execution and isolated storage."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime
from tests.evolution.test_evolution_stage_outcomes import terminal
from tests.evolution.test_evolution_background_execution import attach_background_launch

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit


@pytest.fixture(scope='module')
def _terminal_checkpoint_authority_owner():
    """The isolated native authority owns the same subject as these tasks."""
    import os
    previous = os.environ.get('TOFUDB_OWNER_ID')
    os.environ['TOFUDB_OWNER_ID'] = str(OWNER)
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('TOFUDB_OWNER_ID', None)
        else:
            os.environ['TOFUDB_OWNER_ID'] = previous


@pytest.fixture(scope='module')
def credential_storage(_terminal_checkpoint_authority_owner, credential_storage):
    return credential_storage


def committed(runtime, monkeypatch, *, content='Observed experiment result 🧪'):
    from lib.tasks_pkg.manager import _persist, _terminal
    from lib.agent_core.execution_session import ExecutionPhase
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve exact measured observations.'}, user_id=OWNER)
    task = terminal(tasks[0], content)
    task['messages'].append({'role': 'tool', 'tool_call_id': 'measured-tool',
                            'content': {'baseline': 81, 'candidate': 42, 'text': '原始证据 🧪'}})
    task['_executionSession'].settle(ExecutionPhase.COMPLETED, cause='controlled_test')
    with monkeypatch.context() as patch:
        patch.setattr(_terminal, 'notify_terminal_listeners', lambda _: (_ for _ in ()).throw(RuntimeError('process lost after result commit')))
        assert _persist.persist_task_result(task, _defer_heavy_release=True) is False
    return run, task


def test_result_commit_recovers_exact_review_without_executor_replay(runtime, monkeypatch):
    from lib.evolution import stage_outcomes as outcomes
    from lib.storage import get_storage_client
    runs, _, tasks = runtime
    run, task = committed(runtime, monkeypatch)
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None
    row = get_storage_client().query('task_results.replay_get',
        {'key': task['id'], 'user_id': OWNER, 'include_metadata': True})
    frozen = row['metadata']['terminalContext']['outcome']
    assert row['status'] == 'done' and frozen['tool_evidence']['message_count'] == 1
    task['content'] = 'uncommitted changed output'
    task['messages'][-1]['content'] = 'uncommitted fabricated evidence'
    monkeypatch.setattr(runs, 'default_task_loader', lambda *a, **kw: None)
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['status'] == 'reviewing' and len(tasks) == 2
    assert tasks[1]['messages'][0]['content'] == frozen['review_prompt']
    assert 'baseline' in frozen['review_prompt'] and 'fabricated' not in frozen['review_prompt']
    assert resumed['episode_id'] == run['episode_id'] and resumed['attempt'] == run['attempt']
    assert resumed['actuals']['tokens'] == frozen['actuals']['tokens'] == 15
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) == frozen
    runs.advance_run(run['run_id'], user_id=OWNER)
    assert len(tasks) == 2


def test_preparation_failure_retains_debt_and_cannot_commit_terminal_result(runtime, monkeypatch):
    from lib.tasks_pkg.manager import _persist
    from lib.evolution import records, stage_outcomes as outcomes
    from lib.storage import get_storage_client
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Do not lose terminal tool evidence.'}, user_id=OWNER)
    task = terminal(tasks[0])
    original = records.put_record
    with monkeypatch.context() as patch:
        def fail(namespace, *args, **kwargs):
            if namespace == outcomes.PART_NAMESPACE:
                raise RuntimeError('source write failed')
            return original(namespace, *args, **kwargs)
        patch.setattr(records, 'put_record', fail)
        assert _persist.persist_task_result(task) is False
    row = get_storage_client().query('task_results.replay_get', {'key': task['id'], 'user_id': OWNER})
    assert row['status'] == 'pending' and task['content'] == 'Observed result'
    assert task['_terminalPersistencePending'] and task['_terminalPersistenceRetryReady']
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None
    assert _persist.persist_task_result(task, _defer_heavy_release=True) is True
    assert runs.get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'


def test_stale_terminal_writer_leaves_only_non_authoritative_parts(runtime, monkeypatch):
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import _persist
    from lib.evolution import stage_outcomes as outcomes
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Reject old execution after startup retirement.'}, user_id=OWNER)
    task = tasks[0]
    from lib.tasks_pkg.manager import recover_stale_tasks_on_startup
    recover_stale_tasks_on_startup({'verdict': 'unclean'})
    terminal(task)
    assert _persist.persist_task_result(task, _defer_heavy_release=True) is False
    assert task['_workspaceDeliveryOwnershipRejected'] is True
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None
    assert outcomes.recover(run, user_id=OWNER) == (None, None)
    assert runs.get_run(run['run_id'], user_id=OWNER)['receipts'] == [] and len(tasks) == 1
    with pytest.raises(runs.EvolutionStateError, match='ownership'):
        outcomes.capture(run, task, user_id=OWNER, review_prompt_factory=lambda _: 'untrusted')


@pytest.mark.parametrize('fault', ['owner', 'task', 'dispatch', 'epoch', 'status', 'version', 'finish', 'digest', 'missing_part'])
def test_commit_identity_and_complete_sources_fence_materialization(runtime, monkeypatch, fault):
    from lib.evolution import terminal_checkpoint, stage_outcomes as outcomes
    from lib.storage import get_storage_client
    runs, store, tasks = runtime
    run, task = committed(runtime, monkeypatch)
    row = get_storage_client().query('task_results.replay_get',
        {'key': task['id'], 'user_id': OWNER, 'include_metadata': True})
    changed = deepcopy(row)
    capsule = changed['metadata']['terminalContext']
    if fault == 'owner': changed['user_id'] = OWNER + 1
    elif fault == 'task': changed['task_id'] = 'foreign-task'
    elif fault == 'dispatch': capsule['dispatch']['state'] = 'bound'
    elif fault == 'epoch': changed['metadata']['contentEpoch'] += 1
    elif fault == 'status': changed['status'] = 'interrupted'
    elif fault == 'version': changed['version'] = capsule['dispatch']['birth_version']
    elif fault == 'finish': changed['metadata']['finishReason'] = 'length'
    elif fault == 'digest': capsule['outcome']['content_sha256'] = '0' * 64
    elif fault == 'missing_part':
        del store.rows[(outcomes.PART_NAMESPACE, outcomes._part_key(task['id'], 0, OWNER))]
    actual = get_storage_client()
    class Client:
        def query(self, operation, payload, **kw):
            if operation == 'task_results.replay_get': return changed
            return actual.query(operation, payload, **kw)
    monkeypatch.setattr('lib.storage.get_storage_client', lambda **kw: Client())
    with pytest.raises(runs.EvolutionStateError):
        terminal_checkpoint.materialize_committed(run, user_id=OWNER)
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None
    assert len(tasks) == 1


def test_concurrent_materializers_share_one_frozen_outcome(runtime, monkeypatch):
    from lib.evolution.terminal_checkpoint import materialize_committed
    run, _ = committed(runtime, monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: materialize_committed(run, user_id=OWNER), range(8)))
    assert all(value == values[0] for value in values)
    assert len(runtime[2]) == 1


def test_cancelled_run_accounts_committed_consumption_without_review(runtime, monkeypatch):
    runs, _, tasks = runtime
    run, _ = committed(runtime, monkeypatch)
    runs.cancel_run(run['run_id'], user_id=OWNER)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *a, **kw: None)
    current = runs.advance_run(run['run_id'], user_id=OWNER)
    assert current['status'] == 'cancelled' and current['actuals']['tokens'] == 15
    assert len(tasks) == 1


def test_large_tool_originals_do_not_duplicate_in_checkpoint_metadata(runtime, monkeypatch):
    from lib.agent_core.terminal_delivery import prepare_terminal_persistence
    from lib.evolution import stage_outcomes as outcomes
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Retain large experimental originals.'}, user_id=OWNER)
    task = terminal(tasks[0], 'result' * 12000)
    task['messages'].extend({'role': 'tool', 'tool_call_id': f'tool-{i}',
                             'content': 'measurement 🧪' * 9000} for i in range(16))
    capsule = prepare_terminal_persistence(task, content=task['content'], content_epoch=0)
    assert len(json.dumps(capsule, ensure_ascii=False).encode()) < 60000
    assert capsule['outcome']['tool_evidence']['content_chars'] > 1000000
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None


def test_sweep_waits_for_managed_terminal_owned_commit(runtime):
    from lib.evolution import stage_outcomes as outcomes
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Respect terminal persistence ordering.'}, user_id=OWNER)
    terminal(tasks[0])
    assert runs.advance_run(run['run_id'], user_id=OWNER)['status'] == 'executing'
    assert outcomes.read_metadata(run['run_id'], tasks[0]['id'], user_id=OWNER) is None
    assert len(tasks) == 1


def test_missing_committed_parts_keep_known_consumption_after_owner_loss(runtime, monkeypatch):
    from lib.evolution import stage_outcomes as outcomes
    runs, store, tasks = runtime
    run, task = committed(runtime, monkeypatch)
    del store.rows[(outcomes.PART_NAMESPACE, outcomes._part_key(task['id'], 0, OWNER))]
    monkeypatch.setattr(runs, 'default_task_loader', lambda *a, **kw: None)
    monkeypatch.setattr('lib.evolution.review_recovery.owner_state', lambda *a: 'dead')
    current = runs.advance_run(run['run_id'], user_id=OWNER)
    assert current['status'] == 'needs_attention' and current['actuals']['tokens'] == 15
    assert 'archive is incomplete' in current['error'] and len(tasks) == 1


def test_unknown_usage_stays_frozen_despite_late_usage_and_cannot_launch_review(runtime, monkeypatch):
    from lib.tasks_pkg.manager import _persist, _terminal
    from lib.evolution import stage_outcomes as outcomes
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve unknown usage.', 'budget': {'max_tokens': 1000}}, user_id=OWNER)
    task = terminal(tasks[0])
    task['_executionActualsComplete'] = False
    with monkeypatch.context() as patch:
        patch.setattr(_terminal, 'notify_terminal_listeners', lambda _: (_ for _ in ()).throw(RuntimeError('lost ACK')))
        assert _persist.persist_task_result(task, _defer_heavy_release=True) is False
    task['_executionActualsComplete'] = True
    task['usage'] = {'input_tokens': 900, 'output_tokens': 100}
    monkeypatch.setattr(runs, 'default_task_loader', lambda *a, **kw: None)
    current = runs.advance_run(run['run_id'], user_id=OWNER)
    saved = outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER)
    assert saved['actuals_complete'] is False and saved['actuals']['tokens'] == 15
    assert current['status'] == 'needs_attention' and current['actuals_complete'] is False
    assert len(tasks) == 1


def test_required_observer_cannot_bypass_terminal_preparation(runtime):
    from lib.agent_core.terminal_delivery import deliver_terminal
    from lib.evolution import stage_outcomes as outcomes
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Reject a premature terminal observer.'}, user_id=OWNER)
    terminal(tasks[0])
    with pytest.raises(runs.EvolutionStateError, match='preparation'):
        deliver_terminal(tasks[0])
    assert outcomes.read_metadata(run['run_id'], tasks[0]['id'], user_id=OWNER) is None
    assert len(tasks) == 1


@pytest.mark.parametrize('observed',[False,True])
def test_actual_result_commit_preserves_required_command_policy(runtime,monkeypatch,observed):
    from lib.tasks_pkg.manager import _persist,_terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.storage import get_storage_client
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Require actual command completion','require_command_execution':True,
        'max_attempts':1},user_id=OWNER)
    task=terminal(tasks[0])
    if observed:task['toolRounds']=[{'toolName':'run_command','toolCallId':'executed','status':'done','results':[{'exitCode':'0'}]}]
    task['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_test')
    monkeypatch.setattr(_terminal,'notify_terminal_listeners',lambda _:(_ for _ in ()).throw(RuntimeError('lost callback')))
    assert _persist.persist_task_result(task,_defer_heavy_release=True) is False
    row=get_storage_client().query('task_results.replay_get',{'key':task['id'],'user_id':OWNER,'include_metadata':True})
    assert row['metadata']['terminalContext']['outcome']['command_execution_observed'] is observed
    monkeypatch.setattr(runs,'default_task_loader',lambda *a,**k:None)
    resumed=runs.advance_run(run['run_id'],user_id=OWNER)
    assert resumed['status']==('reviewing' if observed else 'failed')
    assert len(tasks)==(2 if observed else 1)


from tests.evolution.test_evolution_background_execution import command as command


def test_background_original_is_paged_after_owned_commit_process_loss_and_registry_eviction(runtime,monkeypatch,command):
    from lib.tasks_pkg.manager import _persist,_terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.evolution import stage_outcomes as outcomes
    from lib.tasks_pkg import background_commands as commands
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Review the actual background measurements','require_command_execution':True},user_id=OWNER)
    task=terminal(tasks[0])
    source,handle=command
    attach_background_launch(command,task)
    raw='$ experiment\n'+('paired-measurement 🧪\n'*2200)+'central-metric=0.8\n[exit code: 0]'
    commands.finish_handed_off_command(command_id=handle,result=raw,error=None)
    task['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_test')
    monkeypatch.setattr(_terminal,'notify_terminal_listeners',lambda _:(_ for _ in ()).throw(RuntimeError('lost callback')))
    assert _persist.persist_task_result(task,_defer_heavy_release=True) is False
    with commands._lock:commands._registry.pop(handle,None)
    monkeypatch.setattr(runs,'default_task_loader',lambda *a,**k:None)
    resumed=runs.advance_run(run['run_id'],user_id=OWNER)
    assert resumed['status']=='reviewing' and len(tasks)==2
    assert 'tool_evidence' in tasks[1]['messages'][0]['content']
    parts=[];offset=0
    while True:
        page=outcomes.page(run['run_id'],task['id'],user_id=OWNER,offset=offset,source_kind='tool_evidence')
        assert page['tool_evidence_scope']=='terminal_retained_tool_messages_and_background_completions'
        parts.append(page['content'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    entry=json.loads(''.join(parts))
    assert entry['content']==raw and entry['command_id']==handle
    assert entry['source_tool_call_id']=='launch'
    assert entry['source']=='owned_background_command_completion'
    assert len(parts)>1


def test_background_snapshot_stays_exact_across_preparation_failure_and_retry(runtime,monkeypatch,command):
    from lib.tasks_pkg.manager import _persist,_terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.evolution import records,stage_outcomes as outcomes
    from lib.tasks_pkg import background_commands as commands
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Keep the first background snapshot','require_command_execution':True},user_id=OWNER)
    task=terminal(tasks[0]);source,handle=command
    attach_background_launch(command,task)
    raw='$ experiment\noriginal paired data\n[exit code: 0]'
    commands.finish_handed_off_command(command_id=handle,result=raw,error=None)
    task['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_test')
    monkeypatch.setattr(_terminal,'notify_terminal_listeners',lambda _:None)
    put=records.put_record
    with monkeypatch.context() as patch:
        def fail(namespace,*args,**kwargs):
            if namespace==outcomes.PART_NAMESPACE:raise RuntimeError('source part acknowledgement unavailable')
            return put(namespace,*args,**kwargs)
        patch.setattr(records,'put_record',fail)
        assert _persist.persist_task_result(task,_defer_heavy_release=True) is False
    with commands._lock:commands._registry.pop(handle,None)
    assert _persist.persist_task_result(task,_defer_heavy_release=True) is True
    from lib.evolution.terminal_checkpoint import materialize_committed
    sealed=materialize_committed(run,user_id=OWNER)
    assert sealed['command_execution_observed'] is True
    page=outcomes.page(run['run_id'],task['id'],user_id=OWNER,source_kind='tool_evidence')
    assert json.loads(page['content'])['content']==raw


def test_acknowledged_background_parts_release_raw_snapshot_before_late_result_retry(runtime,monkeypatch,command):
    import gc
    import weakref
    from lib.tasks_pkg.manager import _persist,_terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from lib.evolution import stage_outcomes as outcomes
    from lib.tasks_pkg import background_commands as commands
    runs,_,tasks=runtime
    run=runs.submit_run({'objective':'Release acknowledged background bytes','require_command_execution':True},user_id=OWNER)
    task=terminal(tasks[0]);source,handle=command;attach_background_launch(command,task)
    class SnapshotText(str):pass
    raw=SnapshotText('$ experiment\n'+'measured data '*4000+'\n[exit code: 0]')
    reference=weakref.ref(raw)
    commands.finish_handed_off_command(command_id=handle,result=raw,error=None)
    task['_executionSession'].settle(ExecutionPhase.COMPLETED,cause='controlled_test')
    monkeypatch.setattr(_terminal,'notify_terminal_listeners',lambda _:None)
    assert _persist.persist_task_result(task,_defer_heavy_release=True) is True
    with commands._lock:commands._registry.pop(handle,None)
    del raw;gc.collect()
    assert reference() is None
    # A later persistence preparation re-verifies acknowledged archive parts.
    from lib.evolution.terminal_checkpoint import materialize_committed
    assert materialize_committed(run,user_id=OWNER)['command_execution_observed'] is True
    assert outcomes.page(run['run_id'],task['id'],user_id=OWNER,source_kind='tool_evidence')['content']


@pytest.mark.parametrize('credential_storage', ['sqlite', 'tofudb'], indirect=True)
def test_large_terminal_context_commits_and_recovers_exact_frozen_prompt(credential_storage, runtime, monkeypatch):
    from lib.evolution import stage_outcomes as outcomes
    from lib.storage import get_storage_client
    import time
    import tracemalloc
    runs, _, tasks = runtime
    frozen_prompt = '完整冻结评审输入 🧪' * 60000
    monkeypatch.setattr(runs, '_review_prompt', lambda *args, **kwargs: frozen_prompt)
    tracemalloc.start(); started = time.perf_counter()
    run, task = committed(runtime, monkeypatch)
    row = get_storage_client().query('task_results.replay_get',
        {'key': task['id'], 'user_id': OWNER, 'include_metadata': True})
    capsule = row['metadata']['terminalContext']
    size = len(json.dumps(capsule, ensure_ascii=False).encode())
    assert size > 1 << 20 and len(frozen_prompt) > 200000
    assert row['status'] == 'done' and capsule['outcome']['review_prompt'] == frozen_prompt
    assert outcomes.read_metadata(run['run_id'], task['id'], user_id=OWNER) is None
    task['content'] = 'Uncommitted changed output'
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    monkeypatch.setattr(runs, '_review_prompt', lambda *args, **kwargs: pytest.fail('must restore frozen input'))
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    elapsed = time.perf_counter()-started; _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    assert resumed['status'] == 'reviewing' and len(tasks) == 2
    assert tasks[1]['messages'][0]['content'] == frozen_prompt
    assert resumed['attempt'] == 1 and resumed['episode_id'] == run['episode_id']
    runs.advance_run(run['run_id'], user_id=OWNER)
    assert len(tasks) == 2
    print(f'terminal_context_utf8_bytes={size} commit_restore_seconds={elapsed:.4f} traced_peak_bytes={peak}')
