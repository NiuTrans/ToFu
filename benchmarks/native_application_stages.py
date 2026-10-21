"""Opt-in joined HTTP ROOT stage diagnostics; never timing admission.

Wrappers delegate exact arguments/results/exceptions to existing owners. Sync
CPU is calling-thread time, async spans record wall only. Nested spans overlap
and cannot be summed. No task content, credentials, callbacks or results retained.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from functools import wraps
import threading
import time
from unittest.mock import patch


_PHASES = ContextVar('tofu_diagnostic_stage_parents', default=())


class Stages:
    def __init__(self):
        self.origin=time.perf_counter_ns();self.rows=[];self.lock=threading.Lock()

    @contextmanager
    def span(self,name,*,cpu=True,operation=None):
        start=time.perf_counter_ns();thread_start=time.thread_time_ns() if cpu else None
        outcome='returned'
        parents=_PHASES.get()
        token=_PHASES.set((*parents,name))
        try:yield
        except BaseException:
            outcome='raised';raise
        finally:
            row=dict(name=name,start_ns=start-self.origin,wall_ns=time.perf_counter_ns()-start,
                thread_cpu_ns=time.thread_time_ns()-thread_start if cpu else None,outcome=outcome)
            _PHASES.reset(token)
            if operation is not None:
                row['operation']=operation
                row['phase']=next((parent for parent in reversed(parents) if parent in
                    ('native.terminal_writer','native.context_prepare','native.route_acquire','fixture.attempt_poll')), 'other')
            with self.lock:self.rows.append(row)

    def sync(self,name,original):
        @wraps(original)
        def call(*args,**kwargs):
            with self.span(name):return original(*args,**kwargs)
        return call

    def asynchronous(self,name,original):
        @wraps(original)
        async def call(*args,**kwargs):
            with self.span(name,cpu=False):return await original(*args,**kwargs)
        return call

    def model_control(self,original):
        @wraps(original)
        async def call(owner,value,process,**kwargs):
            action=value.get('action') if isinstance(value,dict) else None
            label=action if action in ('reserve','dispatch','settle') else 'invalid'
            with self.span('native.model_control.'+label,cpu=False):
                return await original(owner,value,process,**kwargs)
        return call

    def terminal_initializer(self,original):
        @wraps(original)
        def call(owner,*args,**kwargs):
            result=original(owner,*args,**kwargs)
            owner.project=self.sync('native.terminal_projection',owner.project)
            return result
        return call

    def storage_v2_attempt(self,original):
        catalog=json.loads((Path(__file__).resolve().parents[1]/'contracts/storage_operations_v1.json').read_text())
        operations=frozenset(row['name'] for row in catalog['operations'])
        @wraps(original)
        def call(*args,**kwargs):
            operation=args[1] if len(args)>1 else kwargs.get('operation')
            label=operation if type(operation) is str and operation in operations else 'uncataloged'
            # Keep the stable aggregate label; only canonical operation names
            # and closed phase labels may accompany it. Never retain payloads.
            with self.span('storage.v2.attempt',operation=label):return original(*args,**kwargs)
        return call

    def install(self,stack,provider):
        from lib import observability
        from lib.conversation_sync import task_start
        from lib.tasks_pkg import native_prepared,native_terminal
        from lib.tasks_pkg.native_terminal import NativeTaskTerminalPolicy
        from lib.tasks_pkg.native_execution import NativeTaskRounds
        from lib.tasks_pkg.native_context import NativeContextPolicy
        from lib.tasks_pkg.native_cost_projection import NativeTaskCostPolicy
        from lib.storage.v2_client import StorageV2Client
        from lib.tasks_pkg.orchestrator import _run
        from lib.tasks_pkg.native_invocation import NativePreparedInvocation
        from lib.agent_core import native_artifact
        from lib.agent_core.native_process import NativeProcessClient
        from lib.agent_core import native_process
        from lib.agent_core.native_model_control import ModelControl
        import asyncio
        from lib.storage import client as storage_client
        from lib.model_routing import dispatch_adapter,routing
        from lib.tasks_pkg.manager import _persist
        from lib import turn_lifecycle
        from lib.llm_dispatch import ephemeral,factory,discovery,slot
        from lib.llm_dispatch.dispatcher import LLMDispatcher
        from lib.resource_control import adapters
        original=observability.measure_task_startup
        @contextmanager
        def startup(stage):
            with self.span('startup.'+stage),original(stage):yield
        stack.enter_context(patch.object(task_start,'measure_task_startup',startup))
        stack.enter_context(patch.object(observability,'measure_task_startup',startup))
        original_attempt=storage_client.StorageClient._attempt
        def attempt(owner,kind,operation,*args,**kwargs):
            # Operation identifiers are contract labels, never payload/owner/
            # credential values. These wrappers exist only in fixture workers.
            label='storage.'+str(kind)+'.'+str(operation)[:128]
            with self.span(label):return original_attempt(owner,kind,operation,*args,**kwargs)
        stack.enter_context(patch.object(storage_client.StorageClient,'_attempt',attempt))
        stack.enter_context(patch.object(NativeTaskTerminalPolicy,'__init__',
            self.terminal_initializer(NativeTaskTerminalPolicy.__init__)))
        stack.enter_context(patch.object(StorageV2Client,'_attempt',
            self.storage_v2_attempt(StorageV2Client._attempt)))
        for owner,name,label in [
            (NativeTaskTerminalPolicy,'_commit','native.terminal_commit'),
            (NativeTaskTerminalPolicy,'_projection_identity','native.terminal_identity'),
            (native_terminal,'_persist','native.terminal_writer'),
            (NativeTaskRounds,'_open','native.round_presentation'),
            (NativeContextPolicy,'_prepare','native.context_prepare'),
            (NativeTaskCostPolicy,'_row','native.cost_projection'),
            (slot,'_is_valid_thinking_format','routing.dialect_validation'),
            (ephemeral,'_normalise_base_url','routing.endpoint_admission'),
            (dispatch_adapter,'candidate_inference_metadata','routing.inference_metadata'),
            (dispatch_adapter,'compile_candidates','routing.primary_compile'),
            (dispatch_adapter,'compile_model_fallback_candidates','routing.fallback_compile'),
            (routing,'normalize_document','routing.normalize_authority'),
            (ephemeral,'_probe_reachable','routing.reachability'),
            (ephemeral,'_seed_caps_and_pricing','routing.seed'),
            (ephemeral,'_audit_slot','routing.audit'),
            (factory,'get_dispatcher','routing.dispatcher_acquire'),
            (LLMDispatcher,'initialize','routing.dispatcher_initialize'),
            (discovery,'should_bypass_proxy','routing.proxy_policy'),
            (adapters,'bind_ephemeral','routing.resource_binding'),
            (storage_client,'create_connection','storage.connect'),
            (storage_client,'send_frame','storage.send'),
            (storage_client,'recv_frame','storage.receive'),
            (_persist,'_upsert_task_row','task.birth_checkpoint'),
            (dispatch_adapter,'mint_routed_slot_group','routing.issue_group'),
            (turn_lifecycle,'mark_task_started','task.worker_start_commit'),
            (_run,'assemble_round_tools','root.tool_assembly'),
            (_run,'inject_context_and_emit_chips','root.context_injection'),
            (native_artifact,'seal_archive','native.archive_admission'),
            (native_artifact,'seal_executable','native.executable_admission'),
            (native_artifact,'_self_check','native.identity_check'),
            (native_prepared,'compile_native_application_route','native.route_compile'),
            (NativePreparedInvocation,'run','native.worker_bridge'),
            (provider.server.RequestHandlerClass,'do_POST','provider.request'),
        ]:
            stack.enter_context(patch.object(owner,name,self.sync(label,getattr(owner,name))))
        for owner,name,label in [
            (NativeTaskRounds,'reserve','native.round_reserve'),
            (NativeTaskRounds,'settle','native.round_settle'),
            (NativeContextPolicy,'prepare','native.context_worker'),
            (NativeTaskCostPolicy,'settle','native.cost_settle'),
            (asyncio,'create_subprocess_exec','native.process_spawn'),
            (native_process._ApprovalControl,'handle','native.approval_control'),
            (native_process._CheckpointControl,'handle','native.checkpoint_control'),
            (native_process._RoundControl,'handle','native.round_control'),
            (native_process._ProgressControl,'handle','native.progress_control'),
            (native_process._TextControl,'handle','native.text_control'),
            (native_process._TerminalControl,'handle','native.terminal_control'),
        ]:
            stack.enter_context(patch.object(owner,name,self.asynchronous(label,getattr(owner,name))))
        stack.enter_context(patch.object(ModelControl,'handle',self.model_control(ModelControl.handle)))
        stack.enter_context(patch.object(NativeProcessClient,'run',
            self.asynchronous('native.process_run',NativeProcessClient.run)))
        stack.enter_context(patch.object(native_prepared,'run_native_task',
            self.asynchronous('native.application_execution',native_prepared.run_native_task)))
        return self

    def record_operation_clock(self, start_ns, wall_ns):
        """Anchor raw spans to the actual clock, excluding fixture setup/readback."""
        with self.lock:
            self.rows.append(dict(name='fixture.operation_clock',start_ns=start_ns-self.origin,
                wall_ns=wall_ns,thread_cpu_ns=None,outcome='returned'))

    def snapshot(self):
        with self.lock:return sorted((dict(row) for row in self.rows),key=lambda row:row['start_ns'])
