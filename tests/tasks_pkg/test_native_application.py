"""Application policy binds issued pending tasks without creating native work."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock,patch

import pytest
from lib.app.app_factory import create_base_app
from lib.agent_core.native_process import NativeConfigurationError
from lib.tasks_pkg.native_application import NativeRootConfiguration,register_native_application,bind_application_native_task
from lib.tasks_pkg.native_host import register_native_host
from tests.tasks_pkg.test_native_host import image
from tests.model_routing.test_native_route import Repository,principal,selection
from tests.model_routing.test_native_issued_route import group

pytestmark=pytest.mark.unit


def profile():
    from tests.support.native_process_probe import config
    from lib.model_routing.native_route import ROUTE_OVERRIDE_FIELDS
    value=config('ignored','task')
    for key in ({'version','type','owner_user_id','task_id','messages','tools','epoch',
            'event_bytes','output_bytes','request_bytes','retained_bytes','feedback_bytes',
            'tool_frame_bytes','tool_calls','rounds','deadline_ms'} | ROUTE_OVERRIDE_FIELDS):
        value.pop(key,None)
    value.update(model_control=dict(ack_ms=5000,pricing=True),
        model_counter=dict(input_bytes=4096,text_bytes=4096,tool_json_bytes=4096,depth=128,worker_stack_bytes=1<<20),
        terminal_persistence=dict(attempts=2,ack_ms=5000))
    return value


def task():
    repo=Repository();issued=group(repo)
    return dict(id='task',status='pending',_userId=7,_principalContext=principal().to_payload(),
        _model_routing_group=issued,_model_routing_selection=selection(),_pinned_provider_id=issued.pin_id)


@pytest.mark.parametrize('flow,actor_recovery', [(False, False), (True, False), (True, True)])
def test_application_binding_borrows_issued_owners_and_freezes_policy(flow, actor_recovery):
    async def run():
        app=create_base_app(__name__);release=image();host=register_native_host(app,image=release)
        from lib.tasks_pkg.native_pricing import NativeRoutePricing
        raw=profile();repository=Mock(return_value=Repository())
        raw.update(checkpoint_tree_usage=True,checkpoint_shared_account=True,checkpoint_frame_bytes=4096,checkpoint_child_routes=True)
        checkpoint=Mock()
        from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration, recovery_options
        recovery = NativeGoalRecoveryConfiguration(storage_factory=Mock(return_value=SimpleNamespace(request_timeout_seconds=8))) if actor_recovery else None
        if actor_recovery:
            raw['checkpoint_actor_account'] = True
        pricing=Mock(return_value=NativeRoutePricing(principal=principal(),margin=0))
        options=NativeRootConfiguration(configuration=raw,working_directory=Path('/tmp'),
            repository_factory=repository,pricing_factory=pricing,checkpoint_factory=checkpoint,
            goal_recovery=recovery)
        binding=register_native_application(app,configuration=options)
        assert register_native_application(app,configuration=options) is binding
        raw['model_control']['pricing']=False
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            async with app.test_app():
                runtime=SimpleNamespace(loop=asyncio.get_running_loop(),sync_executor=callbacks,
                    shutdown_requested=threading.Event(),resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))
                app.extensions['tofu_serving_loop_lifecycle']={'runtime':runtime}
                value=task()
                with patch('lib.agent_core.native_artifact.seal_archive') as seal:
                    async with app.app_context():
                        bind_application_native_task(value,flow=flow)
                        application_options = recovery_options()
                    port=value['_nativePreparedInvocation']
                    assert port.host is host and port.loop is runtime.loop and port.executor is callbacks
                    assert port.selection is value['_model_routing_selection']
                    assert repository.call_args.args==pricing.call_args.args[:1]==(principal(),)
                    import json
                    settings=json.loads(port._profile)
                    assert settings['model_control']['pricing'] is True
                    assert settings['epoch']=='task' and settings['terminal_persistence']['operation_id']=='native-root:task'
                    assert settings['rounds'] is settings['deadline_ms'] is None
                    assert port.checkpoint_factory is (None if flow and not actor_recovery else checkpoint)
                    assert port.goal_recovery is recovery
                    if flow:
                        assert 'checkpoint_tree_usage' not in settings and 'checkpoint_child_routes' not in settings
                        assert ('checkpoint_shared_account' in settings) == actor_recovery
                        if actor_recovery:
                            options = recovery_options(value)
                            assert options['pending_actor_bounds']['snapshot_bytes'] == release.budget.frame_bytes
                            assert options['storage'] is recovery.storage_factory.return_value
                            assert application_options == options
                        else:
                            assert application_options == {}
                        assert value['_nativePricingRequired'] and value['_nativeFrozenCostProjection']
                        assert value['_executionBudget'].consumption()[0]==0
                    else:
                        assert settings['checkpoint_tree_usage'] is True
                    checkpoint.assert_not_called()
                    assert not host._clients
                    seal.assert_not_called()
                with pytest.raises(NativeConfigurationError):binding.bind_task(value)
        assert binding.closed and binding.app is None and host._closed
    asyncio.run(run())


@pytest.mark.parametrize('fault',['owner','tenant','selection','pin','disposed','shutdown','resources','role'])
def test_application_binding_refuses_foreign_or_retired_owners_before_factories(fault):
    async def run():
        app=create_base_app(__name__);register_native_host(app,image=image())
        repository,pricing=Mock(),Mock()
        binding=register_native_application(app,configuration=NativeRootConfiguration(configuration=profile(),
            working_directory=Path('/tmp'),repository_factory=repository,pricing_factory=pricing))
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            async with app.test_app():
                runtime=SimpleNamespace(loop=asyncio.get_running_loop(),sync_executor=callbacks,
                    shutdown_requested=threading.Event(),resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))
                app.extensions['tofu_serving_loop_lifecycle']={'runtime':runtime}
                value=task()
                if fault=='owner':value['_userId']=8
                elif fault=='tenant':value['_principalContext']['tenant_id']='foreign'
                elif fault=='selection':value.pop('_model_routing_selection')
                elif fault=='pin':value['_pinned_provider_id']='other'
                elif fault=='disposed':value['_model_routing_group'].disposed=True
                elif fault=='shutdown':runtime.shutdown_requested.set()
                elif fault=='resources':runtime.resource_controller=None
                else:app.extensions['tofu_serving_loop_lifecycle']['owns_task_workers']=False
                with pytest.raises(NativeConfigurationError):binding.bind_task(value)
                assert '_nativePreparedInvocation' not in value
                repository.assert_not_called();pricing.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('field',['epoch','messages','endpoint'])
def test_application_profile_cannot_supply_request_or_route_authority(field):
    raw=profile();raw[field]='foreign'
    with pytest.raises(NativeConfigurationError):
        NativeRootConfiguration(configuration=raw,working_directory=Path('/tmp'))


def test_application_factories_are_synchronous_worker_ports():
    async def repository(principal):return Repository()
    with pytest.raises(NativeConfigurationError):
        NativeRootConfiguration(configuration=profile(),working_directory=Path('/tmp'),repository_factory=repository)


@pytest.mark.parametrize('flow,cancelled,expected',[(False,False,True),(True,False,False),(False,True,False)])
def test_explicit_image_preparation_requires_live_root_value(flow,cancelled,expected):
    from lib.tasks_pkg.native_host import NativeHost
    from lib.tasks_pkg.native_pricing import NativeRoutePricing
    async def run():
        app=create_base_app(__name__);host=register_native_host(app,image=image())
        options=NativeRootConfiguration(configuration=profile(),working_directory=Path('/tmp'),
            image_preparation=True,repository_factory=lambda p:Repository(),
            pricing_factory=lambda p,t:NativeRoutePricing(principal=p,margin=0))
        binding=register_native_application(app,configuration=options)
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            async with app.test_app():
                runtime=SimpleNamespace(loop=asyncio.get_running_loop(),sync_executor=callbacks,
                    shutdown_requested=threading.Event(),resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))
                app.extensions['tofu_serving_loop_lifecycle']={'runtime':runtime}
                value=task();value['abort_event']=threading.Event()
                if cancelled:value['abort_event'].set()
                with patch.object(NativeHost,'prepare_image') as prepare:
                    binding.bind_task(value,flow=flow)
                    assert prepare.called is expected
                    if expected:prepare.assert_called_once_with(runtime.loop)
                assert value['_nativePreparedInvocation'].host is host
    asyncio.run(run())


def test_image_preparation_selection_is_strict_and_default_lazy():
    assert NativeRootConfiguration(configuration=profile(),working_directory=Path('/tmp')).image_preparation is False
    with pytest.raises(NativeConfigurationError,match='explicit boolean'):
        NativeRootConfiguration(configuration=profile(),working_directory=Path('/tmp'),image_preparation=1)
