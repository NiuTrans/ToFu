"""Owned process-loss and accepted continuation ports for native Goal scenarios.

These helpers use real storage, ordinary conversation commands and the existing
resume fixture. They create no replacement execution or recovery authority.
"""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import uuid
from types import SimpleNamespace


def kill_at_ready(module, values, *, directory, name):
    ready = directory/(name+'-ready.json')
    config = directory/(name+'-config.json')
    log = directory/(name+'.log')
    assert not ready.exists()
    config.write_text(json.dumps(dict(values, ready=str(ready)))+'\n')
    with log.open('wb') as output:
        child = subprocess.Popen([sys.executable,'-m',module,str(config)],
            cwd=Path(__file__).resolve().parents[2], stdout=output,
            stderr=subprocess.STDOUT, start_new_session=True)
        try:
            expires = time.monotonic()+60
            while not ready.exists():
                assert child.poll() is None, log.read_text()[-6000:]
                assert time.monotonic()<expires, log.read_text()[-6000:]
                time.sleep(.02)
            receipt = json.loads(ready.read_text())
            if writer := receipt.get('checkpoint_writer'):
                # Reap the owned daemon in the still-live source host before
                # killing that host. The receipt follows Popen.wait, so a dead
                # writer cannot remain an unjoined orphan after this boundary.
                import psutil
                producer = psutil.Process(writer['pid'])
                assert producer.ppid() == child.pid
                producer.kill()
                joined = Path(writer['exit_receipt'])
                expires = time.monotonic() + 10
                while not joined.exists():
                    assert child.poll() is None and time.monotonic() < expires
                    time.sleep(.01)
                assert json.loads(joined.read_text()) == dict(pid=writer['pid'], returncode=-signal.SIGKILL)
            os.killpg(child.pid,signal.SIGKILL)
            assert child.wait(10)==-signal.SIGKILL
        finally:
            if child.poll() is None:
                os.killpg(child.pid,signal.SIGKILL)
                child.wait(10)
    return json.loads(ready.read_text())


def accept_recovery(storage, source, *, target_id):
    from lib.agent_core.abort_signal import AbortSignal
    from lib.conversation_sync.command_service import ConversationTurnCommandService
    from lib.conversation_sync.repository import SidecarConversationSyncRepository
    from lib.goal_runs.recovery import GoalRecoveryPorts, bind_goal_recovery_task, resume_chat_goal
    from lib.goal_runs.repository import SidecarGoalRunRepository
    from lib.goal_runs.service import GoalRunService, GoalRunServicePorts
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    from lib.orchestration.startup_recovery import retire_interrupted_orchestration_runs_detailed
    from lib.turn_lifecycle import get_attempt, mark_task_started, recover_running_attempts

    provider = lambda **kwargs: storage
    assert recover_running_attempts()==1
    retired = retire_interrupted_orchestration_runs_detailed(
        error={'kind':'worker_lost','source':'orchestration.startup_recovery'},client=provider)
    assert retired.retired==1
    runs=OrchestrationRunService(SidecarOrchestrationRunStore(2,client=provider))
    goals=SidecarGoalRunRepository(2,client=provider)
    conversations=SidecarConversationSyncRepository(client_factory=provider)
    started=[]
    def start(conv,cfg,data,abort_after,registered,**kwargs):
        task=dict(id=target_id,convId=conv,_userId=2,_tenant_id='',
            _turnId=cfg['_turnId'],_attemptId=cfg['_attemptId'],config=cfg,
            messages=[],status='running',abort_event=AbortSignal())
        registered(target_id)
        assert mark_task_started(task['_attemptId'],target_id,user_id=2)['status']=='running'
        started.append(task)
        return target_id,None
    commands=ConversationTurnCommandService(build_user_message=lambda *args:None,
        was_aborted_after=lambda *args:False,start_task=start)
    ports=GoalRecoveryPorts(conversations,goals,get_attempt,commands)
    bounds=dict(header_bytes=4096,snapshot_bytes=1<<20,identity_bytes=4096,io_timeout_ms=10000)
    assert resume_chat_goal(runs,source['run_id'],owner_user_id=2,ports=ports,
        pending_actor_bounds=bounds,storage=storage)=='goal_'+target_id
    assert len(started)==1
    task=started[0]
    seed=bind_goal_recovery_task(task,runs=runs,conversation_repository=conversations,
        get_attempt=get_attempt,pending_actor_bounds=bounds,storage=storage)
    inputs=seed.inspect_pending_inputs(maximum_bytes=8<<20,task=task)
    goal_service=GoalRunService(ports=GoalRunServicePorts(lambda *args:goals))
    goal_service.start(task,seed.definition)
    return SimpleNamespace(task=task,seed=seed,inputs=inputs,runs=runs,
        goals=goals,goal_service=goal_service)


def claim_recovery(context, storage, source):
    from lib.goal_runs.pending_claim import claim_pending_goal_actors
    from lib.identity import PrincipalContext
    principal=PrincipalContext.user(subject_id=source['wallet_id'],owner_user_id=2)
    claim=claim_pending_goal_actors(context.task,context.seed,principal=principal,storage=storage,
        wallet_user_id=source['wallet_id'],reserved_micro=source['held_micro'],
        header_bytes=4096,snapshot_bytes=1<<20,evidence_bytes=8<<20,io_timeout_ms=10000)
    return principal,claim


@contextmanager
def file_provider():
    """A fresh call writes only after a reconciled file-not-published observation."""
    bodies=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model']=='alpha-wire-a'
            bodies.append(body)
            observations=[row for row in body['messages'] if row.get('role')=='tool']
            try:
                last=json.loads(observations[-1]['content']) if observations else {}
            except (ValueError,TypeError):
                last={}
            if last.get('outcome')=='file_not_published':
                call=next(call for row in body['messages'] for call in row.get('tool_calls',[])
                    if call['id']==observations[-1]['tool_call_id'])
                delta={'tool_calls':[dict(index=0,id='retry-'+uuid.uuid4().hex,
                    type='function',function=call['function'])]}
                finish='tool_calls'
            else:
                delta={'content':'File publication verified.\n[PROGRESS: resolved=1 remaining=0]\n[VU: TASK_DONE]'}
                finish='stop'
            wire=('data: '+json.dumps({'choices':[{'delta':delta,'finish_reason':finish}],
                'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})
                +'\n\ndata: [DONE]\n\n').encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(wire)))
            self.end_headers()
            self.wfile.write(wire)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    server.daemon_threads=False
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01})
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/v1',bodies
    finally:
        server.shutdown();server.server_close();thread.join(5)
        assert not thread.is_alive()
