"""Synchronous invocation port into the application's existing native loop.

NativePreparedInvocation binds explicit host/loop/principal/routing/resources.
run consumes the already prepared RootLoopRequest on a worker, blocks until the
native task and client scope join, and never creates a loop/executor/fallback.
"""
import asyncio
from concurrent.futures import Executor,ThreadPoolExecutor,CancelledError as FutureCancelledError
import json
from pathlib import Path
import threading
from contextlib import contextmanager
from contextvars import copy_context

from lib.agent_core.native_process import Budget,NativeConfigurationError
from lib.identity import PrincipalContext
from lib.model_routing.domain import NativeModelSelection
from lib.tasks_pkg.native_host import NativeHost
from lib.tasks_pkg.native_prepared import run_native_prepared_task
from lib.tasks_pkg.native_resources import compile_native_resources
from lib.tasks_pkg.native_terminal import project_native_terminal


class NativePreparedInvocation:
    __slots__=('host','loop','principal','budget','repository','selection','executor',
               'working_directory','pending_calls','pending_bytes','_profile',
               'pricing_resolver','checkpoint_policy','checkpoint_factory','checkpoint_preparation','goal_recovery','_dispatch_admission','route_policy','project_terminal','project_text','_transient','fee_transfer_target','flow_scheduler')

    def __init__(self,*,host,loop,principal,repository,selection,configuration,budget,
                 executor,working_directory,pending_calls,pending_bytes,resource_policy,
                 pricing_resolver=None,checkpoint_policy=None,checkpoint_factory=None,checkpoint_preparation=None,route_policy=None,
                 project_terminal=project_native_terminal,project_text=None,transient:bool=False,fee_transfer_target=None,flow_scheduler=None,goal_recovery=None):
        if (not isinstance(host,NativeHost) or not isinstance(loop,asyncio.AbstractEventLoop)
                or not isinstance(principal,PrincipalContext) or not isinstance(budget,Budget)
                or not isinstance(selection,NativeModelSelection) or not isinstance(executor,Executor)
                or not isinstance(working_directory,Path) or not working_directory.is_absolute()
                or type(pending_calls) is not int or pending_calls<=0
                or type(pending_bytes) is not int or pending_bytes<=0):
            raise NativeConfigurationError('native invocation requires explicit application owners')
        if (type(transient) is not bool or (transient and configuration.get('checkpoint_actor_account') is not True
                and any(value is not None for value in (checkpoint_policy,checkpoint_factory)))):
            raise NativeConfigurationError('native transient invocation cannot acquire durable ports')
        if flow_scheduler is not None:
            from lib.orchestration.native_flow import NativeFlowLibrary
            if not isinstance(flow_scheduler,NativeFlowLibrary):
                raise NativeConfigurationError('native Flow scheduler is not issued')
        self.flow_scheduler=flow_scheduler
        self._transient=transient
        self.fee_transfer_target=fee_transfer_target
        if fee_transfer_target is not None and (not transient or pricing_resolver is None):
            raise NativeConfigurationError('native child fee transfer requires priced transient composition')
        principal.require_owner(context='native invocation')
        if checkpoint_factory is not None and (not callable(checkpoint_factory) or checkpoint_policy is not None):
            raise NativeConfigurationError('native invocation has one checkpoint composition owner')
        if checkpoint_preparation is not None and (not callable(checkpoint_preparation)
                or checkpoint_factory is None):
            raise NativeConfigurationError('native checkpoint preparation requires its ordinary factory')
        profile=compile_native_resources(configuration,transport=budget,launch_policy=resource_policy)
        raw=json.dumps(profile,allow_nan=False)
        if len(raw.encode())>budget.configuration_bytes:
            raise NativeConfigurationError('native invocation profile exceeds host bound')
        self.host,self.loop,self.principal,self.budget=host,loop,principal,budget
        self.repository,self.selection,self.executor=repository,selection,executor
        self.working_directory=working_directory
        self.pending_calls,self.pending_bytes=pending_calls,pending_bytes
        self._profile=raw
        self.pricing_resolver,self.checkpoint_policy,self.route_policy=pricing_resolver,checkpoint_policy,route_policy
        self.checkpoint_factory=checkpoint_factory
        self.checkpoint_preparation=checkpoint_preparation
        if goal_recovery is not None:
            from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration
            if (not isinstance(goal_recovery, NativeGoalRecoveryConfiguration) or transient
                    or checkpoint_factory is None or profile.get('checkpoint_actor_account') is not True
                    or profile.get('checkpoint_shared_account') is not True):
                raise NativeConfigurationError('native Goal recovery requires explicit root actor checkpoint composition')
        self.goal_recovery=goal_recovery
        self._dispatch_admission=None
        self.project_terminal,self.project_text=project_terminal,project_text

    @contextmanager
    def admission_scope(self, task, admit):
        """Keep ordinary child admission after whole checkpoint preparation.

        The child owner still supplies the durable callback. Only a prepared
        native recipient defers it; no model/tool can enter before it succeeds.
        The blocking invocation joins the serving-loop work before scope exit.
        """
        if (task.get('_nativePreparedInvocation') is not self or not self._transient
                or not callable(admit) or self._dispatch_admission is not None):
            raise NativeConfigurationError('native child admission owner differs or is active')
        if self.checkpoint_preparation is None:
            admit()
            yield
            return
        entry = {'task':task, 'admit':admit, 'entered':False}
        self._dispatch_admission = entry
        try:
            yield
        finally:
            self._dispatch_admission = None

    async def _admit_prepared(self, request):
        entry = self._dispatch_admission
        if entry is None:
            return
        if entry['task'] is not request.task or entry['entered']:
            raise NativeConfigurationError('native child admission request differs or is repeated')
        entry['entered'] = True
        from lib.agent_core.native_process import _joined
        work = asyncio.get_running_loop().run_in_executor(self.executor, copy_context().run, entry['admit'])
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            await _joined(work)
            raise

    def resolve_child_model_selection(self, parent_task, model):
        """Resolve a declared logical child model against this owned repository.

        Role strings name exact catalog IDs. Wire IDs, display names, ambiguous
        creator identities and unauthorized deployments grant no selection.
        Candidate compilation performs no credential resolution or dispatch.
        """
        from lib.agent_core.execution_budget import ExecutionBudget
        from lib.model_routing.domain import ModelRoutingError
        from lib.model_routing.repository import OwnerBoundary
        from lib.model_routing.routing import RouteCandidateCompiler
        account=parent_task.get('_executionBudget') if isinstance(parent_task,dict) else None
        if (not isinstance(account,ExecutionBudget)
                or (not self._transient and self.pricing_resolver is None)
                or parent_task.get('_nativePreparedInvocation') is not self
                or parent_task.get('id')!=json.loads(self._profile)['epoch']
                or parent_task.get('status') not in {'pending','running'}
                or parent_task.get('_userId')!=self.principal.require_owner(context='native child selection')
                or PrincipalContext.from_payload(parent_task.get('_principalContext'))!=self.principal
                or not account.authorizes_model_owner(parent_task,parent_task['_userId'])
                or not isinstance(model,str) or not model):
            raise NativeConfigurationError('native child model selection requires its owned parent')
        try:
            account.validate_child_model_parent(parent_task,parent_task['_userId'])
        except ValueError as error:
            raise NativeConfigurationError('native child model parent is not live') from error
        checkpoint_owner=self
        if self.fee_transfer_target is not None:
            try:
                account.validate_child_invoice_receiver(self.fee_transfer_target,parent_task)
            except ValueError as error:
                raise NativeConfigurationError('native child model root receiver is not live') from error
            checkpoint_owner=self.fee_transfer_target.get('_nativePreparedInvocation')
            if not isinstance(checkpoint_owner,NativePreparedInvocation):
                raise NativeConfigurationError('native child model root execution owner is not issued')
        if ((checkpoint_owner.checkpoint_policy is not None or checkpoint_owner.checkpoint_factory is not None)
                and json.loads(checkpoint_owner._profile).get('checkpoint_child_routes') is not True):
            raise NativeConfigurationError('native child model routes require explicit checkpoint support')
        boundary=OwnerBoundary.create(parent_task['_userId'],self.principal.tenant_id)
        authority=self.repository.get(boundary)
        if authority.boundary!=boundary:
            raise NativeConfigurationError('native child model repository owner differs')
        compiler=RouteCandidateCompiler(authority.document)
        try:
            reference=compiler.resolve_model_id(model)
        except ModelRoutingError as error:
            raise NativeConfigurationError('native child model must name one exact catalog identity') from error
        selected=NativeModelSelection(reference,None,
            preferred_provider_id=self.selection.preferred_provider_id)
        if not compiler.compile(selected,policy=self.route_policy,max_candidates=1):
            raise NativeConfigurationError('native child model has no authorized deployment')
        return selected

    def bind_transient_child(self,child,*,parent_task_id,parent_task=None):
        """Borrow host/loop/resources; the child owns its newly issued route/account."""
        from lib.model_routing.domain import NativeModelSelection
        from lib.model_routing.dispatch_adapter import RoutedSlotGroup
        from lib.tasks_pkg.native_terminal import task_lifetime_matches
        group=child.get('_model_routing_group')
        selection=child.get('_model_routing_selection')
        expected_selection=self.selection
        if selection!=self.selection and isinstance(parent_task,dict):
            expected_selection=self.resolve_child_model_selection(parent_task,
                (child.get('config') or {}).get('model') or '')
        if ((not self._transient and self.pricing_resolver is None)
                or child.get('_parentTaskId')!=parent_task_id
                or json.loads(self._profile).get('epoch')!=parent_task_id
                or not task_lifetime_matches(child,True)
                or not isinstance(group,RoutedSlotGroup) or group.disposed
                or not isinstance(selection,NativeModelSelection) or selection!=expected_selection
                or child.get('_pinned_provider_id')!=group.pin_id
                or child.get('status')!='pending'
                or child.get('_userId')!=self.principal.require_owner(context='native child')
                or (group.route_identity or {}).get('owner_user_id')!=child['_userId']
                or ((group.route_identity or {}).get('tenant_id') or '')!=(self.principal.tenant_id or '')):
            raise NativeConfigurationError('native child requires its own issued transient authority')
        if self.pricing_resolver is not None:
            if (self.checkpoint_policy is not None or self.checkpoint_factory is not None) and (
                    json.loads(self._profile).get('checkpoint_tree_usage') is not True
                    and json.loads(self._profile).get('checkpoint_actor_account') is not True):
                raise NativeConfigurationError('native priced child journal transfer is not admitted')
            if not isinstance(parent_task,dict) or parent_task.get('id')!=parent_task_id:
                raise NativeConfigurationError('native priced child requires the parent fee receiver')
            from lib.agent_core.execution_budget import ExecutionBudget
            receiver = self.fee_transfer_target if self.fee_transfer_target is not None else parent_task
            account = parent_task.get('_executionBudget')
            if not isinstance(account, ExecutionBudget):
                raise NativeConfigurationError('native child fee receiver account is not admitted')
            try:
                account.validate_child_invoice_receiver(receiver, parent_task)
            except ValueError as error:
                raise NativeConfigurationError('native child fee receiver is not admitted') from error
        repository_runtime = parent_task.get('_repository_runtime') if isinstance(parent_task, dict) else None
        if repository_runtime is not None:
            from lib.repo_map.native_runtime import NativeRepositoryRuntime
            if (not isinstance(repository_runtime, NativeRepositoryRuntime)
                    or parent_task.get('id') != parent_task_id
                    or parent_task.get('_nativePreparedInvocation') is not self
                    or parent_task.get('_userId') != child['_userId']
                    or (child.get('_repository_runtime') is not None
                        and child['_repository_runtime'] is not repository_runtime)):
                raise NativeConfigurationError('native child repository requires its issued parent owner')
        configuration=json.loads(self._profile)
        configuration['epoch']=child['id']
        actor_checkpoint=configuration.get('checkpoint_actor_account') is True
        if actor_checkpoint and child.get('_invocationStorageFree') is not False:
            raise NativeConfigurationError('native actor checkpoint requires registered conversation lineage')
        removed=('checkpoint_tree_usage','checkpoint_child_routes') if actor_checkpoint else (
            'checkpoint_tree_usage','checkpoint_shared_account','checkpoint_frame_bytes','checkpoint_child_routes')
        for field in removed:
            configuration.pop(field, None)
        configuration['terminal_persistence']=dict(configuration['terminal_persistence'],
            operation_id='native-child:'+child['id'])
        child['_nativePreparedInvocation']=NativePreparedInvocation(host=self.host,loop=self.loop,
            principal=self.principal,repository=self.repository,selection=selection,configuration=configuration,
            budget=self.budget,executor=self.executor,working_directory=self.working_directory,
            pending_calls=self.pending_calls,pending_bytes=self.pending_bytes,
            resource_policy={'frame_maximum':min(self.budget.frame_bytes,self.budget.configuration_bytes)},
            route_policy=self.route_policy,pricing_resolver=self.pricing_resolver,transient=True,
            checkpoint_factory=self.checkpoint_factory if actor_checkpoint else None,
            flow_scheduler=self.flow_scheduler,
            fee_transfer_target=(self.fee_transfer_target if self.fee_transfer_target is not None
                else parent_task) if self.pricing_resolver is not None else None)
        if repository_runtime is not None:
            # Immutable code is borrowed; task/root scope and abort authority
            # are created independently on the child's first repository call.
            child['_repository_runtime'] = repository_runtime

    def finalize_flow_accounting(self, task):
        """Certify the existing child invoice before the graph terminal owner commits."""
        from lib.agent_core.execution_budget import ExecutionBudget
        account=task.get('_executionBudget')
        if (not isinstance(account,ExecutionBudget) or self._transient
                or task.get('_nativePreparedInvocation') is not self
                or task.get('id')!=json.loads(self._profile)['epoch']
                or not account.authorizes_model_owner(task,self.principal.require_owner(context='native Flow'))):
            raise NativeConfigurationError('native Flow fee owner differs from its shared account')
        task['_executionActualsComplete']=account.actuals_complete()
        from lib.billing.cost import native_task_cost
        native_task_cost(task,model=(task.get('config') or {}).get('model') or '')

    async def _run(self,request):
        if self.checkpoint_preparation is None:
            return await self._run_prepared(request)
        # Enter before factory construction: a failed factory must wake sibling
        # recipients as well. The caller owns this scope on the serving loop;
        # readiness never occupies a callback-executor worker.
        async with self.checkpoint_preparation(request) as ready:
            return await self._run_prepared(request,checkpoint_ready=ready)

    async def _run_prepared(self,request,*,checkpoint_ready=None):
        from lib.tasks_pkg.native_terminal import task_lifetime_matches
        if not task_lifetime_matches(request.task,self._transient):
            raise NativeConfigurationError('native invocation task lifetime differs')
        if 'model_control' in json.loads(self._profile):
            from lib.agent_core.execution_budget import _ensure_execution_budget
            # The common ROOT prelude has finished resolving profiles/resume
            # input. Reuse its canonical tree account, including inherited or
            # already hydrated consumption, before native model admission.
            _ensure_execution_budget(request.task)
        if (self.pricing_resolver is not None and request.task.get('_billingUserId')
                and request.task.get('_checkpointApiRounds')):
            from lib.billing.cost import native_task_cost
            request.task['_nativePricingRequired'] = True
            prefix = request.task['_checkpointApiRounds']
            if any(isinstance(row, dict) and not row.get('taskId') for row in prefix):
                from lib.conversation_sync.dispatch_contract import read_accepted_resume_lineage
                from copy import deepcopy
                lineage = read_accepted_resume_lineage(request.task)
                rows = request.task.get('apiRounds')
                if (not isinstance(rows, list) or len(rows) != len(prefix)
                        or rows != prefix):
                    raise ValueError('legacy invoice admission requires the exact pristine prefix')
                # Original quotes remain unchanged. The selected authority
                # identifies the executor that supplied this legacy prefix.
                detached = deepcopy(prefix)
                for row in detached:
                    if isinstance(row, dict) and not row.get('taskId'):
                        row['taskId'] = lineage.source_task_id
                candidate = dict(request.task, apiRounds=detached,
                                 _checkpointApiRounds=deepcopy(detached))
                native_task_cost(candidate, model=request.cfg.get('model') or '')
                request.task['apiRounds'] = detached
                request.task['_checkpointApiRounds'] = candidate['_checkpointApiRounds']
            # Fail before a provider effect if the restored invoice cannot be
            # attributed to a different executor. Keep native billing authority
            # on failure too, so the janitor cannot reprice inherited usage.
            if (any(row.get('billableTaskId') for row in prefix)
                    and self.checkpoint_policy is None and self.checkpoint_factory is None):
                from lib.agent_core.native_process import _joined
                def restore_invoice():
                    from lib.conversation_sync.dispatch_contract import read_accepted_resume_lineage
                    from lib.billing.native_settlement import restore_accepted_invoice_prefix
                    lineage = read_accepted_resume_lineage(request.task)
                    restore_accepted_invoice_prefix(request.task, lineage=lineage,
                        model=request.cfg.get('model') or '')
                work = asyncio.get_running_loop().run_in_executor(self.executor,
                    copy_context().run, restore_invoice)
                try:
                    await asyncio.shield(work)
                except asyncio.CancelledError as cancelled:
                    try:
                        await _joined(work)
                    except BaseException as error:
                        cancelled.add_note('native invoice prefix cleanup failed: '+type(error).__name__)
                    raise
            invoice_task = request.task
            if (prefix and any(row.get('billableTaskId') for row in prefix)
                    and json.loads(self._profile).get('checkpoint_tree_usage') is True
                    and (self.checkpoint_policy is not None or self.checkpoint_factory is not None)):
                # Validate original quotes now; the owned journal restores full
                # tree consumption before any subsequent model admission.
                invoice_task = dict(request.task, _executionBudget=None,
                    _nativePricingRequired=False, _nativeFrozenCostProjection=True)
            from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
            recover_same_executor = (isinstance(self.checkpoint_policy, NativeCheckpointPolicy)
                and self.checkpoint_policy.recover_abandoned
                and self.checkpoint_policy.task is request.task
                and self.checkpoint_policy.task_id == request.task['id'])
            if recover_same_executor:
                # Validate quote arithmetic without granting recovery. The exact
                # owned journal must prove writer death and match these rows
                # before restoring the account or admitting any model request.
                for row in prefix:
                    native_task_cost(dict(id=row.get('taskId', request.task['id']),
                        apiRounds=[row], _nativeFrozenCostProjection=True),
                        model=request.cfg.get('model') or '')
            else:
                native_task_cost(invoice_task, model=request.cfg.get('model') or '')
        checkpoint=self.checkpoint_policy
        configuration=json.loads(self._profile)
        if self.checkpoint_factory is not None:
            from lib.agent_core.native_process import _joined
            from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
            work=asyncio.get_running_loop().run_in_executor(self.executor,copy_context().run,
                self.checkpoint_factory,request,self.principal,self.executor,json.loads(self._profile))
            try:
                checkpoint=await asyncio.shield(work)
            except asyncio.CancelledError as cancelled:
                try:
                    checkpoint=await _joined(work)
                    if isinstance(checkpoint,NativeCheckpointPolicy):
                        await checkpoint.close()
                except BaseException as error:
                    cancelled.add_note('native checkpoint composition cleanup failed: '+type(error).__name__)
                raise
            if checkpoint is not None and not isinstance(checkpoint,NativeCheckpointPolicy):
                raise NativeConfigurationError('native application checkpoint factory must return selected policy')
            if checkpoint is not None:
                if (checkpoint.task is not request.task or checkpoint.owner != self.principal.require_owner(
                        context='native application checkpoint factory') or checkpoint.executor is not self.executor):
                    await checkpoint.close()
                    raise NativeConfigurationError('native application checkpoint factory ownership differs')
        def project_terminal(task, terminal_request):
            result = self.project_terminal(task, terminal_request)
            if result is not None:
                raise TypeError('native terminal projector must return None')
            if self.fee_transfer_target is not None:
                from lib.billing.cost import transfer_native_child_fees
                transfer_native_child_fees(self.fee_transfer_target, task)

        def complete(task):
            from lib.tasks_pkg.orchestrator._post_commit import finish_committed_task
            finish_committed_task(task,messages=task.get('messages') or [],cfg=request.cfg,
                project_enabled=request.project_enabled,project_path=request.project_path)
        try:
            if checkpoint_ready is not None:
                if checkpoint is None:
                    raise NativeConfigurationError('native checkpoint preparation returned no policy')
                await checkpoint_ready(checkpoint)
            if checkpoint is not None and self.checkpoint_factory is not None:
                # Ordinary Continue uses its factory's epoch. A whole Goal
                # handoff binds the claimed source epoch before native entry.
                configuration['epoch']=checkpoint.epoch
            await self._admit_prepared(request)
            async with self.host.client(self.principal,budget=self.budget) as client:
                return await run_native_prepared_task(client,self.repository,self.selection,configuration,
                    request=request,executor=self.executor,working_directory=self.working_directory,
                    pending_calls=self.pending_calls,pending_bytes=self.pending_bytes,
                    pricing_resolver=self.pricing_resolver,checkpoint_policy=checkpoint,
                    route_policy=self.route_policy,project_terminal=project_terminal,project_text=self.project_text,
                    after_terminal=complete,transient=self._transient)
        finally:
            if self.checkpoint_factory is not None and checkpoint is not None:
                import sys
                primary=sys.exception()
                try:await checkpoint.close()
                except BaseException as error:
                    if primary is None:raise
                    primary.add_note('native application checkpoint cleanup failed: '+type(error).__name__)

    def require_worker(self):
        """Refuse a synchronous bridge that would occupy its own callback pool."""
        try:asyncio.get_running_loop()
        except RuntimeError:pass
        else:raise NativeConfigurationError('native synchronous invocation requires a worker thread')
        # A bounded stdlib pool cannot service callbacks while all its workers
        # block on this bridge. Demand executors have no such worker-count gate.
        if isinstance(self.executor,ThreadPoolExecutor):
            # submit starts a worker before publishing it in _threads; the
            # stdlib publication lock closes that first-job membership race.
            with self.executor._shutdown_lock:
                if threading.current_thread() in self.executor._threads:
                    raise NativeConfigurationError('native callbacks require a separate bounded worker pool')
        if not self.loop.is_running() or self.loop.is_closed():
            raise NativeConfigurationError('native serving loop is unavailable')

    def run(self,request):
        from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest
        if (not isinstance(request,RootLoopRequest) or type(request.task.get('_userId')) is not int
                or request.task['_userId']!=self.principal.require_owner(context='native invocation')
                or request.task.get('status') not in {'pending','running'}):
            raise NativeConfigurationError('native invocation preparation owner differs')
        self.require_worker()
        work=self._run(request)
        try:future=asyncio.run_coroutine_threadsafe(work,self.loop)
        except BaseException:
            work.close()
            raise
        try:return future.result()
        except FutureCancelledError:
            # This Future is private and never cancelled by the worker. Its
            # cancelled state therefore follows completed coroutine teardown.
            raise asyncio.CancelledError from None
        except BaseException:
            if not future.done():
                request.task['abort_event'].set()
                # Stop is a task signal; cancelling the concurrent Future would
                # report completion before the native coroutine finishes joining.
                while not future.done():
                    try:future.result()
                    except BaseException:
                        if future.done():break
            raise
