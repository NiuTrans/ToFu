"""Accepted resume ancestry comes from scoped storage, never caller config."""
from unittest.mock import patch
import pytest
from lib.conversation_sync.dispatch_contract import read_accepted_resume_lineage

pytestmark=pytest.mark.unit


def records():
    receipt={'attemptId':'source','taskId':'source-task','status':'interrupted','cause':'user_stop'}
    task={'id':'target-task','_userId':7,'convId':'conversation','_turnId':'turn',
          '_attemptId':'target','config':{'_resumeSource':{'taskId':'forged'}}}
    target={'attemptId':'target','taskId':'target-task','turnId':'turn','conversationId':'conversation',
            'operation':'continue','status':'running','config':{'_resumeSource':receipt}}
    source={'attemptId':'source','taskId':'source-task','turnId':'turn','conversationId':'conversation',
            'operation':'generate','status':'interrupted'}
    turn={'turnId':'turn','conversationId':'conversation','currentAttemptId':'target','status':'running'}
    return task,target,source,turn


def read(task,target,source,turn):
    seen=[]
    def attempt(key,*,user_id,include_execution_config=False):
        assert user_id==7;seen.append((key,include_execution_config))
        return target if key=='target' else source
    def current(conversation_id,turn_id,*,user_id):
        assert (conversation_id,turn_id,user_id)==('conversation','turn',7)
        return turn
    with patch('lib.turn_lifecycle.get_attempt',side_effect=attempt), \
         patch('lib.turn_lifecycle.get_turn',side_effect=current):
        return read_accepted_resume_lineage(task),seen


def test_lineage_ignores_forged_task_config_and_reads_actual_scoped_receipt():
    task,target,source,turn=records()
    relation,seen=read(task,target,source,turn)
    assert (relation.task_id,relation.source_task_id)==('target-task','source-task')
    assert relation.owner_user_id==7
    assert seen==[('target',True),('source',False)]


@pytest.mark.parametrize('fault',['target_id','target_task','target_turn','operation','target_status','missing_receipt',
    'source_id','source_task','source_turn','source_live','changed_receipt_status','same_executor',
    'superseded','turn_id','conversation_id','extra_receipt_field','nonstring_cause'])
def test_lineage_refuses_foreign_stale_or_unbound_authority(fault):
    task,target,source,turn=records()
    if fault=='target_id':target['attemptId']='foreign'
    elif fault=='target_task':target['taskId']='foreign'
    elif fault=='target_turn':target['turnId']='foreign'
    elif fault=='operation':target['operation']='regenerate'
    elif fault=='target_status':target['status']='completed'
    elif fault=='missing_receipt':target['config'].clear()
    elif fault=='source_id':source['attemptId']='foreign'
    elif fault=='source_task':source['taskId']='foreign'
    elif fault=='source_turn':source['turnId']='foreign'
    elif fault=='source_live':source['status']='running'
    elif fault=='changed_receipt_status':target['config']['_resumeSource']['status']='failed'
    elif fault=='same_executor':target['config']['_resumeSource']['taskId']=task['id']
    elif fault=='superseded':turn['currentAttemptId']='successor'
    elif fault=='turn_id':turn['turnId']='foreign'
    elif fault=='conversation_id':turn['conversationId']='foreign'
    elif fault=='extra_receipt_field':target['config']['_resumeSource']['extra']='untrusted'
    else:target['config']['_resumeSource']['cause']=False
    with pytest.raises(ValueError):read(task,target,source,turn)


@pytest.mark.parametrize('fault',[None,'foreign_producer','foreign_turn','live_ancestor','cycle','byte_bound','cancel'])
def test_original_producer_requires_bounded_accepted_ancestry(fault):
    from lib.conversation_sync.dispatch_contract import validate_resume_producer
    task,target,source,turn=records()
    source['operation']='continue'
    source['config']={'_resumeSource':{'attemptId':'original','taskId':'original-task',
        'status':'interrupted','cause':'service_restart'}}
    original=dict(source,attemptId='original',taskId='original-task',operation='generate',config={})
    rows={'target':target,'source':source,'original':original}
    if fault=='foreign_turn':original['turnId']='foreign'
    elif fault=='live_ancestor':original['status']='running'
    elif fault=='cycle':source['config']['_resumeSource'].update(attemptId='source',taskId='source-task')
    def cancelled():
        if fault=='cancel':raise ValueError('cancelled')
    def attempt(key,*,user_id,include_execution_config=False):
        assert user_id==7
        return rows.get(key)
    with patch('lib.turn_lifecycle.get_attempt',side_effect=attempt),patch('lib.turn_lifecycle.get_turn',return_value=turn):
        def check():validate_resume_producer(task,'outside' if fault=='foreign_producer' else 'original-task',
            byte_limit=1 if fault=='byte_bound' else 4096,check_cancelled=cancelled)
        if fault is None:check()
        else:
            with pytest.raises(ValueError):check()
