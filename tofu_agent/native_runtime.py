"""Trusted storage-free Rust execution composition, independent of application lifecycle.

NativeAgentConfiguration borrows an admitted image host, running event loop,
callback executor and issued launch policy. bind_task consumes the SDK's existing
owned routing issuance. The caller joins its runtime before closing borrowed
resources. The optional owned_native_agent_configuration entry admits the CLI archive and
joins its launch-sized callback owner. No storage, model registry, fallback loop
or default switch.
"""
import asyncio
from contextlib import asynccontextmanager
from collections.abc import Mapping
from concurrent.futures import Executor
import json
from pathlib import Path


class NativeAgentConfiguration:
    __slots__=('host','loop','executor','working_directory','pending_calls','pending_bytes','_profile','_frame_maximum')

    def __init__(self,*,host,loop,executor,configuration,working_directory,resource_policy,
                 pending_calls,pending_bytes):
        from lib.agent_core.native_process import NativeConfigurationError
        from lib.tasks_pkg.native_host import NativeHost
        from lib.tasks_pkg.native_resources import compile_native_resources
        from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
        reserved={'version','type','owner_user_id','task_id','messages','tools','wire_tools',
                  'wire_tool_preparation','epoch'} | ROUTE_OVERRIDE_FIELDS
        terminal=configuration.get('terminal_persistence') if isinstance(configuration,Mapping) else None
        if (not isinstance(host,NativeHost) or not isinstance(loop,asyncio.AbstractEventLoop)
                or not loop.is_running() or loop.is_closed() or not isinstance(executor,Executor)
                or not isinstance(working_directory,Path) or not working_directory.is_absolute()
                or not isinstance(configuration,Mapping) or reserved & configuration.keys()
                or any(not isinstance(key,str) or key.startswith('checkpoint_') for key in configuration)
                or not isinstance(terminal,Mapping) or 'operation_id' in terminal
                or type(pending_calls) is not int or pending_calls<=0
                or type(pending_bytes) is not int or pending_bytes<=0
                or not isinstance(configuration.get('model_control'),Mapping)
                or configuration['model_control'].get('pricing',False) is not False
                or not isinstance(configuration.get('model_counter'),Mapping)
                or not isinstance(configuration.get('policy'),Mapping)):
            raise NativeConfigurationError('native SDK requires explicit live storage-free owners')
        profile=compile_native_resources(configuration,transport=host.image.budget,launch_policy=resource_policy)
        self._profile=json.dumps(profile,allow_nan=False)
        if len(self._profile.encode())>host.image.budget.configuration_bytes:
            raise NativeConfigurationError('native SDK profile exceeds host bound')
        self._frame_maximum=min(resource_policy['frame_maximum'],host.image.budget.configuration_bytes,
            host.image.budget.frame_bytes)
        self.host,self.loop,self.executor=host,loop,executor
        self.working_directory=working_directory
        self.pending_calls,self.pending_bytes=pending_calls,pending_bytes

    def assert_sync_lane(self):
        from tofu_agent.models import AgentConfigurationError
        try:loop=asyncio.get_running_loop()
        except RuntimeError:return
        if loop is self.loop:
            raise AgentConfigurationError('Use async SDK operations on the native serving loop')

    def bind_task(self,task,*,principal,repository,selection,route_group):
        from lib.agent_core.native_process import NativeConfigurationError
        from lib.tasks_pkg.native_invocation import NativePreparedInvocation
        from lib.tasks_pkg.native_terminal import task_lifetime_matches
        from lib.model_routing.dispatch_adapter import RoutedSlotGroup
        if (not task_lifetime_matches(task,True) or task.get('status')!='pending'
                or task.get('_userId')!=principal.require_owner(context='native SDK binding')
                or '_nativePreparedInvocation' in task
                or not isinstance(route_group,RoutedSlotGroup) or route_group.disposed
                or task.get('_pinned_provider_id')!=route_group.pin_id
                or (route_group.route_identity or {}).get('owner_user_id')!=task['_userId']
                or ((route_group.route_identity or {}).get('tenant_id') or '')!=(principal.tenant_id or '')):
            raise NativeConfigurationError('native SDK task lacks its live issued owners')
        profile=json.loads(self._profile)
        profile['epoch']=task['id']
        profile['terminal_persistence']=dict(profile['terminal_persistence'],operation_id='native-sdk:'+task['id'])
        profile['terminal_persistence'].setdefault('frame_bytes',self.host.image.budget.frame_bytes)
        profile.setdefault('approval_control',dict(receipts=self.host.image.budget.frame_bytes,
            identity_bytes=self.host.image.budget.frame_bytes))
        task['_model_routing_group']=route_group
        task['_model_routing_selection']=selection
        task['_nativePreparedInvocation']=NativePreparedInvocation(host=self.host,loop=self.loop,
            principal=principal,repository=repository,selection=selection,configuration=profile,
            budget=self.host.image.budget,executor=self.executor,working_directory=self.working_directory,
            pending_calls=self.pending_calls,pending_bytes=self.pending_bytes,
            resource_policy={'frame_maximum':self._frame_maximum},transient=True)


@asynccontextmanager
async def owned_native_agent_configuration(*,archive,configuration,working_directory,environ,shutdown_seconds):
    """CLI lifespan: admit one image, use launch-sized callbacks, and join close."""
    import hashlib
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.tasks_pkg.native_resources import compile_native_transport_budget
    from lib.resource_control.runtime import launch_policy
    from lib.tasks_pkg.native_host import NativeHost
    from lib.version import __version__
    from tofu_workers.executor import DemandExecutor
    policy=launch_policy(environ)
    frame_bytes=policy['frame_maximum']
    budget=compile_native_transport_budget(launch_policy=policy,shutdown_seconds=shutdown_seconds)
    archive=Path(archive).resolve()
    def digest():
        with archive.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()
    try:digest_value=await asyncio.to_thread(digest)
    except OSError as exc:
        from tofu_agent.models import AgentConfigurationError
        raise AgentConfigurationError('Cannot read native runtime archive') from exc
    image=NativeArchiveImage(archive=archive,sha256=digest_value,application_version=__version__,budget=budget)
    host=NativeHost(image)
    callbacks=DemandExecutor(max_workers=policy['root_maximum'],queue_capacity=policy['root_maximum'],
        max_abandoned_workers=1,thread_name_prefix='tofu-native-sdk-callback',metric_pool='native-sdk-callback')
    try:
        native=NativeAgentConfiguration(host=host,loop=asyncio.get_running_loop(),executor=callbacks,
            configuration=configuration,working_directory=Path(working_directory).resolve(),resource_policy=policy,
            pending_calls=policy['root_maximum'],pending_bytes=frame_bytes)
        descriptor=await image.duplicate(budget.binary_bytes)
        descriptor.close()
        yield native
    finally:
        # Unknown host cleanup fences callback/loop retirement; never abandon
        # physical callbacks while the native client can still observe them.
        await host.close()
        await asyncio.to_thread(callbacks.shutdown,wait=True,cancel_futures=True)
