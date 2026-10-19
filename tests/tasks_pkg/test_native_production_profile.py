"""Production native composition follows capacity without acquiring execution."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from lib.agent_core.native_process import Budget
from lib.tasks_pkg.native_application import NativeRootConfiguration
from lib.tasks_pkg.native_execution import prepare_native_application_counter
from lib.tasks_pkg.native_production_profile import compile_native_application_profile
from lib.tasks_pkg.native_resources import compile_native_transport_budget

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('capacity', [1 << 20, 128 << 20, 2 << 30])
def test_production_profile_scales_bytes_without_model_or_execution_limits(capacity):
    policy = {'frame_maximum': capacity, 'root_maximum': 17}
    with patch('lib.agent_core.native_artifact.seal_archive') as seal:
        budget = compile_native_transport_budget(launch_policy=policy, shutdown_seconds=7)
        profile = compile_native_application_profile(transport=budget, launch_policy=policy)
        root = NativeRootConfiguration(configuration=profile, working_directory=Path('/tmp'))
        seal.assert_not_called()
    assert budget.frame_bytes == budget.configuration_bytes == capacity
    assert budget.shutdown_seconds == 7
    assert profile['request_bytes'] == profile['retained_bytes'] == capacity
    assert profile['model_counter']['input_bytes'] == profile['model_counter']['text_bytes'] == capacity
    assert profile['model_counter']['tool_json_bytes'] == capacity
    assert profile['terminal_persistence'] == {'ack_ms': None, 'attempts': 2, 'frame_bytes': capacity}
    assert profile['model_control'] == {'ack_ms': None, 'pricing': True}
    assert profile['rounds'] is profile['deadline_ms'] is profile['total_tool_calls'] is None
    assert profile['policy']['request_headers_ms'] is None
    counter = prepare_native_application_counter({'logical_model': 'gpt-4o'},
        {'max_completion_tokens': 123}, profile['model_counter'])
    assert counter['encoding'] == 'o200k_base' and counter['output_field'] == 'max_completion_tokens'
    assert counter['depth'] == 128 and counter['worker_stack_bytes'] == 2 << 20
    assert 'encoding' not in profile['model_counter'] and 'operation_id' not in profile['terminal_persistence']
    profile['model_control']['pricing'] = False
    assert json.loads(root._profile)['model_control']['pricing'] is True


def test_smaller_explicit_host_bounds_and_detached_transport_override_are_preserved():
    override = {'write_ms': None, 'stream_idle_ms': 31}
    profile = compile_native_application_profile(transport=Budget(0, 1000, 2000, 3000, 2),
        launch_policy={'frame_maximum': 4000}, http_overrides=override)
    assert profile['model_counter']['input_bytes'] == 2000
    assert profile['terminal_persistence']['frame_bytes'] == 3000
    assert profile['policy']['write_ms'] is None and profile['policy']['stream_idle_ms'] == 31
    override['stream_idle_ms'] = 99
    assert profile['policy']['stream_idle_ms'] == 31
    # Overrides remain ordinary trusted configuration and are frozen by the
    # existing application owner, never interpreted as request authority.
    profile['model_control']['ack_ms'] = 7000
    profile['terminal_persistence'].update(ack_ms=9000, attempts=3)
    root = NativeRootConfiguration(configuration=profile, working_directory=Path('/tmp'))
    assert json.loads(root._profile)['terminal_persistence']['attempts'] == 3


@pytest.mark.parametrize('policy', [{}, {'frame_maximum': True}, {'frame_maximum': 0}, None])
def test_transport_requires_issued_positive_capacity(policy):
    with pytest.raises(ValueError):
        compile_native_transport_budget(launch_policy=policy, shutdown_seconds=7)


def test_release_composition_is_lazy_and_preserves_existing_checkpoint_and_flow_owners():
    from lib.tasks_pkg.native_production_profile import compose_native_application
    from lib.version import __version__
    checkpoint = lambda *args: None
    with patch('lib.agent_core.native_artifact.seal_archive') as seal:
        native = compose_native_application(archive=Path('/missing/pinned-native.tar.gz'),
            sha256='1' * 64, working_directory=Path('/tmp'),
            launch_policy={'frame_maximum': 1 << 20}, shutdown_seconds=7,
            checkpoint_factory=checkpoint, flow_checkpoint_factory=checkpoint, flow_archive=True)
        seal.assert_not_called()
    image, root = native['native_image'], native['native_root']
    assert image.sha256 == '1' * 64 and image.application_version == __version__
    assert not image.admission_started and image.budget.frame_bytes == 1 << 20
    assert root.checkpoint_factory is root.flow_checkpoint_factory is checkpoint
    assert root.flow_archive is True
    assert json.loads(root._profile)['model_control'] == {'ack_ms': None, 'pricing': True}
