"""Goal event delivery, ownership, CAS, coordinator handoff and bounded storage."""
import json
import pytest
from lib.evolution import coordination as c, coordinator as manager, library
from lib.evolution.contracts import EvolutionStateError
from tests.support.evolution_store import install_store
pytestmark=pytest.mark.unit

@pytest.fixture
def world(monkeypatch):
    store=install_store(monkeypatch)
    run={'run_id':'a','goal_id':'a','plan_id':'a','task_id':'worker','status':'executing','objective':'Deliver tested output','result':''}
    from lib.evolution import records
    store.rows[(records.NAMESPACE_RUNS,'12:a')]=dict(run)
    monkeypatch.setattr('lib.evolution.runs.get_run',lambda rid,*,user_id:dict(run) if rid=='a' and user_id==12 else None)
    tasks=[]
    def create(conv,messages,config,**kwargs):
        task={'id':f'manager-{len(tasks)}','_userId':kwargs['user_id'],'config':config,'messages':messages,'status':'running'}
        tasks.append(task);return task
    monkeypatch.setattr('lib.tasks_pkg.manager.create_task',create)
    monkeypatch.setattr('lib.tasks_pkg.spawn.spawn_task',lambda task:task.update(spawned=True))
    monkeypatch.setattr('lib.tasks_pkg.manager._terminal.reject_unstarted_chat_task',lambda task,*args,**kw:task.update(rejected=True))
    from types import SimpleNamespace
    monkeypatch.setattr('lib.agent_core.execution_session.execution_session_for_task',lambda task:SimpleNamespace())
    monkeypatch.setattr('lib.evolution.automation.install',lambda:None)
    monkeypatch.setattr(manager,'derive_actuals',lambda task:{'tokens':10,'tool_calls':1,'wall_clock_seconds':1})
    return store,run,tasks


def update(text='Add a regression check',kind='guidance',key='one'):
    return c.publish('a',{'kind':kind,'content':text},user_id=12,event_id=key)


def worker():return {'id':'worker','_userId':12,'config':{'evolution':{'run_id':'a','stage':'executing'}}}


def test_updates_are_idempotent_and_owner_scoped(world):
    first=update();assert update()==first
    with pytest.raises(EvolutionStateError):update('changed')
    with pytest.raises(EvolutionStateError):c.snapshot('a',user_id=99)
    assert c.snapshot('a',user_id=12)['revision']==1


def test_shelf_and_notice_are_one_commit_even_when_wake_fails(world,monkeypatch):
    monkeypatch.setattr(manager,'wake',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('offline')))
    item=library.save_material('a',{'kind':'paper','title':'Reference','url':'https://example.org/paper'},user_id=12)
    state=c.snapshot('a',user_id=12)
    assert state['updates'][0]['references']==[item['url']]
    assert len(library.list_materials('a',user_id=12))==1
    store=world[0];store.permanent_failure=True
    with pytest.raises(RuntimeError):library.remove_material('a',item['material_id'],user_id=12)
    store.permanent_failure=False
    assert c.snapshot('a',user_id=12)['revision']==1
    assert len(library.list_materials('a',user_id=12))==1


def test_delivery_preserves_tool_pairs_and_requires_explicit_ack(world):
    update();task=worker()
    messages=[{'role':'assistant','tool_calls':[{'id':'tc','type':'function','function':{'name':'x','arguments':'{}'}}]}]
    c.inject_updates(task,messages,1);assert len(messages)==1
    with pytest.raises(EvolutionStateError):c.acknowledge(task,1,'accepted','considered')
    messages.append({'role':'tool','tool_call_id':'tc','content':'done'})
    c.inject_updates(task,messages,2)
    assert task['_evolutionPresented']==1
    assert c.snapshot('a',user_id=12)['acks']==[]
    assert c.unacknowledged_guidance(world[1],user_id=12)
    c.acknowledge(task,1,'accepted','will add regression')
    assert not c.unacknowledged_guidance(world[1],user_id=12)
    before=len(messages);c.inject_updates(task,messages,3);assert len(messages)==before
    update('New constraint',key='two');c.inject_updates(task,messages,4)
    assert task['_evolutionPresented']==2
    task['id']='stale'
    with pytest.raises(EvolutionStateError):c.acknowledge(task,2,'accepted','stale')


def test_update_capacity_refuses_without_evicting_material_or_history(world):
    for i in range(96):update(str(i),kind='experiment',key=str(i))
    with pytest.raises(EvolutionStateError):library.save_material('a',{'kind':'web','title':'No room','url':'https://example.org/'},user_id=12)
    assert library.list_materials('a',user_id=12)==[]
    state=c.snapshot('a',user_id=12);assert len(state['updates'])==96
    assert len(json.dumps(state).encode())<c.MAX_WORKSPACE_BYTES


def test_manager_single_flight_pending_messages_and_durable_reply(world):
    update('Please explain the next step',kind='message')
    first=manager.wake('a',user_id=12,enable=True)
    second=manager.wake('a',user_id=12,enable=True)
    assert first['task_id']==second['task_id'];assert len(world[2])==1
    task=world[2][0];c.inject_updates(task,task['messages'],1)
    update('Also consider this result',kind='message',key='two')
    task.update(status='done',content='I will compare the evidence.')
    manager.settle(task)
    assert len(world[2])==2 and world[2][1]['spawned']
    state=c.snapshot('a',user_id=12)
    assert any(e['kind']=='reply' and e['content']==task['content'] for e in state['updates'])
    assert state['manager']['tokens']==10
    manager.settle(task);assert c.snapshot('a',user_id=12)['manager']['tokens']==10


def test_manager_lost_runtime_is_not_replayed(world,monkeypatch):
    update(kind='message');manager.wake('a',user_id=12,enable=True)
    monkeypatch.setattr(manager,'default_task_loader',lambda *a,**kw:None)
    result=manager.reconcile('a',user_id=12)
    assert result['status']=='needs_attention' and 'not replayed' in result['error']
    assert len(world[2])==1


def test_manager_usage_is_telemetry_not_a_lifetime_quota(world):
    update(kind='message')
    c.mutate('a',12,lambda state:state['manager'].update(
        turns=160,tokens=640000,tool_calls=640))
    result=manager.wake('a',user_id=12,enable=True)
    assert result['status']=='running' and result['error']==''
    assert result['turns']==161 and len(world[2])==1
    task=world[2][0];cfg=task['config']
    assert 'executionLimits' not in cfg and '_invocationDeadline' not in task
    assert 'maxTokens' not in cfg  # Ordinary model/provider output policy, not 2,000 tokens.
    assert cfg['codeExecEnabled'] is False and cfg['fetchEnabled'] is False
    assert cfg['toolSpecAllowlist']==['evolution']
    assert cfg['humanGuidanceEnabled'] is False


def test_role_specific_assignments_are_not_global_instructions(world):
    update(kind='message');manager.wake('a',user_id=12,enable=True);task=world[2][0]
    c.inject_updates(task,task['messages'],1)
    result=c.handle_coordinate(task,None,'evolution_coordinate','call',{'action':'assign','source_seq':1,'content':'Run the regression test'},1,{}, {},'',False)
    assert result[0]['fetched']
    state=c.snapshot('a',user_id=12)
    assert any(e['kind']=='assignment' for e in c.page_for(state,'executing')['updates'])
    assert not any(e['kind']=='assignment' for e in c.page_for(state,'reviewing')['updates'])
    w=worker()
    assert not c.handle_coordinate(w,None,'evolution_coordinate','call',{'action':'assign','source_seq':1,'content':'Not allowed'},1,{}, {},'',False)[0]['fetched']


def test_cas_collision_cannot_drop_an_update(world,monkeypatch):
    store=world[0];original=store.command;once=True
    def command(op,payload,cid):
        nonlocal once
        if once:
            once=False;update('concurrent',key='other')
        return original(op,payload,cid)
    monkeypatch.setattr(store,'command',command)
    update();state=c.snapshot('a',user_id=12)
    assert [e['seq'] for e in state['updates']]==[1,2]
    assert {e['content'] for e in state['updates']}=={'concurrent','Add a regression check'}


def test_manager_claim_collision_rejects_unstarted_loser(world,monkeypatch):
    update(kind='message');original=c.mutate;once=True
    def mutate(rid,uid,change):
        nonlocal once
        # Race at the claim, after enable but before this task is committed.
        if once and world[2]:
            once=False;manager.wake('a',user_id=12)
        return original(rid,uid,change)
    monkeypatch.setattr(c,'mutate',mutate)
    with pytest.raises(EvolutionStateError):manager.wake('a',user_id=12,enable=True)
    assert len([t for t in world[2] if t.get('spawned')])==1
    assert len([t for t in world[2] if t.get('rejected')])==1


def test_followup_freezes_context_and_reuses_intent(world,monkeypatch):
    world[1]['status']='completed';calls=[]
    def submit(payload,**kw):calls.append((payload,kw));return {'run_id':'child'}
    monkeypatch.setattr('lib.evolution.runs.submit_run',submit)
    update('Original source report',kind='experiment')
    body={'kind':'guidance','content':'Now reproduce the result'}
    manager.start_followup('a',body,user_id=12,request_id='follow')
    update('Later report',kind='experiment',key='later')
    manager.start_followup('a',body,user_id=12,request_id='follow')
    assert calls[0][1]['context_seed']==calls[1][1]['context_seed']
    assert calls[0][1]['parent_run_id']=='a'
    assert calls[0][1]['idempotency_key']==calls[1][1]['idempotency_key']
    with pytest.raises(EvolutionStateError):manager.start_followup('a',body,user_id=12,request_id='different')


def test_utf8_workspace_budget_refuses_without_partial_append(world):
    from lib.evolution import records
    for i in range(96):
        try:update('资料'*2000,kind='experiment',key=str(i))
        except EvolutionStateError:break
    state=c.snapshot('a',user_id=12)
    assert 0<len(state['updates'])<96
    revision=state['revision']
    with pytest.raises(EvolutionStateError):update('资料'*2000,kind='experiment',key='overflow')
    assert c.snapshot('a',user_id=12)['revision']==revision
    assert max(len(json.dumps(v,ensure_ascii=False).encode()) for v in world[0].rows.values())<=c.MAX_WORKSPACE_BYTES


def test_large_reference_lists_have_bounded_preview_and_explicit_detail(world):
    payload={'kind':'experiment','content':'Evidence '*400,'references':['https://example.org/'+str(i)+'x'*1800 for i in range(8)]}
    c.publish('a',payload,user_id=12,event_id='large')
    state=c.snapshot('a',user_id=12);page=c.page_for(state,'executing')
    assert page['updates'][0]['detail_required'] is True
    assert len(json.dumps(page,ensure_ascii=False))<c.PAGE_CHARS+300
    task=worker();task['_evolutionPresented']=1
    result=c.handle_coordinate(task,None,'evolution_coordinate','detail',{'action':'detail','through_seq':1},1,{}, {},'',False)
    assert len(json.loads(result[0]['snippet'])['references'])==8


def test_cancellation_before_manager_claim_prevents_spawn(world,monkeypatch):
    update(kind='message');original=c.mutate;once=True
    def mutate(rid,uid,change):
        nonlocal once
        if once and world[2]:
            once=False
            original(rid,uid,lambda state:state['manager'].update(enabled=False))
        return original(rid,uid,change)
    monkeypatch.setattr(c,'mutate',mutate)
    with pytest.raises(EvolutionStateError):manager.wake('a',user_id=12,enable=True)
    assert not any(task.get('spawned') for task in world[2])
    assert world[2][0]['rejected']


def test_owner_message_to_assignment_execution_receipt_and_report(world):
    update('Please verify the new result',kind='message')
    manager.wake('a',user_id=12,enable=True);task=world[2][0]
    c.inject_updates(task,task['messages'],1)
    c.acknowledge(task,1,'accepted','Will assign a reproducibility check')
    assignment=c.handle_coordinate(task,None,'evolution_coordinate','assign-1',
        {'action':'assign','source_seq':1,'content':'Reproduce the result and report evidence'},1,{}, {},'',False)
    assert assignment[0]['fetched']
    execution=worker();messages=[];c.inject_updates(execution,messages,1)
    assert c.unacknowledged_guidance(world[1],user_id=12)
    c.acknowledge(execution,execution['_evolutionPresented'],'accepted','Will reproduce with fixed inputs')
    assert not c.unacknowledged_guidance(world[1],user_id=12)
    report=c.handle_coordinate(execution,None,'evolution_coordinate','report-1',
        {'action':'report','content':'Reproduction completed; independent review still required'},1,{}, {},'',False)
    assert report[0]['fetched']
    receipt = json.loads(report[0]['snippet'])
    assert receipt['delivery_status'] == 'progress_only' and receipt['run_status'] == 'executing'
    assert 'final assistant answer' in receipt['next_step']
    assert receipt['completion_tool'] == 'evolution_coordinate'
    assert receipt['completion_action'] == 'submit_result'
    stored = next(row for row in c.snapshot('a',user_id=12)['updates'] if row['kind'] == 'experiment')
    assert 'delivery_status' not in stored and stored['content'] == receipt['content']
    c.inject_updates(task,task['messages'],2)
    assert any('independent review still required' in str(message) for message in task['messages'])
    assert world[1]['status']=='executing'


def test_curiosity_is_visible_to_coordinator_not_executor(world):
    update('Why is this taking time?',kind='message')
    state=c.snapshot('a',user_id=12)
    assert c.page_for(state,'executing')['updates']==[]
    assert len(c.page_for(state,'coordinator')['updates'])==1
    assert not c.unacknowledged_guidance(world[1],user_id=12)


def test_conversation_receipt_tracks_the_linked_assignment_not_prose(world):
    from lib.evolution.conversation import conversation_page
    update('Please add a regression check',kind='message')
    manager.wake('a',user_id=12,enable=True);task=world[2][0]
    c.inject_updates(task,task['messages'],1)
    c.acknowledge(task,1,'accepted','This needs an executor change')
    page=lambda:conversation_page('a',user_id=12,recent=True)
    assert page()['delivery']['1']['status']=='considered'
    bad=c.handle_coordinate(task,None,'evolution_coordinate','bad',{'action':'assign','source_seq':999,'content':'Cannot cite unseen input'},1,{}, {},'',False)
    assert not bad[0]['fetched']
    c.handle_coordinate(task,None,'evolution_coordinate','assigned',{'action':'assign','source_seq':1,'content':'Add regression evidence'},1,{}, {},'',False)
    assert page()['delivery']['1']['status']=='assigned'
    executor=worker();c.inject_updates(executor,[],1)
    c.acknowledge(executor,2,'conflict','Conflicts with existing acceptance criteria')
    assert page()['delivery']['1']['status']=='conflict'
    c.acknowledge(executor,2,'accepted','Resolved and will implement')
    assert page()['delivery']['1']['status']=='accepted'
    assert world[1]['status']=='executing'
    from jsonschema import validate
    from lib.evolution.contracts import load_schema
    schema=load_schema('evolution_run_v1.schema.json')
    validate(page(),{'$ref':'#/$defs/coordination_page','$defs':schema['$defs']})


def test_recent_and_earlier_pages_keep_newest_large_message_without_gaps(world):
    from lib.evolution.conversation import conversation_page
    for i in range(12):update(str(i)+'x'*3900,kind='message',key=str(i))
    page=conversation_page('a',user_id=12,recent=True);seen=[e['seq'] for e in page['updates']]
    assert seen[-1]==12
    while page['has_earlier']:
        page=conversation_page('a',user_id=12,before=page['before_seq'])
        seen=[e['seq'] for e in page['updates']]+seen
    assert seen==list(range(1,13))
    with pytest.raises(EvolutionStateError):conversation_page('a',user_id=99,recent=True)


def test_coordinator_can_save_owner_sources_after_execution_ends(world):
    world[1]['status']='completed';update('Save this source for reference',kind='message')
    manager.wake('a',user_id=12,enable=True);task=world[2][0]
    result=library.handle_material(task,None,'evolution_material','save',{'action':'save','kind':'web','title':'Owner reference','url':'https://example.org/reference','note':'For later comparison'},1,{}, {},'',False)
    assert result[0]['fetched']
    assert len(library.list_materials('a',user_id=12))==1
    task['id']='stale-manager'
    refused=library.handle_material(task,None,'evolution_material','list',{'action':'list'},1,{}, {},'',False)
    assert not refused[0]['fetched']
    assert world[1]['status']=='completed' and len(world[2])==1


def test_direct_headless_guidance_has_executor_receipt(world):
    from lib.evolution.conversation import conversation_page
    update('Use local storage')
    task=worker();c.inject_updates(task,[],1);c.acknowledge(task,1,'accepted','Will use local storage')
    assert conversation_page('a',user_id=12)['delivery']['1']['status']=='accepted'


def test_coordinator_reads_complete_delivery_with_bounded_pages(world):
    world[1].update(status='completed',result='x'*4000+'Final evidence')
    update('Explain the final evidence',kind='message');manager.wake('a',user_id=12,enable=True)
    task=world[2][0]
    def read(after):
        result=c.handle_coordinate(task,None,'evolution_coordinate','read-result',{'action':'delivery','after':after},1,{}, {},'',False)
        assert result[0]['fetched']
        return json.loads(result[0]['snippet'])
    first=read(0);assert len(first['result'])==4000 and first['next_cursor']==4000
    second=read(first['next_cursor']);assert second['result']=='Final evidence' and second['next_cursor'] is None
    assert world[1]['status']=='completed'


@pytest.mark.parametrize('terminal_status', ['done', 'error'])
def test_coordinator_full_journal_settles_once_without_replay(world, terminal_status):
    """A concurrent journal fill cannot strand a terminal coordinator as running."""
    update('Please explain the next step', kind='message')
    manager.wake('a', user_id=12, enable=True)
    task = world[2][0]
    task['_evolutionPresented'] = 1

    def fill(state):
        c.append_to_row({'coordination': state}, event_id='terminal:worker',
                        kind='run_result', content='Run result', origin='system')
        while len(state['updates']) < c.MAX_UPDATES:
            seq = len(state['updates'])
            c.append_to_row({'coordination': state}, event_id=f'old-reply-{seq}',
                            kind='reply', content='Retained response', origin='agent')
    c.mutate('a', 12, fill)
    before = c.snapshot('a', user_id=12)['updates']
    task.update(status=terminal_status, content='The terminal response remains visible.')
    if terminal_status == 'error':
        task['error'] = 'provider failed'
    settled = manager.settle(task)
    assert settled['status'] == 'needs_attention'
    assert settled['tokens'] == 10 and settled['tool_calls'] == 1
    assert settled['last_response'] == task['content']
    assert c.snapshot('a', user_id=12)['updates'] == before
    assert len(world[2]) == 1
    assert manager.settle(task) == settled


def test_coordinator_full_journal_refuses_launch_before_allocating_task(world):
    update('Please explain the next step', kind='message')
    def fill(state):
        c.append_to_row({'coordination': state}, event_id='terminal:worker',
                        kind='run_result', content='Run result', origin='system')
        while len(state['updates']) < c.MAX_UPDATES:
            seq = len(state['updates'])
            c.append_to_row({'coordination': state}, event_id=f'old-reply-{seq}',
                            kind='reply', content='Retained response', origin='agent')
    c.mutate('a', 12, fill)
    settled = manager.wake('a', user_id=12, enable=True)
    assert settled['status'] == 'needs_attention'
    assert settled['error'] == 'coordinator_reply_capacity_exhausted'
    assert world[2] == []


@pytest.mark.parametrize('terminal_status', ['done', 'error'])
def test_coordinator_byte_capacity_keeps_task_reference_and_accounts_once(world, monkeypatch, terminal_status):
    from lib.evolution import records
    update('Explain the result', kind='message')
    manager.wake('a', user_id=12, enable=True)
    task = world[2][0]
    task['_evolutionPresented'] = 1
    before = c.snapshot('a', user_id=12)['updates']
    row = next(value for (namespace, _), value in world[0].rows.items()
               if namespace == records.NAMESPACE_LIBRARIES)
    # Model a concurrent fill leaving enough space for metadata, not a reply.
    monkeypatch.setattr(c, 'MAX_WORKSPACE_BYTES', len(json.dumps(row, ensure_ascii=False).encode()) + 256)
    task.update(status=terminal_status, content='资料' * 4000)
    settled = manager.settle(task)
    assert settled['status'] == 'needs_attention'
    assert settled['task_id'] == task['id']
    assert 'capacity' in settled['error']
    assert settled['tokens'] == 10 and settled['tool_calls'] == 1
    assert c.snapshot('a', user_id=12)['updates'] == before
    assert len(world[2]) == 1
    assert manager.settle(task) == settled
    row = next(value for (namespace, _), value in world[0].rows.items()
               if namespace == records.NAMESPACE_LIBRARIES)
    assert len(json.dumps(row, ensure_ascii=False).encode()) <= c.MAX_WORKSPACE_BYTES


def test_replies_preserve_terminal_slot_and_bytes(world):
    update(kind='message')
    def fill(state):
        while len(state['updates']) < c.MAX_UPDATES - 1:
            c.append_to_row({'coordination': state}, event_id=f"reply-{state['revision']}",
                            kind='reply', content='response', origin='agent')
    c.mutate('a', 12, fill)
    assert manager.wake('a', user_id=12, enable=True)['status'] == 'needs_attention'
    assert not world[2]
    with pytest.raises(EvolutionStateError):
        c.mutate('a', 12, lambda state: c.append_to_row({'coordination': state},
            event_id='one-too-many', kind='reply', content='reply', origin='agent'))
    world[1].update(status='completed', result='Verified result')
    result = c.record_run_result(world[1], user_id=12)
    assert result['kind'] == 'run_result'
    assert c.record_run_result(world[1], user_id=12) == result
    assert len(c.snapshot('a', user_id=12)['updates']) == c.MAX_UPDATES


def test_legacy_full_journal_does_not_block_terminal_reconciliation(world, monkeypatch):
    from lib.evolution import records
    update(kind='message')
    manager.wake('a', user_id=12, enable=True)
    task = world[2][0]
    # Build a valid legacy snapshot before reply-slot reservations existed.
    row = next(value for (namespace, _), value in world[0].rows.items()
               if namespace == records.NAMESPACE_LIBRARIES)
    state = row['coordination']
    while len(state['updates']) < c.MAX_UPDATES:
        seq = state['revision'] + 1
        state['updates'].append({'event_id': f'legacy-{seq}', 'kind': 'reply',
            'content': 'preserved', 'origin': 'agent', 'target': 'all',
            'task_id': '', 'references': [], 'seq': seq, 'created_at': 1})
        state['revision'] = seq
    before = json.dumps(state['updates'])
    world[1].update(status='completed', result='Verified result')
    monkeypatch.setattr(manager, 'default_task_loader', lambda *a, **kw: task)
    task.update(status='done', content='Run is finished')
    result = manager.reconcile('a', user_id=12)
    assert result['status'] == 'needs_attention'
    assert result['tokens'] == 10
    assert manager.reconcile('a', user_id=12)['tokens'] == 10
    assert json.dumps(c.snapshot('a', user_id=12)['updates']) == before
    assert len(world[2]) == 1


@pytest.mark.parametrize('state',['done','error','aborted','interrupted','abort_flag','abort_event'])
def test_local_task_revocation_refuses_before_durable_authority_read(world,monkeypatch,state):
    import threading
    task=worker()
    if state=='abort_flag':task['aborted']=True
    elif state=='abort_event':
        task['abort_event']=threading.Event();task['abort_event'].set()
    else:task['status']=state
    monkeypatch.setattr(library,'_owner_run',lambda *a,**k:pytest.fail('revoked task must not read authority'))
    with pytest.raises(EvolutionStateError,match='no tool authority'):c.authorize_task(task)


def test_cancellation_during_authority_read_refuses_before_grant(world,monkeypatch):
    task=worker();owner_run=library._owner_run
    def cancel(*args,**kwargs):
        task['aborted']=True
        return owner_run(*args,**kwargs)
    monkeypatch.setattr(library,'_owner_run',cancel)
    with pytest.raises(EvolutionStateError,match='no tool authority'):c.authorize_task(task)


def test_revoked_task_update_injection_preserves_existing_messages(world):
    task=worker();task['aborted']=True
    messages=[{'role':'user','content':'Original objective'}]
    c.inject_updates(task,messages,0)
    assert messages==[{'role':'user','content':'Original objective'}]
