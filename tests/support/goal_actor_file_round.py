"""Real registered file gateway, parked before the native result ACK.

The enclosing Goal SIGKILL probe owns the private database and process. Model
usage is a fixed paid fixture; preparation, quota, handler and file proof are real.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import signal
import threading
import time


class GoalActorFileRound:
    def __init__(self, child, state, directory, ready, run_id, phase):
        from lib.identity import PrincipalContext
        from lib.agent_core.native_process import CheckpointRequest, RoundRequest, RoundSettlement
        from lib.tasks_pkg.invocation import InvocationPolicy
        from lib.tasks_pkg.native_gateway import NativeGateway
        from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
        from lib.tasks_pkg.handlers import project  # noqa: F401
        from lib.project_mod import modifications
        from tests.tasks_pkg.test_native_gateway import catalog

        self.child, self.ready, self.run_id, self.phase = child, Path(ready), run_id, phase
        self.project = Path(directory).parent / 'project'; self.project.mkdir()
        modifications.SESSIONS_DIR = str(Path(directory).parent / 'file-journal')
        self.principal = PrincipalContext.user(subject_id='goal-crash', owner_user_id=7)
        child.update(_transientRuntime=True, _invocationStorageFree=False,
            _invocationPolicy=InvocationPolicy.child(child['_parentTaskId']),
            _invocationEffectLock=threading.RLock(), events=[], events_lock=threading.Lock(),
            abort_event=threading.Event(), _attended=True, _dispatch_heartbeat=0.,
            _t_last_event=0., toolRounds=[], created_at=time.time(), aborted=False)
        child['config'].update(projectPath=str(self.project), autoApply=True,
            tools={'resultEnvelope': 'legacy'})
        definitions = catalog(); child['_executable_tool_catalog'] = definitions
        self.executor = ThreadPoolExecutor(max_workers=1)
        self.gateway = NativeGateway(principal=self.principal, task=child, executor=self.executor,
            configuration=child['config'], tool_list=definitions, permissions={'write_file': 'write'},
            project_path=str(self.project), project_enabled=True, model='fixture', round_source=lambda: 0,
            decision_policy=None, pending_calls=4, pending_bytes=65536)
        self.recovery = NativeFileToolRecovery(principal=self.principal, task=child,
            executor=self.executor, registered_child=True)
        self.recovery.bind_gateway(self.gateway)
        self.args = dict(path='result.txt', content='once\n', append=True)
        state['messages'][-1]['tool_calls'][0]['function']['arguments'] = json.dumps(self.args)
        self.gateway.prepare_response(RoundSettlement(
            RoundRequest(self.principal, child['id'], 'model-result', 0, 0),
            dict(kind='returned', model_calls=state['messages'][-1]['tool_calls'])))
        request = CheckpointRequest(self.principal, child['id'], 'pending', 0,
            state['epoch'], state['binding'], threading.Event(), state=state)
        self.state = asyncio.run(self.recovery.capture(request))
        self.request = replace(request, state=self.state)

    def execute_until_interruption(self):
        from lib.agent_core.native_process import ApprovalRequest, ToolRequest
        from lib.project_mod.write_tools import _ops
        from lib.project_mod import effect_reconciliation

        def park(*args, **kwargs):
            self.ready.write_text(json.dumps(dict(run_id=self.run_id,
                operation_id=self.state['tool_recovery']['calls'][0]['inspection']['operationId'])))
            while True:
                signal.pause()
        if self.phase == 'file_published':
            _ops._record_modification_checked = park
        else:
            bind = effect_reconciliation.bind_prepared_write
            def before_replace(record):
                if self.phase == 'file_unknown':
                    park()
                bind(record)
                park()
            effect_reconciliation.bind_prepared_write = before_replace
        async def execute():
            await self.recovery.committed(self.request)
            decision = await self.gateway.decide(ApprovalRequest(self.principal,
                self.child['id'], 'approve', 'write-1', 'write_file', self.request.epoch,
                json.dumps(self.args).encode(), b'null', asyncio.Event(), 1))
            assert decision == 'auto_write_mode', decision
            await self.gateway.execute(ToolRequest(self.principal, self.child['id'],
                'execute', 'write_file', self.args, 'write-1', model_round=0))
        asyncio.run(execute())
        raise AssertionError('file gateway returned before the injected crash')
