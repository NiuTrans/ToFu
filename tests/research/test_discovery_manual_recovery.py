"""Manual resume reuses durable discovery ownership and usage recovery."""
import pytest
from lib.research import records, runtime, engine, recipe, discovery_owner as owners
from lib.research import model_attempt_journal as journal
from tests.research.test_discovery_account_recovery import stopped_account as stopped_account, bound_source, research_task, storage
pytestmark = pytest.mark.unit


def retire(task):
    runtime._research_runtime.remove_owned(task['task_id'],user_id=12)


def test_manual_resume_enters_worker_with_original_consumption(stopped_account,monkeypatch,tmp_path):
    task, _, _ = stopped_account
    retire(task)
    assert records.load_record(task['task_id'],user_id=12)['resumable']
    monkeypatch.setattr(engine,'research_root',lambda:str(tmp_path / 'research'))
    spawned = []
    monkeypatch.setattr(runtime._research_runtime,'spawn',lambda *args:spawned.append(args))
    assert records.resume_record(task['task_id'],user_id=12) == {'task_id':task['task_id'],'deduped':False}
    assert len(spawned) == 1
    recovered = spawned[0][-1]
    def graph(*args,model_budget_admit,**kwargs):
        account = recovered['_research_model_account']['_executionBudget']
        assert account.consumption_snapshot() == (11,0,True)
        lease = model_budget_admit(input_tokens=1,max_output_tokens=2)
        assert lease.ordinal == 2
        lease.mark_dispatched();lease.settle({'prompt_tokens':1,'completion_tokens':1})
        return {'accepted':[], 'rejected':[], 'corpus_size':0}
    monkeypatch.setattr(recipe,'build_research_from_direction',graph)
    engine.run_research_task(recovered)
    assert recovered['status'] == 'done'
    assert journal.summarize_model_attempts(task['task_id'],user_id=12)['reported_tokens'] == 13


@pytest.mark.parametrize('stopped_account',[False],indirect=True)
def test_unknown_usage_does_not_advertise_or_create_manual_resume(stopped_account,monkeypatch):
    task, _, old = stopped_account
    retire(task)
    before = records.load_record(task['task_id'],user_id=12,internal=True)
    assert not records.load_record(task['task_id'],user_id=12)['resumable']
    monkeypatch.setattr(runtime,'_new_research_task',lambda *args,**kwargs:pytest.fail('no replacement birth'))
    with pytest.raises(ValueError,match='complete retained model usage'):
        records.resume_record(task['task_id'],user_id=12)
    assert records.load_record(task['task_id'],user_id=12,internal=True) == before
    assert owners._load(task['task_id'],12)[-1] == old.value


def test_foreign_owner_cannot_inspect_or_recover(stopped_account):
    task, _, old = stopped_account
    retire(task)
    assert records.load_record(task['task_id'],user_id=13) is None
    with pytest.raises(ValueError,match='not found'):
        records.resume_record(task['task_id'],user_id=13)
    assert owners._load(task['task_id'],12)[-1] == old.value


def test_cancelled_evolution_does_not_transfer_on_manual_resume(stopped_account,monkeypatch):
    task, _, old = stopped_account
    retire(task)
    def cancelled(*args,**kwargs):
        raise ValueError('evolution no longer permits execution')
    monkeypatch.setattr(records,'resume_evolution_binding',cancelled)
    with pytest.raises(ValueError,match='no longer permits'):
        records.resume_record(task['task_id'],user_id=12)
    assert owners._load(task['task_id'],12)[-1] == old.value


def test_storage_outage_retains_typed_failure_without_task_birth(stopped_account,monkeypatch):
    from lib.storage.errors import StorageError
    task, _, old = stopped_account
    retire(task)
    def unavailable(*args,**kwargs):
        raise StorageError('database_unavailable','synthetic selected store outage')
    monkeypatch.setattr(owners,'recover_discovery_execution',unavailable)
    with pytest.raises(StorageError,match='selected store outage'):
        records.resume_record(task['task_id'],user_id=12)
    assert runtime._research_runtime.get(task['task_id']) is None
    assert owners._load(task['task_id'],12)[-1] == old.value
