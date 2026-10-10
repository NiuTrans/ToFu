"""Stopped-process discovery resumes one owner and preserves paid consumption."""
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
import pytest
from lib.research import budget_binding as binding, discovery_owner as owners, engine
from lib.research import model_attempt_journal as journal, records, runtime
from lib.agent_core.execution_budget import model_budget_admission
from lib.storage.errors import StorageError
from tests.research.test_research_budget_binding import bound_source as bound_source, LIMITS
from tests.research.test_research_record_ack import research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark = pytest.mark.unit


@pytest.fixture
def stopped_account(bound_source, monkeypatch, request):
    task, client = bound_source
    binding.bind_model_budget(task['task_id'], user_id=12, execution_limits=LIMITS)
    import sys
    process = subprocess.Popen([sys.executable, '-c',
        'import json,time; from lib.process_owner import process_owner; print(json.dumps(process_owner()),flush=True); time.sleep(60)'],
        stdout=subprocess.PIPE, text=True)
    actual_owner = owners.process_owner
    try:
        old_identity = json.loads(process.stdout.readline())
        assert old_identity is not None
        monkeypatch.setattr(owners, 'process_owner', lambda: old_identity)
        claim = owners.claim_discovery_execution(task['task_id'], user_id=12)
        carrier = {'id':task['task_id'], '_userId':12, 'config':{'executionLimits':LIMITS}}
        journal.install_model_attempt_journal(carrier, execution_claim=claim)
        lease = model_budget_admission(carrier)(input_tokens=10,max_output_tokens=20)
        lease.mark_dispatched()
        lease.settle({'prompt_tokens':8,'completion_tokens':3} if getattr(request,'param',True) else None)
        process.terminate(); process.wait(timeout=10)
        monkeypatch.setattr(owners, 'process_owner', actual_owner)
        assert owners.owner_state(old_identity) == 'dead'
        yield task, client, claim
    finally:
        if process.poll() is None:
            process.kill(); process.wait(timeout=10)
        process.stdout.close()


def test_recovered_worker_account_preserves_history_and_ordinal(stopped_account):
    task, _, old = stopped_account
    claim = owners.recover_discovery_execution(task['task_id'], user_id=12)
    with pytest.raises(StorageError, match='no longer owns'):
        old.assert_current()
    task.update(_research_record_enabled=True, _research_recovered=True,
                _research_recovery_claim=claim)
    admit = engine._prepare_model_budget(task)
    assert task['_research_model_account']['_executionBudget'].consumption_snapshot() == (11,0,True)
    lease = admit(input_tokens=10,max_output_tokens=20)
    assert lease.ordinal == 2
    lease.mark_dispatched(); lease.settle({'prompt_tokens':5,'completion_tokens':2})
    summary = journal.summarize_model_attempts(task['task_id'],user_id=12)
    assert summary['reported_tokens'] == 18 and summary['recorded_usage_complete']
    assert journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)['reported_tokens'] == 11
    from lib.evolution import records as record_store
    key = owners._load(task['task_id'],12)[3] + '-' + old.value['nonce']
    assert record_store.get_record(owners.HISTORY_NAMESPACE,key) == old.value


def test_concurrent_recovery_has_one_winner(stopped_account):
    task, _, _ = stopped_account
    def recover(_):
        try:
            return owners.recover_discovery_execution(task['task_id'], user_id=12)
        except StorageError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(recover, range(2)))
    winners = [value for value in results if value is not None]
    assert len(winners) == 1
    winners[0].assert_current()


def test_false_transition_ack_cannot_grant_recovery(stopped_account, monkeypatch):
    task, client, old = stopped_account
    command = client.command
    def refuse(name,args,command_id):
        if args.get('namespace') == owners.NAMESPACE:
            return {'version':99}
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',refuse)
    with pytest.raises(StorageError,match='exactly acknowledged'):
        owners.recover_discovery_execution(task['task_id'],user_id=12)
    assert owners._load(task['task_id'],12)[-1] == old.value


def test_lost_transition_ack_confirms_only_own_nonce(stopped_account,monkeypatch):
    task, client, _ = stopped_account
    command = client.command
    def lost(name,args,command_id):
        result = command(name,args,command_id)
        if args.get('namespace') == owners.NAMESPACE:
            raise StorageError('database_unavailable','synthetic lost ack')
        return result
    monkeypatch.setattr(client,'command',lost)
    owners.recover_discovery_execution(task['task_id'],user_id=12).assert_current()


@pytest.mark.parametrize('stopped_account',[False],indirect=True)
def test_incomplete_journal_cannot_recover(stopped_account):
    task, _, _ = stopped_account
    assert not journal.summarize_model_attempts(task['task_id'],user_id=12)['recorded_usage_complete']
    with pytest.raises(StorageError,match='complete retained model usage'):
        owners.recover_discovery_execution(task['task_id'],user_id=12)


def test_startup_recovers_after_process_death_before_worker_admission(stopped_account,monkeypatch,tmp_path):
    task, _, _ = stopped_account
    task['workdir'] = str(tmp_path / 'research' / 'jobs' / task['task_id'])
    task['_research_record_enabled'] = True
    task['status'] = 'running'
    engine._write_manifest(task,'running')
    runtime._research_runtime.remove_owned(task['task_id'],user_id=12)
    monkeypatch.setattr(engine,'research_root',lambda:str(tmp_path / 'research'))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime,'spawn',lambda *args:spawned.append(args))
    assert engine.resume_interrupted_research() == 1
    assert len(spawned) == 1
    recovered = spawned[0][-1]
    from lib.research import recipe
    def graph(*args, model_budget_admit, **kwargs):
        assert recovered['_research_model_account']['_executionBudget'].consumption_snapshot() == (11,0,True)
        lease = model_budget_admit(input_tokens=1,max_output_tokens=2)
        assert lease.ordinal == 2
        lease.mark_dispatched()
        lease.settle({'prompt_tokens':1,'completion_tokens':1})
        return {'accepted':[], 'rejected':[], 'corpus_size':0}
    monkeypatch.setattr(recipe,'build_research_from_direction',graph)
    engine.run_research_task(recovered)
    assert recovered['status'] == 'done'
    assert recovered['_research_model_account']['_executionBudget'].consumption_snapshot() == (13,0,True)
    assert journal.summarize_model_attempts(task['task_id'],user_id=12)['reported_tokens'] == 13


def test_history_ack_failure_keeps_original_owner(stopped_account,monkeypatch):
    task, client, old = stopped_account
    command = client.command
    def refuse(name,args,command_id):
        if args.get('namespace') == owners.HISTORY_NAMESPACE:
            return {'version':99}
        return command(name,args,command_id)
    monkeypatch.setattr(client,'command',refuse)
    with pytest.raises(StorageError,match='history was not exactly acknowledged'):
        owners.recover_discovery_execution(task['task_id'],user_id=12)
    assert owners._load(task['task_id'],12)[-1] == old.value


def test_cancelled_evolution_binding_does_not_transfer_owner(stopped_account,monkeypatch,tmp_path):
    task, _, old = stopped_account
    task['workdir'] = str(tmp_path / 'research' / 'jobs' / task['task_id'])
    task['_research_record_enabled'] = True
    task['status'] = 'running'
    engine._write_manifest(task,'running')
    runtime._research_runtime.remove_owned(task['task_id'],user_id=12)
    monkeypatch.setattr(engine,'research_root',lambda:str(tmp_path / 'research'))
    def cancelled(*args,**kwargs):
        raise ValueError('owning evolution no longer permits execution')
    monkeypatch.setattr(records,'resume_evolution_binding',cancelled)
    assert engine.resume_interrupted_research() == 0
    assert owners._load(task['task_id'],12)[-1] == old.value
    assert runtime._research_runtime.get(task['task_id']) is None


def test_recovered_discovery_preserves_goal_allowance_deadline_and_terminal_actuals(stopped_account):
    from lib.agent_core.execution_budget import ModelBudgetAdmissionDenied
    from lib.evolution.bridge import terminal_actuals
    task, _, old = stopped_account
    original = binding.read_model_budget(task['task_id'], user_id=12)
    claim = owners.recover_discovery_execution(task['task_id'], user_id=12)
    task.update(_research_record_enabled=True, _research_recovered=True,
                _research_recovery_claim=claim)
    admit = engine._prepare_model_budget(task)
    carrier = task['_research_model_account']
    assert carrier['config']['executionLimits'] == original['execution_limits'] == LIMITS
    # Prior paid usage leaves 989, not a fresh 1000-token allowance.
    with pytest.raises(ModelBudgetAdmissionDenied):
        admit(input_tokens=990, max_output_tokens=10)
    lease = admit(input_tokens=980, max_output_tokens=20)
    assert lease.max_output_tokens == 9
    assert lease.ordinal == 2
    lease.mark_dispatched()
    lease.settle({'prompt_tokens': 980, 'completion_tokens': 9})
    assert carrier['_executionBudget'].consumption_snapshot() == (1000, 0, True)
    with pytest.raises(ModelBudgetAdmissionDenied):
        admit(input_tokens=0, max_output_tokens=1)
    task.update(kind='research', status='done', usage={}, finished_at=task['created_at'] + 1)
    actuals, complete = terminal_actuals(task)
    assert actuals['tokens'] == 1000 and complete
    assert binding.read_model_budget(task['task_id'], user_id=12) == original
    assert journal.summarize_model_attempts(task['task_id'], user_id=12)['attempt_count'] == 2
    with pytest.raises(StorageError, match='no longer owns'):
        old.assert_current()


def test_recovery_does_not_extend_expired_goal_deadline(stopped_account, monkeypatch):
    from lib.agent_core import execution_budget
    task, _, _ = stopped_account
    claim = owners.recover_discovery_execution(task['task_id'], user_id=12)
    task.update(_research_record_enabled=True, _research_recovered=True,
                _research_recovery_claim=claim)
    admit = engine._prepare_model_budget(task)
    with monkeypatch.context() as clock:
        clock.setattr(execution_budget.time, 'time', lambda: LIMITS['deadline'] + 1)
        with pytest.raises(execution_budget.ModelBudgetAdmissionDenied, match='deadline'):
            admit(input_tokens=1, max_output_tokens=1)
    summary = journal.summarize_model_attempts(task['task_id'], user_id=12)
    assert summary['attempt_count'] == 1
    assert summary['reported_tokens'] == 11
    assert summary['recorded_usage_complete']
