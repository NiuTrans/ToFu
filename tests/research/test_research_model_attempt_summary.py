"""Receipt fold respects concurrent settlement, paging and absent evidence."""
import pytest
from lib.research import model_attempt_journal as journal
from lib.agent_core.execution_budget import model_budget_admission
from lib.storage.errors import StorageError
from tests.research.test_research_model_attempt_journal import prepare
from tests.research.test_research_budget_binding import bound_source as bound_source
from tests.research.test_research_record_ack import research_task
from tests.research.test_research_workspace_evidence_guard import storage
pytestmark=pytest.mark.unit

def test_concurrent_settlement_with_one_row_pages(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    a=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
    b=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
    a.mark_dispatched();b.mark_dispatched()
    b.settle({'prompt_tokens':2,'completion_tokens':3})
    a.settle({'prompt_tokens':3,'completion_tokens':3})
    value=journal.summarize_model_attempts(task['task_id'],user_id=12,page_size=1)
    assert value['attempt_count']==2 and value['reported_tokens']==11
    assert value['account_token_lower_bound']==11 and value['recorded_usage_complete']

def test_pending_and_unknown_usage_are_not_zero_cost(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    a=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
    a.mark_dispatched()
    value=journal.summarize_model_attempts(task['task_id'],user_id=12,page_size=1)
    assert value['unsettled_attempts']==1 and not value['recorded_usage_complete']
    a.settle(None)
    value=journal.summarize_model_attempts(task['task_id'],user_id=12)
    assert value['unknown_usage_attempts']==1 and not value['recorded_usage_complete']

def test_empty_and_unsent_void_are_distinct(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    assert not journal.summarize_model_attempts(task['task_id'],user_id=12)['recorded_usage_complete']
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
    lease.settle(unsent=True)
    value=journal.summarize_model_attempts(task['task_id'],user_id=12)
    assert value['reported_tokens']==0 and value['recorded_usage_complete']

def test_corrupted_terminal_identity_is_rejected(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
    lease.mark_dispatched();lease.settle({'prompt_tokens':1,'completion_tokens':2})
    value=journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
    value['producer_id']='a'*32 if value['producer_id']!='a'*32 else 'b'*32
    client.command('record.put',{'namespace':journal.NAMESPACE,
        'key':journal._key(task['task_id'],1,'terminal',12),'value':value},'synthetic-terminal-corruption')
    with pytest.raises(StorageError,match='identity changed'):
        journal.summarize_model_attempts(task['task_id'],user_id=12,page_size=1)

def test_inherited_baseline_remains_unproven(bound_source):
    task,client,carrier,budget=prepare(bound_source)
    lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
    lease.mark_dispatched();lease.settle({'prompt_tokens':1,'completion_tokens':2})
    key=journal._key(task['task_id'],1,'terminal',12)
    value=journal.read_model_attempt(task['task_id'],1,'terminal',user_id=12)
    value['account_observed_tokens']=8
    client.command('record.put',{'namespace':journal.NAMESPACE,'key':key,'value':value},'synthetic-prior-baseline')
    summary=journal.summarize_model_attempts(task['task_id'],user_id=12,page_size=1)
    assert summary['reported_tokens']==3 and summary['account_token_lower_bound']==8
    assert not summary['recorded_usage_complete']
    assert summary['writer_fence_required_for_recovery']

def test_missing_ordinal_is_not_complete(bound_source,monkeypatch):
    task,client,carrier,budget=prepare(bound_source)
    for _ in range(2):
        lease=model_budget_admission(carrier)(input_tokens=1,max_output_tokens=10)
        lease.mark_dispatched();lease.settle({'prompt_tokens':1,'completion_tokens':2})
    original=journal.record_store.list_record_page
    def missing(*a,**k):return [row for row in original(*a,**k) if row['value']['attempt_ordinal']!=1]
    monkeypatch.setattr(journal.record_store,'list_record_page',missing)
    summary=journal.summarize_model_attempts(task['task_id'],user_id=12)
    assert summary['missing_ordinals']==1 and not summary['recorded_usage_complete']
    assert summary['account_token_lower_bound']==6
