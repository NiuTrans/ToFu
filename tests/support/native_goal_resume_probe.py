"""Compose real native recipient takeover with ordinary Flow completion.

This fixture wires explicit existing ports, using a caller-owned accepted Goal,
selected storage, loopback provider and installed native image. Production mode
enters the ordinary Goal runner; explicit mode exposes crash-injection boundaries.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import uuid


async def resume(*, task, seed, inputs, claim, storage, runs, principal, directory, endpoint, archive, flow_library,
                 production=False, wallet_id=None, checkpoint_binding='registered-flow-actor'):
    from lib.agent_core.native_image import NativeArchiveImage
    from lib.agent_core.native_process import Budget
    from lib.billing import reserve
    from lib.goal_runs.execution_journal import create_goal_execution_journal
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group, dispose_routed_slot_group
    from lib.model_routing.domain import parse_native_model_selection
    from lib.model_routing.repository import OwnerBoundary
    from lib.orchestration.agent_runner import OrchestrationAgentRunnerConfig
    from lib.orchestration.native_flow import NativeFlowLibrary
    from lib.orchestration.prepared_recipients import prepare_flow_recipients
    from lib.orchestration.runtime_service import execute_flow
    from lib.resource_control.runtime import launch_policy
    from lib.swarm.routing import tool_schema_name
    from lib.tasks_pkg.invocation_registration import owned_child_record
    from lib.tasks_pkg.native_checkpoint import NativeCheckpointPolicy
    from lib.tasks_pkg.native_file_recovery import NativeFileToolRecovery
    from lib.tasks_pkg.native_goal_handoff import NativeGoalHandoff
    from lib.tasks_pkg.native_host import NativeHost
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    from scripts.build_native_runtime import verify
    from tests.model_routing.test_native_route import Repository
    from tests.tasks_pkg.test_native_application import profile

    owner = principal.require_owner(context='native Goal resume fixture')
    image_path = Path(archive)
    manifest = verify(image_path)
    transport = Budget(1,16<<20,1<<20,1<<20,5)
    image = NativeArchiveImage(archive=image_path,sha256=hashlib.sha256(image_path.read_bytes()).hexdigest(),
        application_version=manifest['build']['application_version'],budget=transport)
    host = NativeHost(image)
    library_path = Path(flow_library)
    scheduler = NativeFlowLibrary(path=library_path,sha256=hashlib.sha256(library_path.read_bytes()).hexdigest(),
        maximum_bytes=64<<20)
    repo = Repository()
    repo.boundary = OwnerBoundary.create(owner)
    repo.document['connections'][1]['enabled'] = False
    repo.document['connections'][0]['base_url'] = endpoint
    archived = seed.metadata['invocation_policy']['config']
    selected = parse_native_model_selection({'model':archived['modelRef'],
        'routing':archived.get('routing') or {}})
    group = mint_routed_slot_group(repo,repo.boundary,selected,owner_tag='task:'+task['id'])
    wallet_id = claim.request['invoice']['wallet_user_id'] if claim is not None else wallet_id
    reserve(wallet_id,100,ref_id=task['id'])
    task.update(_principalContext=principal.to_payload(),_model_routing_group=group,
        _model_routing_selection=selected,_requestedModelRef=selected.model.public_dict(),
        _pinned_provider_id=group.pin_id,_billingUserId=wallet_id,_billing_reservation_micro=100,
        _nativePricingRequired=True,_nativeFrozenCostProjection=True)
    settings = profile()
    if production:
        from lib.tasks_pkg.native_production_profile import compile_native_application_profile
        settings=compile_native_application_profile(transport=transport,launch_policy=launch_policy({}))
    settings.update(epoch=task['id'],checkpoint_actor_account=True,
        checkpoint_shared_account=True,checkpoint_frame_bytes=1<<20,
        approval_control=dict(receipts=128,identity_bytes=4096))
    for field in ('input_bytes','text_bytes','tool_json_bytes'):
        settings['model_counter'][field]=1<<20
    settings['terminal_persistence'].update(operation_id='native-root:'+task['id'],frame_bytes=1<<20)
    policies,restored,handoffs = [],[],[]
    recovered_parameters = {}
    with ThreadPoolExecutor(max_workers=1) as executor:
        async def restore(request,state):
            restored.append(json.loads(json.dumps(state)))
        def checkpoint_factory(request, principal, executor, profile):
            child=request.task
            recovery=(NativeFileToolRecovery(principal=principal,task=child,executor=executor,
                registered_child=True,storage=storage) if any(tool_schema_name(tool)=='write_file'
                for tool in child.get('_invocationTools',[])) else None)
            original=recovered_parameters.get(child['id'],{})
            policy=NativeCheckpointPolicy(principal=principal,task=child,storage=storage,executor=executor,
                epoch=original.get('epoch',child['id']),binding=original.get('binding',checkpoint_binding),token=uuid.uuid4().hex,
                header_bytes=4096,snapshot_bytes=1<<20,identity_bytes=4096,io_timeout_ms=10000,
                restore=restore,registered_child=True,tool_recovery=recovery)
            policies.append(policy)
            return policy
        from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration
        recovery_configuration = NativeGoalRecoveryConfiguration(storage_factory=lambda:storage,
            header_bytes=4096,snapshot_bytes=1<<20,identity_bytes=4096,evidence_bytes=8<<20)
        port=NativePreparedInvocation(host=host,loop=asyncio.get_running_loop(),principal=principal,
            repository=repo,selection=selected,configuration=settings,budget=transport,executor=executor,
            working_directory=Path(directory),pending_calls=1<<20,pending_bytes=1<<20,
            resource_policy=launch_policy({}),checkpoint_factory=checkpoint_factory,flow_scheduler=scheduler,
            goal_recovery=recovery_configuration if production else None,
            pricing_resolver=NativeRoutePricing(principal=principal,margin=0,
                lookup=lambda *args,**kwargs:{'input':5.,'output':20.},rates=lambda provider:7.24))
        task['_nativePreparedInvocation']=port
        def handoff_factory(journal):
            value=NativeGoalHandoff(principal=principal,task=task,storage=storage,
                evidence_bytes=8<<20,io_timeout_ms=10000,journal=journal)
            handoffs.append(value)
            return value
        try:
            if production:
                from lib.agent_core.execution_budget import _ensure_execution_budget
                from lib.orchestration.chat_flow_runner import run_autopilot_via_flow
                _ensure_execution_budget(task)
                await asyncio.to_thread(run_autopilot_via_flow, task)
                return dict(restored=restored, children=[policy.task for policy in policies])
            journal=await asyncio.to_thread(create_goal_execution_journal,task,seed.definition,seed.launch(),
                runs=runs,native_claim=claim,account_handoff_factory=handoff_factory)
            from lib.billing.native_admission import protect_before_dispatch
            await asyncio.to_thread(protect_before_dispatch,task,budget=task['_executionBudget'],owner=owner)
            options=seed.launch().execution_kwargs(task,subflow_resolver=lambda name:None)
            config=OrchestrationAgentRunnerConfig(**{key:options['executor_options'][key]
                for key in OrchestrationAgentRunnerConfig.__dataclass_fields__})
            scope=prepare_flow_recipients(inputs,config=config,emit=lambda event:None,
                abort_check=task['abort_event'].is_set)
            recipients=await asyncio.to_thread(scope.__enter__)
            try:
                bindings=recipients.source_bindings({key:row['admission']
                    for key,row in seed.pending_actors.actors.items()})
                sources={row['admission']['invocation_id']:row['frontier']
                    for row in seed.pending_actors.actors.values()}
                recovered_parameters.update({child['id']:sources[source] for child,source in bindings})
                authorization=dict(format='tofu.native-goal-actor-handoff/v1',claim_request=claim.request,
                    target_reserved_micro=100,actors=[dict(source_task_id=source,target_task_id=child['id'],
                        target_registration_version=owned_child_record(storage,user_id=owner,
                            conversation_id=task['convId'],invocation_id=child['id'])['version'])
                        for child,source in bindings])
                recipients.bind(journal)
                async with handoffs[0].prepare_recipients(bindings,authorization=authorization,
                                                         executor=executor) as preparation:
                    for child,_ in bindings:
                        child['_nativePreparedInvocation'].checkpoint_preparation=preparation
                    options['executor_options'].update(execution_journal=journal,native_flow_library=scheduler)
                    outcome=await asyncio.to_thread(execute_flow,seed.definition,on_event=lambda event:None,**options)
                    if not outcome.result['ok']:
                        raise outcome.exception or ValueError(outcome.error)
                    journal.verify_consumed()
            except BaseException:
                await asyncio.to_thread(scope.__exit__,*sys.exc_info())
                raise
            else:
                await asyncio.to_thread(scope.__exit__,None,None,None)
            await asyncio.to_thread(port.finalize_flow_accounting,task)
            return dict(outcome=outcome,restored=restored,children=[child for child,_ in bindings])
        finally:
            for handoff in handoffs:
                handoff.close()
            dispose_routed_slot_group(group)
            await host.close()
            assert all(policy.closed for policy in policies)
            assert not host._clients and not scheduler._graphs
