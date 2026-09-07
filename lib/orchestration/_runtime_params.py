"""Unified persisted-node to effective-runtime parameter projection."""

from __future__ import annotations

import copy

from lib.orchestration._control_specs import (
    CONTROL_NODE_DEFAULTS,
    resolve_control_param,
)
from lib.orchestration._role_axes import (
    resolve_emits,
    resolve_isolation,
    resolve_scope,
    resolve_tier,
)


_ROLE_RESOLVERS = {
    'emits': resolve_emits,
    'isolation': resolve_isolation,
    'tier': resolve_tier,
}
_SUBFLOW_RESOLVERS = {
    'emits': resolve_emits,
    'scope': resolve_scope,
}
_CONTROL_PARAM_OWNERS = {
    key: owners[0]
    for key in {
        field
        for defaults in CONTROL_NODE_DEFAULTS.values()
        for field in defaults
    }
    if len(owners := [
        kind for kind, defaults in CONTROL_NODE_DEFAULTS.items()
        if key in defaults
    ]) == 1
}


def resolve_node_runtime_param(node: dict, key: str):
    """Resolve one effective value without exposing node-type policy."""
    node = node if isinstance(node, dict) else {}
    node_type = node.get('type')
    is_compact_role = (
        not node_type and bool(node.get('role')) and not node.get('kind')
    )
    if ((node_type == 'role' or is_compact_role)
            and key in _ROLE_RESOLVERS):
        return _ROLE_RESOLVERS[key](node)
    if node_type == 'subflow' and key in _SUBFLOW_RESOLVERS:
        return _SUBFLOW_RESOLVERS[key](node)
    control_kind = node.get('kind') or (
        _CONTROL_PARAM_OWNERS.get(key) if not node_type else ''
    )
    if node_type == 'control' or (not node_type and control_kind):
        return resolve_control_param(node, key, kind=control_kind)
    params = node.get('params') or {}
    if isinstance(params, dict) and key in params:
        return copy.deepcopy(params[key])
    return None


def node_runtime_defaults() -> dict:
    """Return detached fallbacks applied when persisted params are absent."""
    role = {'type': 'role', 'role': 'general'}
    subflow = {'type': 'subflow', 'role': 'general'}

    def control(kind: str, key: str):
        return resolve_node_runtime_param(
            {'type': 'control', 'kind': kind}, key)

    return {
        'role': {
            key: resolve_node_runtime_param(role, key)
            for key in ('tier', 'isolation')
        },
        'controls': {
            'loop': {
                key: control('loop', key)
                for key in ('max_iterations', 'stop_condition')
            },
            'human': {
                key: control('human', key)
                for key in ('mode', 'timeout_sec')
            },
        },
        'subflow': {
            'scope': resolve_node_runtime_param(subflow, 'scope'),
        },
    }


__all__ = ['node_runtime_defaults', 'resolve_node_runtime_param']
