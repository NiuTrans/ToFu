"""Native automatic resource composition scales without another launch probe."""
from copy import deepcopy

import pytest

from lib.agent_core.native_process import Budget
from lib.tasks_pkg.native_resources import compile_native_resources

pytestmark = pytest.mark.unit


def test_issued_capacity_and_explicit_host_limits_scale_independently():
    transport = Budget(1, 1 << 20, 1 << 28, 1 << 29, 2)
    small = compile_native_resources({}, transport=transport, launch_policy={'frame_maximum': 1 << 20})
    large = compile_native_resources({}, transport=transport, launch_policy={'frame_maximum': 1 << 30})
    assert small['retained_bytes'] == small['output_bytes'] == 1 << 20
    assert large['retained_bytes'] == large['request_bytes'] == 1 << 28
    assert large['output_bytes'] == large['tool_frame_bytes'] == 1 << 29
    assert large['tool_calls'] == large['output_bytes']
    assert all(large[key] is None for key in ('rounds', 'deadline_ms', 'total_tool_calls'))


def test_explicit_overrides_and_policy_objects_are_preserved_without_mutation():
    source = {'output_bytes': 700, 'rounds': 9, 'total_tool_calls': 0,
              'deadline_ms': 500, 'policy': {'connect_ms': 100},
              'terminal_persistence': {'attempts': 3, 'ack_ms': 200}}
    before = deepcopy(source)
    result = compile_native_resources(source, transport=Budget(1, 1000, 2000, 3000, 2),
                                     launch_policy={'frame_maximum': 4000})
    assert all(result[key] == value for key, value in before.items())
    assert source == before
    assert result['policy'] is source['policy']


@pytest.mark.parametrize('key,value', [('frame_maximum', True), ('frame_maximum', 0),
                                     ('frame_maximum', None)])
def test_invalid_launch_capacity_refuses(key, value):
    with pytest.raises(ValueError):
        compile_native_resources({}, transport=Budget(1, 1000, 2000, 3000, 2),
                                 launch_policy={key: value})


@pytest.mark.parametrize('key,value', [('retained_bytes', 0), ('output_bytes', True),
                                     ('tool_calls', -1), ('rounds', 0),
                                     ('deadline_ms', False), ('total_tool_calls', -1)])
def test_invalid_explicit_resource_override_refuses(key, value):
    with pytest.raises(ValueError):
        compile_native_resources({key: value}, transport=Budget(1, 1000, 2000, 3000, 2),
                                 launch_policy={'frame_maximum': 4000})
