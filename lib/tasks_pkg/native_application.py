"""Application composition for native execution of durable ROOT and Flow tasks.

NativeRootConfiguration owns frozen policy inputs and owner-aware repository,
pricing and ROOT/Flow checkpoint factories. The binding borrows the admitted app host,
serving loop, callback executor and issued launch policy; it creates no workers,
stores, probes, task registry or fallback model loop. Task startup calls the
binding inside its existing pre-dispatch rollback boundary.
"""
from collections.abc import Mapping
from concurrent.futures import Executor
import asyncio
import json
import inspect
from pathlib import Path

from lib.agent_core.native_process import NativeConfigurationError, _joined
from lib.identity import PrincipalContext
from lib.model_routing.domain import NativeModelSelection
from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
from lib.tasks_pkg.native_invocation import NativePreparedInvocation


def _repository(principal):
    principal.require_owner(context='native application repository')
    from lib.model_routing import ModelRoutingRepository
    return ModelRoutingRepository()


def _pricing(principal, task):
    from lib.billing.pricing import get_default_margin
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    return NativeRoutePricing(principal=principal, margin=get_default_margin())


def _freeze_profile(configuration):
    """Validate each trusted execution profile through the same authority fence."""
    reserved={'version','type','owner_user_id','task_id','messages','tools','wire_tools',
              'wire_tool_preparation','epoch'} | ROUTE_OVERRIDE_FIELDS
    terminal=configuration.get('terminal_persistence') if isinstance(configuration,Mapping) else None
    if (not isinstance(configuration,Mapping) or reserved & configuration.keys()
            or not isinstance(terminal,Mapping) or 'operation_id' in terminal
            or not isinstance(configuration.get('model_control'),Mapping)
            or configuration['model_control'].get('pricing') is not True
            or not isinstance(configuration.get('model_counter'),Mapping)
            or not isinstance(configuration.get('policy'),Mapping)):
        raise NativeConfigurationError('native application requires owned policy, directory and factories')
    return json.dumps(dict(configuration),allow_nan=False)


class NativeRootConfiguration:
    """Trusted frozen ROOT/Flow profiles, independent of requests and principals.

    An omitted Flow profile/factory preserves the existing shared policy. Explicit
    Flow overrides let actor recovery coexist with ordinary ROOT checkpointing;
    both execution kinds retain the same application and resource owners.
    """
    __slots__=('_profile','_flow_profile','working_directory','repository_factory','pricing_factory','checkpoint_factory','flow_checkpoint_factory','worker_reuse','flow_scheduler','image_preparation','flow_archive','native_repository','goal_recovery')

    def __init__(self, *, configuration, working_directory,
                 repository_factory=_repository, pricing_factory=_pricing, checkpoint_factory=None,worker_reuse=False,flow_scheduler=None,image_preparation=False,flow_archive=False,native_repository=None,goal_recovery=None,
                 flow_configuration=None,flow_checkpoint_factory=None):
        if native_repository is not None:
            from tofu_agent.native_repository import NativeRepositoryConfiguration
            if not isinstance(native_repository, NativeRepositoryConfiguration):
                raise NativeConfigurationError('native repository requires trusted NativeRepositoryConfiguration')
        self.native_repository = native_repository
        if type(flow_archive) is not bool or (flow_archive and flow_scheduler is not None):
            raise NativeConfigurationError('native Flow archive selection must be explicit and have one owner')
        self.flow_archive=flow_archive
        if type(image_preparation) is not bool:raise NativeConfigurationError('native image preparation policy must be explicit boolean')
        self.image_preparation=image_preparation
        if type(worker_reuse) is not bool:raise NativeConfigurationError('native worker reuse policy must be explicit boolean')
        if flow_scheduler is not None:
            from lib.orchestration.native_flow import NativeFlowLibrary
            if not isinstance(flow_scheduler,NativeFlowLibrary):
                raise NativeConfigurationError('native Flow scheduler requires a pinned library')
        self.flow_scheduler=flow_scheduler
        self.worker_reuse=worker_reuse
        self._profile=_freeze_profile(configuration)
        self._flow_profile=(self._profile if flow_configuration is None
                            else _freeze_profile(flow_configuration))
        if (not isinstance(working_directory,Path) or not working_directory.is_absolute()
                or not callable(repository_factory) or not callable(pricing_factory)
                or (checkpoint_factory is not None and not callable(checkpoint_factory))
                or (flow_checkpoint_factory is not None and not callable(flow_checkpoint_factory))
                or any(inspect.iscoroutinefunction(factory) for factory in
                    (repository_factory,pricing_factory,checkpoint_factory,flow_checkpoint_factory))):
            raise NativeConfigurationError('native application requires owned policy, directory and factories')
        self.working_directory=working_directory
        self.repository_factory,self.pricing_factory=repository_factory,pricing_factory
        self.checkpoint_factory=checkpoint_factory
        self.flow_checkpoint_factory=(checkpoint_factory if flow_checkpoint_factory is None
                                      else flow_checkpoint_factory)
        if goal_recovery is not None:
            from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration
            flow_profile=json.loads(self._flow_profile)
            if (not isinstance(goal_recovery, NativeGoalRecoveryConfiguration) or self.flow_checkpoint_factory is None
                    or flow_profile.get('checkpoint_actor_account') is not True
                    or flow_profile.get('checkpoint_shared_account') is not True):
                raise NativeConfigurationError('native Goal recovery requires trusted actor checkpoint policy')
        self.goal_recovery=goal_recovery


class NativeApplicationBinding:
    __slots__=('app','configuration','host','closed','repository_runtime')

    def __init__(self,app,configuration):
        self.app,self.configuration=app,configuration
        self.host=app.extensions['tofu_native_host']
        self.closed=False
        self.repository_runtime = (configuration.native_repository.create_runtime()
                                   if configuration.native_repository is not None else None)

    async def close(self):
        # Detach admission first. The repository owner cancels/joins its scans
        # off the serving loop; shielding joins retirement even if shutdown is
        # cancelled. NativeHost subsequently joins model clients and its image.
        self.closed=True
        self.app=None
        if self.repository_runtime is not None:
            async def settle() -> BaseException | None:
                try:
                    await asyncio.to_thread(self.repository_runtime.close)
                except BaseException as failure:
                    # Task callbacks special-case control exceptions. Return
                    # them to the lifespan owner, where the remaining owners
                    # still receive cleanup before the failure is raised.
                    return failure
                return None

            operation = settle()
            try:
                cleanup = asyncio.create_task(operation)
            except BaseException:
                operation.close()
                raise
            failure = await _joined(cleanup)
            if failure is not None:
                raise failure

    def bind_task(self,task,*,flow=False):
        from lib.model_routing.dispatch_adapter import RoutedSlotGroup
        if type(flow) is not bool:raise NativeConfigurationError('native task execution owner must be explicit')
        lifecycle=self.app.extensions.get('tofu_lifecycle',{}) if self.app is not None else {}
        serving=self.app.extensions.get('tofu_serving_loop_lifecycle',{}) if self.app is not None else {}
        runtime=serving.get('runtime')
        group=task.get('_model_routing_group')
        selection=task.get('_model_routing_selection')
        raw_principal=task.get('_principalContext')
        if (self.closed or lifecycle.get('status') not in {'starting','serving'}
                or serving.get('owns_task_workers') is False
                or runtime is None or runtime.shutdown_requested.is_set()
                or runtime.loop is not lifecycle.get('loop')
                or not isinstance(runtime.sync_executor,Executor)
                or runtime.resource_controller is None
                or task.get('status')!='pending' or task.get('_transientRuntime')
                or '_nativePreparedInvocation' in task
                or not isinstance(raw_principal,dict)
                or not isinstance(selection,NativeModelSelection)
                or not isinstance(group,RoutedSlotGroup) or group.disposed
                or task.get('_pinned_provider_id')!=group.pin_id):
            raise NativeConfigurationError('native application task lacks live issued owners')
        principal=PrincipalContext.from_payload(raw_principal)
        if type(task.get('_userId')) is not int or task['_userId']!=principal.require_owner(context='native application task'):
            raise NativeConfigurationError('native application task principal differs')
        if (not isinstance(group.route_identity,Mapping)
                or group.route_identity.get('owner_user_id')!=task['_userId']
                or (group.route_identity.get('tenant_id') or '')!=(principal.tenant_id or '')):
            raise NativeConfigurationError('native application issued route principal differs')
        if task.get('_repository_runtime') is not None and task.get('_repository_runtime') is not self.repository_runtime:
            raise NativeConfigurationError('native application repository owner differs')
        if self.configuration.worker_reuse:self.host.enable_worker_reuse(runtime.resource_controller.launch_policy)
        profile=json.loads(self.configuration._flow_profile if flow else self.configuration._profile)
        checkpoint_factory=(self.configuration.flow_checkpoint_factory if flow
                            else self.configuration.checkpoint_factory)
        budget=self.host.image.budget
        profile['epoch']=task['id']
        profile['terminal_persistence']=dict(profile['terminal_persistence'],
            operation_id='native-root:'+task['id'])
        profile['terminal_persistence'].setdefault('frame_bytes',budget.frame_bytes)
        profile.setdefault('approval_control',dict(receipts=budget.frame_bytes,identity_bytes=budget.frame_bytes))
        repository=self.configuration.repository_factory(principal)
        pricing=self.configuration.pricing_factory(principal,task)
        actor_checkpoint=profile.get('checkpoint_actor_account') is True
        if actor_checkpoint and (not flow or checkpoint_factory is None):
            raise NativeConfigurationError('native actor checkpoints require explicit Flow checkpoint composition')
        if flow:
            removed=('checkpoint_tree_usage','checkpoint_child_routes') if actor_checkpoint else (
                'checkpoint_tree_usage','checkpoint_shared_account','checkpoint_frame_bytes','checkpoint_child_routes')
            for field in removed:
                profile.pop(field,None)
        from lib.tasks_pkg.native_pricing import NativeRoutePricing
        if (not all(callable(getattr(repository,name,None)) for name in ('get','resolve_secret'))
                or not isinstance(pricing,NativeRoutePricing)
                or pricing.principal.require_owner(context='native application pricing')!=task['_userId']
                or pricing.principal.tenant_id!=principal.tenant_id):
            raise NativeConfigurationError('native application repository or price owner differs')
        task['_nativePreparedInvocation']=NativePreparedInvocation(host=self.host,loop=runtime.loop,
            principal=principal,repository=repository,selection=selection,
            configuration=profile,budget=budget,executor=runtime.sync_executor,
            working_directory=self.configuration.working_directory,
            pending_calls=budget.frame_bytes,pending_bytes=budget.frame_bytes,
            resource_policy=runtime.resource_controller.launch_policy,
            pricing_resolver=pricing,
            checkpoint_factory=None if flow and not actor_checkpoint else checkpoint_factory,
            goal_recovery=self.configuration.goal_recovery if flow else None,
            flow_scheduler=(self.host.flow_scheduler() if flow and self.configuration.flow_archive
                            else self.configuration.flow_scheduler))
        if self.configuration.image_preparation and not flow:
            cancelled=task.get('abort_event')
            if not task.get('aborted') and (cancelled is None or not cancelled.is_set()):
                self.host.prepare_image(runtime.loop)
        if flow:
            from lib.agent_core.execution_budget import _ensure_execution_budget
            _ensure_execution_budget(task)
            task.update(_nativePricingRequired=True,_nativeFrozenCostProjection=True)
        if self.repository_runtime is not None:
            task['_repository_runtime'] = self.repository_runtime


def register_native_application(app,*,configuration):
    from lib.app.app_lifecycle import add_shutdown_handler
    if not isinstance(configuration,NativeRootConfiguration) or 'tofu_native_host' not in app.extensions:
        raise NativeConfigurationError('native application requires admitted host and configuration')
    previous=app.extensions.get('tofu_native_application')
    if previous is not None:
        if previous.configuration is not configuration:
            raise NativeConfigurationError('native application configuration differs')
        return previous
    if app.extensions['tofu_lifecycle']['status']!='created':
        raise NativeConfigurationError('native application must register before startup')
    binding=NativeApplicationBinding(app,configuration)
    if not add_shutdown_handler(app,binding.close,name='tofu.native-application'):
        raise NativeConfigurationError('native application shutdown owner already registered')
    app.extensions['tofu_native_application']=binding
    return binding


def bind_application_native_task(task,*,flow=False):
    """Resolve the current application port; CLI/transient composition stays explicit."""
    from quart import current_app,has_app_context
    if not has_app_context():
        return
    binding=current_app.extensions.get('tofu_native_application')
    if binding is not None:
        if not isinstance(binding,NativeApplicationBinding):
            raise NativeConfigurationError('native application port is not issued')
        binding.bind_task(task,flow=flow)
