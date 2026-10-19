"""Prepared application invocation into the owned Rust task runtime.

Entry run_native_prepared_task: consumes the existing RootLoopRequest, issued
selection and explicit native resource/recovery profile. Creates one gateway
and one route on the supplied executor, then delegates to run_native_task.
No preparation replay, alternate loop, client/pool/store lifecycle or default
production selection is introduced. Cancellation joins route acquisition.
"""
import asyncio
from contextvars import copy_context
from concurrent.futures import Executor
from collections.abc import Mapping
from pathlib import Path

from lib.agent_core.native_process import _joined
from lib.tasks_pkg.native_execution import compile_native_application_route,run_native_task
from lib.tasks_pkg.native_gateway import NativeGateway
from lib.tasks_pkg.native_terminal import project_native_terminal, task_lifetime_matches
from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS

from lib.log import get_logger

logger = get_logger(__name__)


async def run_native_prepared_task(client,repository,selection,configuration,*,
        request,executor:Executor,working_directory:Path,pending_calls:int,pending_bytes:int,
        pricing_resolver=None,checkpoint_policy=None,route_policy=None,
        project_terminal=project_native_terminal,project_text=None,resource_policy=None,after_terminal=None,
        transient: bool=False):
    from lib.tasks_pkg.orchestrator._root_agent_loop import RootLoopRequest
    owner=client.principal.require_owner(context='native prepared invocation')
    if (not isinstance(request,RootLoopRequest) or not isinstance(executor,Executor)
            or not isinstance(configuration,Mapping)):
        raise ValueError('native prepared invocation requires owned preparation and resources')
    task=request.task
    from lib.tasks_pkg.native_checkpoint import checkpoint_lifetime_matches
    model=getattr(request.state,'model',None)
    if (not isinstance(task,dict) or not task_lifetime_matches(task, transient)
            or not checkpoint_lifetime_matches(task,checkpoint_policy,configuration,transient)
            or type(task.get('_userId')) is not int or task['_userId']!=owner
            or not isinstance(task.get('id'),str) or not task['id']
            or task.get('status') not in {'pending','running'}
            or not isinstance(model,str) or not model
            or not isinstance(request.cfg,dict) or request.cfg!=(task.get('config') or {})
            or not isinstance(request.messages,list)
            or any(not isinstance(row,dict) for row in request.messages)
            or (request.tool_list is not None and not isinstance(request.tool_list,list))):
        raise ValueError('native prepared invocation task or context differs')
    route_options={'policy':route_policy}
    if '_model_routing_group' in task:
        group=task['_model_routing_group']
        from lib.model_routing.dispatch_adapter import RoutedSlotGroup
        if (not isinstance(group,RoutedSlotGroup) or group.disposed
                or task.get('_pinned_provider_id') != group.pin_id):
            raise ValueError('native prepared invocation issued route differs')
        route_options['issued_group']=group
    reserved={'version','type','owner_user_id','task_id','messages','tools','wire_tools','wire_tool_preparation'} | ROUTE_OVERRIDE_FIELDS
    if reserved & configuration.keys():
        raise ValueError('native prepared resource profile overrides preparation or routing')
    if resource_policy is not None:
        from lib.tasks_pkg.native_resources import compile_native_resources
        configuration=compile_native_resources(configuration,transport=client.budget,
            launch_policy=resource_policy)
    configuration=dict(configuration,version=client.protocol_version,type='configure',
        owner_user_id=owner,task_id=task['id'],messages=request.messages)
    configuration.setdefault('text_progress',True)
    configuration.setdefault('total_tool_calls',None)
    configuration.setdefault('retain_execution_history',True)
    from lib.tasks_pkg.native_tool_projection import prepared_execution_tools, NativeToolProjection
    execution_tools=prepared_execution_tools(task,request.tool_list or [])
    projection=NativeToolProjection(request,executor)
    gateway=NativeGateway(principal=client.principal,task=task,executor=executor,
        configuration=request.cfg,tool_list=execution_tools,
        project_path=request.project_path,project_enabled=request.project_enabled,
        model=model,round_source=None,decision_policy=None,pending_calls=pending_calls,
        pending_bytes=pending_bytes,multimodal=configuration.get('tool_feedback','text')=='multimodal',
        search_results=request.all_search_results_text)
    route=None
    try:
        worker=executor.submit(copy_context().run,compile_native_application_route,
            repository,client.principal,selection,**route_options)
        future=asyncio.wrap_future(worker)
        try:
            route=await asyncio.shield(future)
        except asyncio.CancelledError as cancelled:
            worker.cancel()  # Withdraw only unstarted acquisition; otherwise join it.
            try:
                await _joined(future)
            except BaseException as diagnostic_error:
                logger.debug('run_native_prepared_task failure handled: %s', type(diagnostic_error).__name__)
                pass  # Joined failed/withdrawn acquisition; cancellation wins.
            if not future.cancelled() and future.exception() is None:
                try:future.result().close()
                except BaseException as error:
                    cancelled.add_note('native route cleanup failed: '+type(error).__name__)
            raise
        def project(task,candidate):
            result=project_terminal(task,candidate)
            if result is not None:raise TypeError('native terminal projector must return None')
            if candidate.event.get('status')=='done':
                from lib.tasks_pkg.orchestrator._finalize import _maybe_append_sources_footer
                try:_maybe_append_sources_footer(task,request.all_search_results_text)
                except Exception as error:
                    from lib.log import get_logger
                    get_logger(__name__).warning('native sources-footer backstop failed: %s',error)
        return await run_native_task(client,route,configuration,task=task,executor=executor,
            callbacks={},working_directory=working_directory,gateway=gateway,
            pricing_resolver=pricing_resolver,checkpoint_policy=checkpoint_policy,
            project_terminal=project,project_text=project_text,after_terminal=after_terminal,
            prepare_wire_tools=projection,wire_context_tools=request.tool_list or [],transient=transient)
    finally:
        import sys
        primary=sys.exception()
        failure=None
        for label,resource in [('route',route),('gateway',gateway)]:
            if resource is None:continue
            try:resource.close()
            except BaseException as error:
                if primary is not None:
                    primary.add_note('native prepared '+label+' cleanup failed: '+type(error).__name__)
                elif failure is None:failure=error
                else:failure.add_note('native prepared '+label+' cleanup failed: '+type(error).__name__)
        if failure is not None:raise failure
