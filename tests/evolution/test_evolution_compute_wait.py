"""A parked compute dependency observes only; terminal status resumes one carrier."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from tests.evolution.test_evolution_runs import runtime as runtime, finish, OWNER
from tofu_environments import Actor
from lib.evolution import compute_wait
from lib.evolution.contracts import EvolutionStateError

pytestmark=pytest.mark.unit

def launch(runtime,tmp_path):
 runs,_,tasks=runtime
 run=runs.submit_run({'objective':'Train and validate the original scientific result.',
  'project_path':str(tmp_path),'max_attempts':1,'executor_plugins':['environments']},user_id=OWNER)
 actor=Actor(OWNER,'',str(tmp_path));status={'operation_id':'job-test','plan_hash':'a'*64,
  'phase':'queued','durable':True,'reference':{'id':'remote-test'},'plan':{'scope':actor.scope,'project_root':actor.project_root}}
 return run,tasks[0],actor,status

def test_register_park_poll_and_terminal_resume_without_submission(runtime,tmp_path,monkeypatch):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status)
 parked=finish(runtime,task,'Waiting; result unknown.')
 assert parked['compute_wait']['parked'] and parked['status']=='executing'
 assert len(runtime[2])==1 and parked['review'] is None
 calls=[]
 def poll(wait,owner):calls.append((wait['operation_id'],owner));return deepcopy(status)
 monkeypatch.setattr(compute_wait,'status_for_wait',poll)
 queued=runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert len(calls)==1 and len(runtime[2])==1
 runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert len(calls)==1
 status.update(phase='succeeded',exit_code=0)
 monkeypatch.setattr(compute_wait.time,'time',lambda:queued['compute_wait']['next_poll_at']+1)
 resumed=runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert resumed['status']=='executing' and len(runtime[2])==2
 assert resumed['attempt']==1 and resumed['objective']==run['objective']
 assert resumed['episode_id']==run['episode_id']
 from lib.evolution.episodes import get_episode
 assert get_episode(resumed['episode_id'],user_id=OWNER)['task_id']==runtime[2][-1]['id']
 assert 'Do not plan, submit or cancel' in runtime[2][-1]['messages'][0]['content']
 runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert len(runtime[2])==2

@pytest.mark.parametrize('change',['owner','task','project','scope','unsubmitted','transient'])
def test_foreign_or_unsubmitted_dependency_refused(runtime,tmp_path,change):
 run,task,actor,status=launch(runtime,tmp_path);task=dict(task)
 if change=='owner':task['_userId']=OWNER+1
 if change=='task':task['id']='foreign'
 if change=='project':actor=Actor(OWNER,'','/foreign')
 if change=='scope':status['plan']['scope']='b'*64
 if change=='unsubmitted':status['phase']='planned'
 if change=='transient':status['durable']=False
 with pytest.raises(EvolutionStateError):compute_wait.register(task,actor,status)
 assert 'compute_wait' not in runtime[0].get_run(run['run_id'],user_id=OWNER)


def test_observation_error_retains_wait_and_does_not_spawn(runtime,tmp_path,monkeypatch):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 def fail(*a):raise TimeoutError('observer unavailable')
 monkeypatch.setattr(compute_wait,'status_for_wait',fail)
 pending=runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert pending['status']=='executing' and pending['compute_wait']['last_error']=='TimeoutError: observer unavailable'
 assert len(runtime[2])==1


@pytest.fixture
def router(monkeypatch):
 """Recording Router Watchdog seam; ``available`` toggles whether a Watchdog exists."""
 state=SimpleNamespace(reports=[],resolved=[],logged=[],available=True)
 def report(owner,**kwargs):
  state.reports.append(kwargs)
  return {'signalId':f"sig-{kwargs['failure']}-{kwargs['since']}"} if state.available else None
 monkeypatch.setattr('lib.router.watchdog.signals.report_compute_stall',report)
 monkeypatch.setattr('lib.router.watchdog.signals.resolve_compute_stall',
  lambda owner,signal_id,resolution:state.resolved.append((signal_id,resolution)) or True)
 monkeypatch.setattr(compute_wait.logger,'warning',lambda *a,**k:state.logged.append(a))
 return state


def test_failed_read_records_where_it_failed_and_routes_each_failure_once(runtime,tmp_path,monkeypatch,router):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 base=float(int(compute_wait.time.time()));clock=[base];monkeypatch.setattr(compute_wait.time,'time',lambda:clock[0])
 def missing_module(*a):
  from lib.runtime_paths import data_root  # noqa: F401  (the stale-install failure this guards)
 failure=[missing_module]
 monkeypatch.setattr(compute_wait,'status_for_wait',lambda *a:failure[0](*a))
 def poll():
  clock[0]+=10;return runtime[0].advance_run(run['run_id'],user_id=OWNER)
 router.available=False
 first=poll()['compute_wait']
 assert first['last_error']=="ModuleNotFoundError: No module named 'lib.runtime_paths'"
 assert first['last_error_origin'].endswith(f'test_evolution_compute_wait.py:{missing_module.__code__.co_firstlineno+1} in missing_module')
 assert first['failed_since']==base+10 and 'watchdog_signal_id' not in first and len(router.logged)==1
 [report]=router.reports
 assert report['failure']=='ModuleNotFoundError' and report['kind']=='compute_observation_failed'
 assert report['run_id']==run['run_id'] and report['operation_id']=='job-test' and report['since']==base+10
 assert 'Raised at' in report['detail'] and 'Traceback' in report['detail'] and 'job-test' in report['detail']
 # No Watchdog yet: every pass offers the same streak again until one takes it, logging nothing new.
 router.available=True
 assert poll()['compute_wait']['watchdog_signal_id']==f'sig-ModuleNotFoundError-{base+10}'
 poll()
 assert len(router.reports)==2 and len(router.logged)==1
 # A different failure class in the same streak supersedes the open signal.
 def denied(*a):raise PermissionError('profile unreadable')
 failure[0]=denied
 superseded=poll()['compute_wait']
 assert superseded['last_error']=='PermissionError: profile unreadable' and superseded['failed_since']==base+10
 assert superseded['watchdog_signal_id']==f'sig-PermissionError-{base+10}' and len(router.logged)==2
 assert router.resolved==[(f'sig-ModuleNotFoundError-{base+10}',
  f'Superseded by sig-PermissionError-{base+10}: the read now fails with PermissionError.')]
 # The first successful read closes the streak and its signal; the wait stays parked.
 failure[0]=lambda *a:deepcopy(status)
 recovered=poll()
 assert recovered['status']=='executing' and recovered['compute_wait']['parked']
 assert not {'last_error','last_error_origin','failed_since','watchdog_signal_id'}&set(recovered['compute_wait'])
 assert router.resolved[-1]==(f'sig-PermissionError-{base+10}','The status read succeeded again (phase queued).')
 # A later failure is a new streak and a new signal.
 failure[0]=denied
 assert poll()['compute_wait']['watchdog_signal_id']==f'sig-PermissionError-{base+60}'
 assert len(runtime[2])==1


def test_unverified_success_stays_visible_and_resumes_once_verified(runtime,tmp_path,monkeypatch,router):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 base=float(int(compute_wait.time.time()));clock=[base];monkeypatch.setattr(compute_wait.time,'time',lambda:clock[0])
 monkeypatch.setattr(compute_wait,'status_for_wait',lambda *a:deepcopy(status))
 status.update(phase='succeeded',exit_code=None)
 stalled=runtime[0].advance_run(run['run_id'],user_id=OWNER)['compute_wait']
 assert stalled['last_error']=='successful_exit_unverified: exit_code=None' and 'last_error_origin' not in stalled
 assert router.reports[0]['kind']=='compute_exit_unverified' and '"phase": "succeeded"' in router.reports[0]['detail']
 assert len(runtime[2])==1
 status.update(exit_code=0);clock[0]+=10
 resumed=runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert 'compute_wait' not in resumed and len(runtime[2])==2
 assert router.resolved==[(f'sig-successful_exit_unverified-{base}','The status read succeeded again (phase succeeded).')]


def test_cancel_closes_the_open_compute_signal(runtime,tmp_path,monkeypatch,router):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 def fail(*a):raise TimeoutError('observer unavailable')
 monkeypatch.setattr(compute_wait,'status_for_wait',fail)
 runtime[0].advance_run(run['run_id'],user_id=OWNER)
 cancelled=runtime[0].cancel_run(run['run_id'],user_id=OWNER)
 assert cancelled['status']=='cancelled' and 'watchdog_signal_id' not in cancelled['compute_wait']
 [(signal_id,resolution)]=router.resolved
 assert signal_id.startswith('sig-TimeoutError-') and 'cancelled' in resolution


def test_cancel_detaches_wait_without_touching_remote_job(runtime,tmp_path,monkeypatch):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 monkeypatch.setattr(compute_wait,'status_for_wait',lambda *a:pytest.fail('Cancelled wait must not poll'))
 cancelled=runtime[0].cancel_run(run['run_id'],user_id=OWNER)
 assert cancelled['status']=='cancelled'
 runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert len(runtime[2])==1


def test_presentation_snapshot_keeps_parked_wait_instead_of_settling(runtime,tmp_path):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 snapshot=runtime[0].get_run_snapshot(run['run_id'],user_id=OWNER)
 assert snapshot['status']=='executing' and snapshot['compute_wait']['parked']
 assert 'snapshot_pending_reconciliation' not in snapshot
 assert snapshot['actuals']==runtime[0].get_run(run['run_id'],user_id=OWNER)['actuals']


def test_parked_wait_survives_missing_original_task(runtime,tmp_path,monkeypatch):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 monkeypatch.setattr(runtime[0],'default_task_loader',lambda *a,**k:None)
 monkeypatch.setattr(compute_wait,'status_for_wait',lambda *a:deepcopy(status))
 restored=runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert restored['status']=='executing' and restored['compute_wait']['parked']
 assert len(runtime[2])==1


def test_continuation_keeps_one_episode_and_exact_consumption(runtime,tmp_path,monkeypatch):
 from lib.evolution.episodes import get_episode
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);parked=finish(runtime,task,'Waiting.')
 assert parked['actuals']['tokens']==15
 status.update(phase='failed',exit_code=1)
 monkeypatch.setattr(compute_wait,'status_for_wait',lambda *a:deepcopy(status))
 continued=runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert continued['episode_id']==run['episode_id'] and continued['actuals']['tokens']==15
 assert get_episode(continued['episode_id'],user_id=OWNER)['status']=='running'
 assert '"phase": "failed"' in runtime[2][-1]['messages'][0]['content']
 assert '"exit_code": 1' in runtime[2][-1]['messages'][0]['content']


def test_optional_sdk_tool_registers_wait_without_provider_mutation(runtime,tmp_path):
 from tofu_environments import host
 run,task,actor,status=launch(runtime,tmp_path)
 calls=[]
 service=SimpleNamespace(status=lambda a,op:(calls.append((a,op)) or deepcopy(status)))
 host.configure_jobs(service)
 try:
  returned=host._handler(task,None,'compute_wait','wait-call',{'operation_id':'job-test'},1,{}, {},str(tmp_path),True)
  import json
  assert json.loads(returned[1])['model_polling_required'] is False
  assert calls==[(actor,'job-test')] and task['_backgroundTaskAccepted']['tool']=='compute_wait'
  assert runtime[0].get_run(run['run_id'],user_id=OWNER)['compute_wait']['parked'] is False
 finally:host.configure_jobs(None)


def test_optional_sdk_wait_refuses_non_evolution_before_status():
 from tofu_environments import host
 host.configure_jobs(SimpleNamespace(status=lambda *a:pytest.fail('No provider call before authority')))
 try:
  import json
  reply=host._handler({'_userId':OWNER},None,'compute_wait','foreign',{'operation_id':'x'},1,{}, {},'/project',True)
  assert json.loads(reply[1])['code']=='evolution_required'
 finally:host.configure_jobs(None)
