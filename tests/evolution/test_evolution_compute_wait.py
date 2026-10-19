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
 assert pending['status']=='executing' and pending['compute_wait']['last_error']=='TimeoutError'
 assert len(runtime[2])==1


def test_cancel_detaches_wait_without_touching_remote_job(runtime,tmp_path,monkeypatch):
 run,task,actor,status=launch(runtime,tmp_path);compute_wait.register(task,actor,status);finish(runtime,task,'Waiting.')
 monkeypatch.setattr(compute_wait,'status_for_wait',lambda *a:pytest.fail('Cancelled wait must not poll'))
 cancelled=runtime[0].cancel_run(run['run_id'],user_id=OWNER)
 assert cancelled['status']=='cancelled'
 runtime[0].advance_run(run['run_id'],user_id=OWNER)
 assert len(runtime[2])==1


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
