"""Durable provider-neutral compute dependency; the existing run reaper owns polling.

Entry points register(task, actor, status), advance(run, version, user_id), park.
No model loop, submission, cancellation, daemon or provider credential is stored.
Optional SDK ports load only for an enrolled wait; jobs retain status authority.
"""
from contextlib import ExitStack
from pathlib import Path
import json
import time
from .contracts import EvolutionStateError


def register(task, actor, status):
    from . import runs
    binding=(task.get('config') or {}).get('evolution') or {}
    run_id=binding.get('run_id')
    run,version=runs._read(run_id,actor.owner_id)
    if (run['status']!='executing' or run['task_id']!=task.get('id')
            or binding.get('stage')!='executing' or task.get('_userId')!=actor.owner_id
            or Path(run.get('project_path') or '').resolve()!=Path(actor.project_root).resolve()):
        raise EvolutionStateError('compute wait requires the current owned project executor')
    if status['plan']['scope']!=actor.scope or status['plan']['project_root']!=actor.project_root:
        raise EvolutionStateError('compute wait operation scope changed')
    if status.get('durable') is not True:
        raise EvolutionStateError('compute wait requires durable operation state')
    if status['phase'] not in ('queued','running'):
        raise EvolutionStateError('compute wait requires a submitted nonterminal operation')
    wait={'operation_id':status['operation_id'],'plan_hash':status['plan_hash'],
          'scope':actor.scope,'project_root':actor.project_root,'tenant_id':actor.tenant_id,
          'task_id':task['id'],'next_poll_at':0,'parked':False}
    previous=run.get('compute_wait')
    if previous and any(previous[k]!=wait[k] for k in ('operation_id','plan_hash','scope','task_id')):
        raise EvolutionStateError('another compute dependency is already enrolled')
    if not previous:
        run['compute_wait']=wait;runs._save(run,version,actor.owner_id)
    task['_backgroundTaskAccepted']={'taskId':task['id'],'tool':'compute_wait',
        'message':'算力等待已登记；实验仍在运行或排队。结果到达后由系统继续处理，不重复提交。'}
    return {'status':'waiting','operation_id':wait['operation_id'],'model_polling_required':False,
            'remote_submission_performed':False}


def status_for_wait(wait, user_id):
    from tofu_environments import Actor
    from tofu_environments.host import _default_service,_LOCK
    import tofu_environments.host as host
    actor=Actor(user_id,wait['tenant_id'],wait['project_root'])
    if actor.scope!=wait['scope']:raise EvolutionStateError('compute wait owner changed')
    task={'id':wait['task_id'],'_userId':user_id,'_tenantId':wait['tenant_id']}
    with ExitStack() as lifecycle:
        with _LOCK:service=host._JOB_SERVICE
        if service is None:service=_default_service(task,jobs=True,lifecycle=lifecycle)
        return service.status(actor,wait['operation_id'])


def park(run, version, user_id):
    from . import runs
    wait=run['compute_wait']
    if wait['task_id']!=run['task_id']:raise EvolutionStateError('compute wait executor changed')
    wait['parked']=True
    run['result']='Compute dependency pending; no completion or scientific acceptance.'
    runs._save(run,version,user_id)
    return run


def advance(run, version, user_id):
    from . import runs
    wait=run['compute_wait']
    if time.time()<wait['next_poll_at']:return run
    if runs._budget_exhausted(run,'executing'):
        run.pop('compute_wait');run.update(status='settling',review=None,error='compute_wait_execution_budget_exhausted')
        return runs._finish(run,runs._save(run,version,user_id),user_id)
    wait['next_poll_at']=time.time()+5
    version=runs._save(run,version,user_id)
    try:observed=status_for_wait(wait,user_id)
    except Exception as exc:
        wait['last_error']=type(exc).__name__;runs._save(run,version,user_id);return run
    if observed['operation_id']!=wait['operation_id'] or observed['plan_hash']!=wait['plan_hash'] or observed['plan']['scope']!=wait['scope']:
        raise EvolutionStateError('compute wait observation identity changed')
    if observed['phase'] not in ('succeeded','failed','cancelled'):return run
    if observed['phase']=='succeeded' and observed.get('exit_code')!=0:
        wait['last_error']='successful_exit_unverified';runs._save(run,version,user_id);return run
    run.pop('compute_wait');run['compute_wait_resume']={'task_id':wait['task_id'],'operation_id':wait['operation_id']}
    prompt=runs._execution_prompt(run,user_id)+'\nContinue after the registered compute dependency. '
    prompt+='The original submission instruction has already been satisfied; it is not an instruction to replay it. Do not plan, submit or cancel this existing operation. Observe/collect only when successful; analyse failure without claiming success. Original goal and acceptance remain unchanged.\n'
    prompt+=json.dumps({'registered_compute_terminal':observed},ensure_ascii=False)
    version=runs._prepare_stage(run,version,user_id,stage='executing',frozen_prompt=prompt)
    return runs._launch(run,version,user_id,stage='executing')


def bind_continuation_episode(run, task, user_id):
    """Rebind the same running logical episode after acknowledged carrier birth."""
    from .episodes import _episode_key,get_episode
    from .records import NAMESPACE_EPISODES,get_versioned_record,put_record
    from .contracts import validate_episode
    expected=run['compute_wait_resume']['task_id']
    episode,version=get_versioned_record(NAMESPACE_EPISODES,_episode_key(run['episode_id'],user_id))
    if episode['status']!='running' or episode['goal_id']!=run['goal_id'] or episode['plan_id']!=run['plan_id']:
        raise EvolutionStateError('compute continuation episode binding changed')
    if episode['task_id']==task['id']:return
    if episode['task_id']!=expected:raise EvolutionStateError('compute continuation parent changed')
    episode.update(task_id=task['id']);validate_episode(episode)
    try:put_record(NAMESPACE_EPISODES,_episode_key(run['episode_id'],user_id),episode,kind='episode',expected_version=version)
    except Exception:
        if get_episode(run['episode_id'],user_id=user_id)!=episode:raise
    if get_episode(run['episode_id'],user_id=user_id)!=episode:
        raise EvolutionStateError('compute continuation episode publication unconfirmed')
