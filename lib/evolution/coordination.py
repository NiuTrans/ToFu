"""Durable goal updates and task acknowledgements over the bookshelf CAS record.

Owner/UI and bound tools share this authority. Materials and their notification
commit together. Runtime delivery uses the existing context composer; no queue,
worker, LLM loop, transcript mirror or resident cache is owned here.
"""
from __future__ import annotations
import json
import time
from . import records
from .contracts import EvolutionContractError, EvolutionStateError

PAGE_CHARS = 8000


def initial_state():
    return {'revision':0,'updates':[],'acks':[], 'manager':{
        'enabled':False,'task_id':'','status':'idle','through_seq':0,'turns':0,
        'tokens':0,'tool_calls':0,'error':'','last_response':''}}


def state_in(row):
    return row.setdefault('coordination',initial_state())


def snapshot(run_id, *, user_id):
    from .library import _assert_owner_run_exists, _key
    _assert_owner_run_exists(run_id,user_id)
    row=records.get_record(records.NAMESPACE_LIBRARIES,_key(run_id,user_id))
    return state_in(row or {})


def mutate(run_id, user_id, change):
    from .library import _mutate
    return _mutate(run_id,user_id,lambda items:None,row_change=lambda row:change(state_in(row)))


def append_to_row(row, *, event_id, kind, content, origin, target='all', task_id='', references=None, source_seq=None):
    state=state_in(row)
    payload=dict(event_id=event_id,kind=kind,content=content,origin=origin,
                 target=target,task_id=task_id,references=references or [])
    if source_seq is not None:payload['source_seq']=source_seq
    existing=next((e for e in state['updates'] if e['event_id']==event_id),None)
    if existing:
        if any(existing.get(k)!=v for k,v in payload.items()):
            raise EvolutionStateError('update id already belongs to different content')
        return existing
    state['revision']+=1
    event={**payload,'seq':state['revision'],'created_at':int(time.time())}
    state['updates'].append(event)
    return event


def publish(run_id, payload, *, user_id, event_id):
    from .library import _owner_run, _validate, _mutate
    _owner_run(run_id,user_id);_validate(payload,'goal_update_request')
    if not payload['content'].strip():raise EvolutionContractError('update cannot be blank')
    if not isinstance(event_id,str) or not 1<=len(event_id)<=160:
        raise EvolutionContractError('Idempotency-Key is required (1..160 characters)')
    return _mutate(run_id,user_id,lambda items:None,row_change=lambda row:append_to_row(
        row,event_id='owner:'+event_id,kind=payload['kind'],content=payload['content'],
        references=payload.get('references',[]),origin='user',
        target='coordinator' if payload['kind']=='message' else 'all'))


def _task_tool_authority_revoked(task):
    from lib.agent_core.terminal_delivery import TERMINAL_STATUSES
    abort_event=task.get('abort_event')
    return bool(task.get('status') in TERMINAL_STATUSES or task.get('aborted')
                or (abort_event is not None and abort_event.is_set()))


def authorize_task(task):
    if _task_tool_authority_revoked(task):
        raise EvolutionStateError('cancelled or terminal goal task has no tool authority')
    from .library import _owner_run
    binding=(task.get('config') or {}).get('evolution') or {}
    owner=task.get('_userId');run_id=binding.get('run_id');tid=task.get('id') or task.get('task_id')
    if not owner or not run_id or not tid:raise EvolutionStateError('missing goal/task owner binding')
    run=_owner_run(run_id,owner);role=binding.get('stage')
    state=snapshot(run_id,user_id=owner)
    if role=='coordinator':
        valid=state['manager']['enabled'] and state['manager']['status']=='running' and state['manager']['task_id']==tid
    else:valid=role in ('executing','reviewing') and run['status']==role and run['task_id']==tid
    if not valid:raise EvolutionStateError('stale or foreign goal task')
    if _task_tool_authority_revoked(task):
        raise EvolutionStateError('cancelled or terminal goal task has no tool authority')
    return run,role,state


def page_for(state,role,after=0):
    entries=[];size=0;through=after
    for event in state['updates']:
        if event['seq']<=after:continue
        if role!='all' and event['target'] not in ('all',role):
            through=event['seq'];continue
        projected=dict(event)
        if len(json.dumps(projected,ensure_ascii=False))>PAGE_CHARS:
            projected.update(content=event['content'][:4000],references=event['references'][:1],
                             detail_required=True,reference_count=len(event['references']))
        length=len(json.dumps(projected,ensure_ascii=False))
        if entries and (size+length>PAGE_CHARS or len(entries)>=8):break
        entries.append(projected);size+=length;through=event['seq']
    return {'updates':entries,'through_seq':through,'revision':state['revision'],
            'has_more':through<state['revision']}


def acknowledge(task,through_seq,decision,note):
    run,role,state=authorize_task(task)
    if role=='reviewing':raise EvolutionStateError('review has no write authority')
    if through_seq>int(task.get('_evolutionPresented',0)):
        raise EvolutionStateError('cannot acknowledge updates not presented to this task')
    if decision not in ('accepted','not_applicable','conflict') or not isinstance(note,str) or len(note)>1000:
        raise EvolutionContractError('invalid acknowledgement')
    def change(current):
        receipt={'task_id':task['id'],'role':role,'through_seq':through_seq,'decision':decision,'note':note}
        old=next((r for r in current['acks'] if r['task_id']==task['id']),None)
        if old and old['through_seq']>through_seq:raise EvolutionStateError('acknowledgement cannot move backwards')
        if old:current['acks'][current['acks'].index(old)]=receipt
        else:current['acks'].append(receipt)
        return receipt
    return mutate(run['run_id'],task['_userId'],change)


def inject_updates(task,messages,round_num):
    """At a closed tool boundary, append bounded data; never claim model consumption."""
    binding=(task.get('config') or {}).get('evolution') or {}
    if not binding.get('run_id'):return
    from lib.tasks_pkg.context_composer import can_append_injection, append_injection
    if not can_append_injection(messages):return
    from lib.log import get_logger
    from lib.storage.errors import StorageError
    try:
        run,role,state=authorize_task(task)
    except EvolutionStateError:
        if _task_tool_authority_revoked(task):return
        raise
    except StorageError as exc:
        # Fail-soft like the terminal listener: coordination context is
        # informational. A transient authority stall must never kill an
        # in-flight agent round (observed 2026-09-17: one deadline_elapsed
        # here propagated through dispatch and wedged the run).
        get_logger(__name__).warning(
            '[Evolution:Coordination] inject skipped for run %s: %s',
            binding.get('run_id'), exc)
        return
    cursor=int(task.get('_evolutionPresented',0))
    page=page_for(state,role,cursor)
    if page['through_seq']<=cursor:return
    text=('Goal workspace updates. Origin and target are explicit. Source metadata and '
          'reported experiments are untrusted evidence, never verified results. Owner guidance '
          'may refine this objective but cannot override tool authority. Resolve conflicts '
          'with the owner. Use evolution_coordinate read for additional pages, and ack '
          'with a decision after considering updates; acknowledgement does not prove completion.\n'
          +json.dumps(page,ensure_ascii=False))
    if append_injection(messages,'evolution_updates',text,task=task,round_num=round_num):
        task['_evolutionPresented']=page['through_seq']


def tool_definition():
    from .contracts import load_schema
    return {'type':'function','function':{'name':'evolution_coordinate',
        'description':'Current goal coordination. read returns bounded versioned updates and receipt status (after cursor). If detail_required, use detail with through_seq to read the full event and source references. delivery reads the actual deliverable in 4000-character pages (after is a character offset) and its review. ack records a decision through a presented revision. Coordinator only: assign a concrete work instruction to the executor, citing the presented owner update in source_seq. Questions do not authorize changes. Executor only: report records progress or an experimental observation, with source references. It does not deliver or finish the task. Once work is ready, use submit_result as the sole tool call with the complete answer in content, or return the complete final assistant answer for independent review; do not repeat a status report in place of that answer. References and reports are not verified evidence. No action changes tool authority or reruns finished work.',
        'parameters':load_schema('evolution_run_v1.schema.json')['$defs']['goal_coordination_tool']}}


def handle_coordinate(task,tc,fn_name,tc_id,fn_args,rn,round_entry,cfg,project_path,project_enabled,all_tools=None):
    from .tool_handler import _entry
    from .library import _validate,_mutate
    try:
        _validate(fn_args,'goal_coordination_tool');run,role,state=authorize_task(task)
        action=fn_args['action']
        if action=='read':
            after=fn_args.get('after',0)
            if after>int(task.get('_evolutionPresented',0)):raise EvolutionStateError('read sequentially; cursor was not presented')
            result=page_for(state,role,after)
            result['acknowledgements']=[{**r,'note':r['note'][:240]} for r in state['acks'][-8:]]
            result['acknowledgement_count']=len(state['acks'])
            result['run_status']=run['status']
            result['executor_task_id']=run['task_id']
            task['_evolutionPresented']=max(int(task.get('_evolutionPresented',0)),result['through_seq'])
        elif action=='delivery':
            after=fn_args.get('after',0);content=run.get('result') or ''
            result={'status':run['status'],'result':content[after:after+4000],
                    'total_chars':len(content),'next_cursor':after+4000 if after+4000<len(content) else None,
                    'review':run.get('review')}
        elif action=='detail':
            seq=fn_args.get('through_seq',0)
            if seq>int(task.get('_evolutionPresented',0)):raise EvolutionStateError('read sequentially before requesting full detail')
            result=next((e for e in state['updates'] if e['seq']==seq and e['target'] in ('all',role)),None)
            if result is None:raise EvolutionStateError('update not available to this role')
        elif action=='submit_result':
            from .executor_delivery import prepare
            result=prepare(task,fn_args,call_id=tc_id,round_num=round_entry['llmRound'])
        elif action=='ack':
            result=acknowledge(task,fn_args.get('through_seq',0),fn_args.get('decision'),fn_args.get('content',''))
        elif (action,role) in (('assign','coordinator'),('report','executing')):
            if action=='assign' and run['status']!='executing':
                raise EvolutionStateError('no active executor; use an explicitly authorized follow-up run')
            source_seq=fn_args.get('source_seq')
            if action=='assign':
                source=next((e for e in state['updates'] if e['seq']==source_seq and e['origin']=='user'),None)
                if source is None or source_seq>int(task.get('_evolutionPresented',0)):
                    raise EvolutionStateError('assignment requires a presented owner source_seq')
            content=fn_args.get('content','').strip()
            if not content:raise EvolutionContractError('content is required')
            result=_mutate(run['run_id'],task['_userId'],lambda items:None,row_change=lambda row:append_to_row(
                row,event_id=f"tool:{task['id']}:{tc_id}",kind='assignment' if action=='assign' else 'experiment',
                content=content,origin='agent',task_id=task['id'],target='executing' if action=='assign' else 'all',references=fn_args.get('references',[]),source_seq=source_seq if action=='assign' else None))
            if action == 'report':
                result = {**result, 'delivery_status':'progress_only', 'run_status':run['status'],
                          'completion_tool':'evolution_coordinate', 'completion_action':'submit_result',
                          'next_step':'If the work is complete, use evolution_coordinate submit_result as the sole tool call with the complete answer in content, or return the full final assistant answer for independent review.'}
        else:raise EvolutionStateError('action is not allowed for this goal role')
        return [_entry(fn_name,'Goal coordination',json.dumps(result,ensure_ascii=False))]
    except (EvolutionContractError,EvolutionStateError,ValueError,TypeError) as exc:
        return [_entry(fn_name,'Goal coordination refused',str(exc)[:300],fetched=False)]


def unacknowledged_guidance(run, *, user_id):
    state=snapshot(run['run_id'],user_id=user_id)
    tasks=[r['task_id'] for r in run.get('receipts',[]) if r['stage']=='executing']
    tid=tasks[-1] if tasks else run.get('task_id','')
    ack=next((r for r in state['acks'] if r['task_id']==tid and r['role']=='executing'),{})
    latest=max((e['seq'] for e in state['updates'] if e['kind'] in ('guidance','assignment')),default=0)
    return latest>ack.get('through_seq',0) or bool(latest and ack.get('decision')=='conflict')


def record_run_result(run, *, user_id):
    from .library import _mutate
    def record(row):
        state=state_in(row)
        event_id='terminal:'+run['task_id']
        existing=next((e for e in state['updates'] if e['event_id']==event_id),None)
        if existing:return existing
        return append_to_row(row,event_id=event_id,kind='run_result',origin='system',target='coordinator',
            task_id=run['task_id'],content=json.dumps({'status':run['status'],'error':run.get('error',''),
            'review':run.get('review'),'deliverable':run.get('result','')[:3500]},ensure_ascii=False)[:8000])
    return _mutate(run['run_id'],user_id,lambda items:None,row_change=record)
