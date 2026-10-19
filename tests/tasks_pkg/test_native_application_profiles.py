"""One native application keeps ROOT and actor-checkpoint Flow policy distinct."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from types import SimpleNamespace
import threading
from unittest.mock import Mock, patch

import pytest

from lib.agent_core.native_process import NativeConfigurationError
from lib.app.app_factory import create_base_app
from lib.goal_runs.native_recovery import NativeGoalRecoveryConfiguration, recovery_options
from lib.tasks_pkg.native_application import NativeRootConfiguration, register_native_application
from lib.tasks_pkg.native_host import register_native_host
from lib.tasks_pkg.native_pricing import NativeRoutePricing
from tests.model_routing.test_native_route import Repository
from tests.tasks_pkg.test_native_application import profile, task
from tests.tasks_pkg.test_native_host import image

pytestmark = pytest.mark.unit


def actor_profile():
    value = profile()
    value.update(checkpoint_actor_account=True, checkpoint_shared_account=True,
                 checkpoint_tree_usage=True, checkpoint_child_routes=True)
    value['terminal_persistence']['ack_ms'] = 7000
    return value


@pytest.mark.parametrize('order', [(False, True), (True, False)])
@pytest.mark.parametrize('root_checkpoint', [False, True])
def test_same_application_binds_both_policies_without_crossing_recovery_authority(order, root_checkpoint):
    async def run():
        app = create_base_app(__name__)
        release = image()
        host = register_native_host(app, image=release)
        root_factory = Mock() if root_checkpoint else None
        flow_factory, storage = Mock(), Mock()
        storage.return_value.request_timeout_seconds=9.5
        recovery = NativeGoalRecoveryConfiguration(storage_factory=storage)
        root_settings, flow_settings = profile(), actor_profile()
        options = NativeRootConfiguration(
            configuration=root_settings, flow_configuration=flow_settings,
            working_directory=Path('/tmp'), repository_factory=lambda principal: Repository(),
            pricing_factory=lambda principal, value: NativeRoutePricing(principal=principal, margin=0),
            checkpoint_factory=root_factory, flow_checkpoint_factory=flow_factory,
            goal_recovery=recovery)
        binding = register_native_application(app, configuration=options)
        # Each profile is frozen at application composition, before tasks exist.
        root_settings['model_control']['pricing'] = False
        flow_settings['checkpoint_actor_account'] = False
        flow_settings['terminal_persistence']['ack_ms'] = 1
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            async with app.test_app():
                app.extensions['tofu_serving_loop_lifecycle'] = {'runtime': SimpleNamespace(
                    loop=asyncio.get_running_loop(), sync_executor=callbacks,
                    shutdown_requested=threading.Event(),
                    resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))}
                issued = {}
                with patch('lib.agent_core.native_artifact.seal_archive') as seal:
                    async with app.app_context():
                        for flow in order:
                            value = task()
                            value['id'] = 'flow' if flow else 'ordinary-root'
                            binding.bind_task(value, flow=flow)
                            port = value['_nativePreparedInvocation']
                            settings = json.loads(port._profile)
                            assert port.host is host
                            assert settings['epoch'] == value['id']
                            assert settings['model_control']['pricing'] is True
                            assert settings['terminal_persistence']['operation_id'] == 'native-root:' + value['id']
                            assert port.checkpoint_factory is (flow_factory if flow else root_factory)
                            assert port.goal_recovery is (recovery if flow else None)
                            assert settings.get('checkpoint_actor_account', False) is flow
                            assert settings['terminal_persistence']['ack_ms'] == (7000 if flow else 5000)
                            assert 'checkpoint_tree_usage' not in settings
                            assert 'checkpoint_child_routes' not in settings
                            issued[flow] = value
                        assert recovery_options(issued[False]) == {}
                        preflight = recovery_options()
                        assert preflight == recovery_options(issued[True])
                        assert preflight['pending_actor_bounds']['io_timeout_ms'] == 9500
                    seal.assert_not_called()
                    assert not host._clients
                    flow_factory.assert_not_called()
                    if root_factory is not None:
                        root_factory.assert_not_called()
        assert binding.closed and host._closed and release._image is None
    asyncio.run(run())


@pytest.mark.parametrize('field', [
    'version', 'type', 'owner_user_id', 'task_id', 'messages', 'tools',
    'wire_tools', 'wire_tool_preparation', 'epoch', 'endpoint', 'headers', 'template',
])
def test_flow_profile_cannot_supply_request_or_route_authority(field):
    value = actor_profile()
    value[field] = 'foreign'
    with pytest.raises(NativeConfigurationError, match='owned policy'):
        NativeRootConfiguration(configuration=profile(), flow_configuration=value,
                                working_directory=Path('/tmp'))


@pytest.mark.parametrize('fault', ['profile', 'terminal', 'operation', 'counter', 'policy', 'pricing'])
def test_flow_profile_uses_the_same_required_policy_validation(fault):
    value = actor_profile()
    if fault == 'profile':
        value = []
    elif fault == 'terminal':
        value['terminal_persistence'] = []
    elif fault == 'operation':
        value['terminal_persistence']['operation_id'] = 'foreign'
    elif fault == 'counter':
        value['model_counter'] = None
    elif fault == 'policy':
        value['policy'] = None
    else:
        value['model_control']['pricing'] = False
    with pytest.raises(NativeConfigurationError, match='owned policy'):
        NativeRootConfiguration(configuration=profile(), flow_configuration=value,
                                working_directory=Path('/tmp'))


@pytest.mark.parametrize('fault', ['actor', 'shared', 'factory'])
def test_goal_recovery_requires_the_selected_flow_checkpoint_policy(fault):
    value = actor_profile()
    if fault != 'factory':
        value['checkpoint_actor_account' if fault == 'actor' else 'checkpoint_shared_account'] = False
    with pytest.raises(NativeConfigurationError, match='actor checkpoint'):
        NativeRootConfiguration(
            configuration=actor_profile(), flow_configuration=value,
            working_directory=Path('/tmp'),
            flow_checkpoint_factory=None if fault == 'factory' else Mock(),
            goal_recovery=NativeGoalRecoveryConfiguration())


def test_flow_checkpoint_factory_must_be_a_synchronous_worker_port():
    async def asynchronous_factory(*args):
        return None
    for factory in (False, object(), asynchronous_factory):
        with pytest.raises(NativeConfigurationError, match='owned policy'):
            NativeRootConfiguration(configuration=profile(), flow_configuration=actor_profile(),
                working_directory=Path('/tmp'), flow_checkpoint_factory=factory)


def test_omitted_flow_policy_preserves_the_existing_explicit_shared_configuration():
    factory = Mock()
    options = NativeRootConfiguration(configuration=actor_profile(), working_directory=Path('/tmp'),
        checkpoint_factory=factory, goal_recovery=NativeGoalRecoveryConfiguration())
    assert options.checkpoint_factory is options.flow_checkpoint_factory is factory
    assert options._flow_profile is options._profile


def test_shared_actor_profile_still_refuses_ordinary_root_without_changing_its_policy():
    async def run():
        app = create_base_app(__name__)
        host = register_native_host(app, image=image())
        factory = Mock()
        binding = register_native_application(app, configuration=NativeRootConfiguration(
            configuration=actor_profile(), checkpoint_factory=factory,
            working_directory=Path('/tmp'), repository_factory=lambda principal: Repository(),
            pricing_factory=lambda principal, value: NativeRoutePricing(principal=principal, margin=0)))
        with ThreadPoolExecutor(max_workers=1) as callbacks:
            async with app.test_app():
                app.extensions['tofu_serving_loop_lifecycle'] = {'runtime': SimpleNamespace(
                    loop=asyncio.get_running_loop(), sync_executor=callbacks,
                    shutdown_requested=threading.Event(),
                    resource_controller=SimpleNamespace(launch_policy={'frame_maximum':4096}))}
                value = task()
                with pytest.raises(NativeConfigurationError, match='explicit Flow checkpoint'):
                    binding.bind_task(value)
                assert '_nativePreparedInvocation' not in value
                factory.assert_not_called()
                assert not host._clients and host.image._image is None
    asyncio.run(run())
