"""Durable provider-neutral compute dependency; the existing run reaper owns polling.

Entry points register(task, actor, status), advance(run, version, user_id), park,
release(run, user_id, resolution).
No model loop, submission, cancellation, daemon or provider credential is stored.
Optional SDK ports load only for an enrolled wait; jobs retain status authority.
A failed status read (or a success whose exit cannot be verified) keeps the wait
parked and records where it failed: the bounded error, the raising frame and
the streak start. Each new failure is logged once with its traceback and
reported to the owner's Router Watchdog (lib.router.watchdog.signals); the
first successful read, the end of the wait or cancellation closes that signal.
"""
from contextlib import ExitStack
from functools import lru_cache
from pathlib import Path
import json
import os
import sys
import time
import traceback
from lib.log import get_logger
from .contracts import EvolutionStateError, load_schema

logger = get_logger(__name__)


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


@lru_cache(maxsize=1)
def _last_error_bound():
    return load_schema('evolution_run_v1.schema.json')['$defs']['compute_wait']['properties']['last_error']['maxLength']


def _headline(exc):
    message=str(exc).strip()
    return f'{type(exc).__name__}: {message}' if message else type(exc).__name__


def _raised_at(exc):
    """Innermost source frame of the failure, relative to its import root."""
    frames=[frame for frame in traceback.extract_tb(exc.__traceback__) if not frame.filename.startswith('<')]
    if not frames:return ''
    frame=frames[-1]
    roots=[os.path.join(os.path.abspath(entry),'') for entry in sys.path if entry]
    root=max((root for root in roots if frame.filename.startswith(root)),key=len,default='')
    return f'{frame.filename[len(root):]}:{frame.lineno} in {frame.name}'


def _close_signal(wait, user_id, resolution):
    signal_id=wait.pop('watchdog_signal_id',None)
    if signal_id:
        from lib.router.watchdog.signals import resolve_compute_stall
        resolve_compute_stall(user_id,signal_id,resolution)


def release(run, user_id, resolution):
    """The wait ends without a successful read: close its open fault signal."""
    if run.get('compute_wait'):_close_signal(run['compute_wait'],user_id,resolution)


def _stalled(run, version, user_id, *, failure, error, origin, kind, detail, exc=None):
    """Keep the wait parked and record where the read failed; never resubmit."""
    from . import runs
    from lib.router.watchdog.signals import report_compute_stall
    wait=run['compute_wait']
    error=error[:_last_error_bound()]
    changed=(wait.get('last_error'),wait.get('last_error_origin',''))!=(error,origin)
    if changed:
        logger.warning('[EvolutionComputeWait] run=%s operation=%s status read failed at %s: %s',
                       run['run_id'],wait['operation_id'],origin or '-',error,exc_info=exc)
    wait.setdefault('failed_since',time.time())
    wait['last_error']=error
    if origin:wait['last_error_origin']=origin
    else:wait.pop('last_error_origin',None)
    previous=wait.get('watchdog_signal_id')
    if changed or not previous:
        signal=report_compute_stall(user_id,run_id=run['run_id'],operation_id=wait['operation_id'],
            project_path=wait['project_root'],failure=failure,since=wait['failed_since'],kind=kind,detail=detail)
        if signal is not None and signal['signalId']!=previous:
            wait['watchdog_signal_id']=signal['signalId']
            if previous:
                from lib.router.watchdog.signals import resolve_compute_stall
                resolve_compute_stall(user_id,previous,f'Superseded by {signal["signalId"]}: the read now fails with {failure}.')
    runs._save(run,version,user_id)
    return run


def advance(run, version, user_id):
    from . import runs
    wait=run['compute_wait']
    if time.time()<wait['next_poll_at']:return run
    if runs._budget_exhausted(run,'executing'):
        release(run,user_id,'The compute wait ended: the run reached its configured execution budget.')
        run.pop('compute_wait');run.update(status='settling',review=None,error='compute_wait_execution_budget_exhausted')
        return runs._finish(run,runs._save(run,version,user_id),user_id)
    wait['next_poll_at']=time.time()+5
    version=runs._save(run,version,user_id)
    try:observed=status_for_wait(wait,user_id)
    except Exception as exc:
        origin=_raised_at(exc)
        detail='\n'.join([_headline(exc),f'Raised at {origin}' if origin else '',f'Operation: {wait["operation_id"]}','',
                          ''.join(traceback.format_exception(exc))])
        return _stalled(run,version,user_id,failure=type(exc).__name__,error=_headline(exc),origin=origin,
                        kind='compute_observation_failed',detail=detail,exc=exc)
    if observed['operation_id']!=wait['operation_id'] or observed['plan_hash']!=wait['plan_hash'] or observed['plan']['scope']!=wait['scope']:
        raise EvolutionStateError('compute wait observation identity changed')
    if observed['phase']=='succeeded' and observed.get('exit_code')!=0:
        error=f'successful_exit_unverified: exit_code={observed.get("exit_code")!r}'
        return _stalled(run,version,user_id,failure='successful_exit_unverified',error=error,origin='',
                        kind='compute_exit_unverified',detail=f'{error}\nOperation: {wait["operation_id"]}\n'
                        +json.dumps({'registered_compute_status':observed},ensure_ascii=False,default=str))
    if 'last_error' in wait:
        _close_signal(wait,user_id,f'The status read succeeded again (phase {observed["phase"]}).')
        for key in ('last_error','last_error_origin','failed_since'):wait.pop(key,None)
        if observed['phase'] not in ('succeeded','failed','cancelled'):runs._save(run,version,user_id)
    if observed['phase'] not in ('succeeded','failed','cancelled'):return run
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
