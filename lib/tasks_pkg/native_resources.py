"""Compile native byte bounds from issued launch and transport policies.

Entry compile_native_resources. Consumes the existing launch_policy result and
owned native host Budget; never probes hardware, allocates workers, or selects
HTTP, model, recovery or persistence policy. Explicit overrides remain visible.
"""
from collections.abc import Mapping

from lib.agent_core.native_process import Budget


def compile_native_transport_budget(*, launch_policy, shutdown_seconds):
    """Size the existing image/stdio owner without probing or allocating work."""
    from lib.agent_core.native_artifact import SPEC
    if (not isinstance(launch_policy, Mapping)
            or type(launch_policy.get('frame_maximum')) is not int
            or launch_policy['frame_maximum'] <= 0):
        raise ValueError('native transport requires issued launch capacity')
    frame_bytes = launch_policy['frame_maximum']
    return Budget(max_inflight=1, binary_bytes=SPEC['maximum_unpacked_bytes'],
                  configuration_bytes=frame_bytes, frame_bytes=frame_bytes,
                  shutdown_seconds=shutdown_seconds)


def compile_native_resources(configuration, *, transport: Budget, launch_policy):
    if (not isinstance(configuration, Mapping) or not isinstance(transport, Budget)
            or not isinstance(launch_policy, Mapping)
            or type(launch_policy.get('frame_maximum')) is not int
            or launch_policy['frame_maximum'] <= 0):
        raise ValueError('native resources require issued launch and transport bounds')
    capacity = launch_policy['frame_maximum']
    # The existing host's explicit limits can be smaller than automatic defaults.
    # No new pool, reservation, aggregate quota or per-task RSS ceiling is created.
    context_bytes = min(capacity, transport.configuration_bytes, transport.frame_bytes)
    frame_bytes = min(capacity, transport.frame_bytes)
    defaults = {
        'event_bytes': frame_bytes,
        'output_bytes': frame_bytes,
        'request_bytes': context_bytes,
        'retained_bytes': context_bytes,
        'feedback_bytes': context_bytes,
        'tool_frame_bytes': frame_bytes,
        # Every distinct JSON tool occurrence costs at least one output byte.
        # This representability bound adds no lower count quota than byte admission.
        'tool_calls': frame_bytes,
        'round_preparation_bytes': context_bytes,
    }
    result = dict(configuration)
    for key, value in defaults.items():
        result.setdefault(key, value)
        if type(result[key]) is not int or result[key] <= 0:
            raise ValueError('native resources require positive byte/count bounds')
    for key in ('rounds', 'deadline_ms', 'total_tool_calls'):
        result.setdefault(key, None)
        value = result[key]
        if value is not None and (type(value) is not int or value < (0 if key == 'total_tool_calls' else 1)):
            raise ValueError('native resources require valid optional execution limits')
    return result
