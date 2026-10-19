"""On-demand goal spokesperson through the ordinary Tofu task engine.

One CAS-fenced manager task per goal, no daemon or second agent loop. Durable
updates survive missed wakes. Explicit reconcile diagnoses lost runtime tasks
without replaying their effects. Consumption is telemetry, not a lifetime quota.
Role permissions, task binding and CAS ownership remain authoritative.
"""
from __future__ import annotations
import json
from . import coordination as journal
from .contracts import EvolutionStateError
from .bridge import default_task_loader, derive_actuals


def wake(run_id, *, user_id, enable=False):
    from .library import _owner_run
    from lib.tasks_pkg.entry import build_chat_config
    from lib.tasks_pkg.manager import create_task
    from lib.tasks_pkg.manager._terminal import reject_unstarted_chat_task
    from lib.tasks_pkg.spawn import spawn_task
    from .automation import install
    run=_owner_run(run_id,user_id)
    if enable:journal.mutate(run_id,user_id,lambda state:state['manager'].update(enabled=True))
    state=journal.snapshot(run_id,user_id=user_id);manager=state['manager']
    if not manager['enabled'] or manager['status']!='idle':return manager
    pending=[e for e in state['updates'] if e['seq']>manager['through_seq']
             and e['kind'] not in ('reply','assignment') and (not e['task_id'] or e['task_id']!=manager['task_id'])]
    if not pending:return manager
    from .role_models import selected_model
    chosen=selected_model(run, "coordinator")
    config=build_chat_config(config={
        **({'modelRef':dict(chosen),'model':chosen.get('model_id') or chosen.get('offering_id')} if chosen else {}),
        'searchMode':'off','fetchEnabled':False,'codeExecEnabled':False,'humanGuidanceEnabled':False,
        'toolSpecAllowlist':['evolution'],
        'evolution':{'run_id':run_id,'goal_id':run['goal_id'],'plan_id':run['plan_id'],'stage':'coordinator'}})
    from pathlib import Path
    skill=Path(__file__).with_name('skills').joinpath('goal-coordinator','SKILL.md').read_text(encoding='utf-8')
    prompt=skill.split('---',2)[2].strip()+'\n'+json.dumps({
        'objective':run['objective'],'run_status':run['status'],
        'executor_task_id':run['task_id'],'last_delivery':run.get('result','')[:4000],
        'delivery_truncated':len(run.get('result',''))>4000,
        'previous_coordinator_reply':manager['last_response'],
        'inherited_update_revision':manager['through_seq']},ensure_ascii=False)
    task=create_task('',[{'role':'user','content':prompt}],config,user_id=user_id,supersede=False,transient=True)
    task['_evolutionPresented']=manager['through_seq']
    captured=state['revision']
    def claim(current):
        m=current['manager']
        if not m['enabled'] or m['status']!='idle' or m['turns']!=manager['turns']:
            raise EvolutionStateError('coordinator launch already claimed')
        m.update(task_id=task['id'],status='running',turns=m['turns']+1,error='')
        return m
    try:claimed=journal.mutate(run_id,user_id,claim)
    except Exception:
        reject_unstarted_chat_task(task,'coordinator launch not committed',cause='evolution_launch_failed')
        raise
    task['_coordinatorInputRevision']=captured
    from lib.agent_core.terminal_delivery import bind_terminal_delivery
    bind_terminal_delivery(task, settle)
    install();spawn_task(task)
    return claimed


def notify(run_id, *, user_id, enable=False):
    """Wake hint only, after commit. Failure is visible; updates remain durable."""
    try:return wake(run_id,user_id=user_id,enable=enable)
    except Exception as exc:
        from lib.log import get_logger
        get_logger(__name__).warning('Coordinator wake pending for %s: %s',run_id,type(exc).__name__)
        return {'status':'pending','error':'wake_failed; use coordinator reconcile'}


def settle(task):
    binding=(task.get('config') or {}).get('evolution') or {}
    run_id=binding['run_id'];owner=task['_userId'];tid=task['id']
    actuals=derive_actuals(task)
    from .library import _mutate

    def finish(row):
        state=journal.state_in(row)
        manager=state['manager']
        if manager['task_id']!=tid or manager['status']!='running':return False
        manager['tokens']+=actuals['tokens'];manager['tool_calls']+=actuals['tool_calls']
        success=task.get('status')=='done' and not task.get('error') and not task.get('_executionLimitReason')
        response=str(task.get('content') or '')
        success=success and bool(response.strip()) and len(response)<=8000
        manager.update(status='idle' if success else 'needs_attention',last_response=response[:8000],
                       error='' if success else 'coordinator_failed; inspect task',
                       through_seq=max(manager['through_seq'],int(task.get('_evolutionPresented',0))))
        if success:
            journal.append_to_row(row,event_id='reply:'+tid,kind='reply',content=response,
                                  origin='agent',target='coordinator',task_id=tid)
        return True
    changed=_mutate(run_id,owner,lambda items:None,row_change=finish)
    if changed:notify(run_id,user_id=owner)
    return journal.snapshot(run_id,user_id=owner)['manager']


def reconcile(run_id, *, user_id):
    from .library import _owner_run
    from .runs import TERMINAL
    run=_owner_run(run_id,user_id)
    if run['status'] in TERMINAL:journal.record_run_result(run,user_id=user_id)
    state=journal.snapshot(run_id,user_id=user_id);manager=state['manager']
    if manager['status']=='running':
        task=default_task_loader(manager['task_id'],user_id=user_id)
        if task is None:
            def lost(current):
                if current['manager']['task_id']==manager['task_id'] and current['manager']['status']=='running':
                    current['manager'].update(status='needs_attention',error='coordinator_task_unavailable; not replayed')
                return current['manager']
            return journal.mutate(run_id,user_id,lost)
        if int(task.get('_userId') or 0)!=int(user_id) or task.get('id')!=manager['task_id'] or (task.get('config') or {}).get('evolution',{}).get('run_id')!=run_id:
            raise EvolutionStateError('coordinator task owner or binding mismatch')
        if task.get('status') in ('done','error','aborted','interrupted'):return settle(task)
        return manager
    return wake(run_id,user_id=user_id)


def start_followup(run_id,payload, *, user_id,request_id):
    """An explicit POST authorizes one linked run; adding a source never does."""
    from .library import _owner_run,_validate,list_materials
    from .runs import submit_run,TERMINAL
    run=_owner_run(run_id,user_id);_validate(payload,'goal_update_request')
    if payload['kind']!='guidance' or not payload['content'].strip():raise EvolutionStateError('follow-up requires execution guidance')
    if not isinstance(request_id,str) or not 1<=len(request_id)<=160:raise EvolutionStateError('Idempotency-Key is required')
    if run['status'] not in TERMINAL:raise EvolutionStateError('finish or cancel the current execution before a follow-up')
    objective=run['objective']+'\nFollow-up requested by owner: '+payload['content']
    if len(objective)>4000:raise EvolutionStateError('combined follow-up objective exceeds 4000 characters')
    state=journal.snapshot(run_id,user_id=user_id)
    existing=state.get('followup')
    if existing and (existing['request_id']!=request_id or existing['content']!=payload['content']):
        raise EvolutionStateError('this goal already has a follow-up; continue in that goal')
    materials=list_materials(run_id,user_id=user_id)
    seed=existing['context_seed'] if existing else json.dumps({'parent_run_id':run_id,'source_revision':state['revision'],
                     'prior_result':run.get('result','')[:8000],
                     'materials':materials[-8:],'material_count':len(materials),
                     'materials_truncated':len(materials)>8,
                     'recent_updates':state['updates'][-8:]},ensure_ascii=False)
    if len(seed)>24000:raise EvolutionStateError('follow-up context too large; provide a focused new goal')
    def claim(current):
        old=current.get('followup')
        if old:
            if old['request_id']!=request_id or old['content']!=payload['content']:
                raise EvolutionStateError('this goal already has a follow-up; continue in that goal')
            return old
        current['followup']={'request_id':request_id,'content':payload['content'],'context_seed':seed,
                             'source_revision':state['revision'],'status':'pending','run_id':''}
        return current['followup']
    intent=journal.mutate(run_id,user_id,claim)
    objective=run['objective']+'\nFollow-up requested by owner: '+intent['content']
    if len(objective)>4000:raise EvolutionStateError('combined follow-up objective exceeds 4000 characters')
    # A linked iteration continues the same workspace and acceptance contract.
    # Carry immutable launch policy, not episode/task identities, spent usage,
    # absolute deadlines or scientific-protocol authority from the prior run.
    from copy import deepcopy
    inherited = {key: deepcopy(run[key]) for key in (
        'project_path', 'executor_plugins', 'executor_schema_budget_tokens', 'budget', 'review_reserve',
        'max_attempts', 'max_review_format_retries', 'acceptance_requirements',
        'require_requirement_originals', 'require_command_execution',
    ) if key in run}
    child=submit_run({**inherited,'objective':objective,'role_models':run.get('role_models', {}),**({'model':run['model']} if run.get('model') else {})},
                     user_id=user_id,idempotency_key='goal-followup:'+run_id,
                     context_seed=intent['context_seed'],parent_run_id=run_id)
    journal.mutate(run_id,user_id,lambda current:current['followup'].update(status='submitted',run_id=child['run_id']))
    return child


def cancel(run_id, *, user_id):
    state=journal.snapshot(run_id,user_id=user_id)
    journal.mutate(run_id,user_id,lambda current:current['manager'].update(enabled=False))
    if state['manager']['status']=='running':
        from lib.tasks_pkg.manager.cancellation import cancel_task
        cancel_task(state['manager']['task_id'],user_id=user_id,source='evolution.coordinator.cancel')
