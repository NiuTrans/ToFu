"""Canonical params for newly-authored orchestration nodes.

This module is deliberately independent of Flask, the store and the engine.
The Studio authoring contract and server-authored built-in graphs both consume
these constructors, so default params cannot drift between entry points.
Runtime compatibility fallbacks remain an engine concern: these functions
describe what a new definition should persist, not how an old one is repaired.
"""

from __future__ import annotations

import copy
from typing import Any

from lib.orchestration._control_specs import CONTROL_NODE_DEFAULTS
from lib.orchestration._role_axes import (
    DEFAULT_ROLE_ISOLATION,
    DEFAULT_ROLE_TIER,
)


def _role_node_params(
    *,
    tier: str = DEFAULT_ROLE_TIER,
    **overrides: Any,
) -> dict:
    params = {
        'objective': '',
        'tier': tier or DEFAULT_ROLE_TIER,
        'isolation': DEFAULT_ROLE_ISOLATION,
    }
    params.update(copy.deepcopy(overrides))
    return params


def _control_node_params(kind: str, **overrides: Any) -> dict:
    try:
        params = copy.deepcopy(CONTROL_NODE_DEFAULTS[kind])
    except KeyError as exc:
        raise ValueError(f'unknown orchestration control kind: {kind!r}') from exc
    params.update(copy.deepcopy(overrides))
    return params


def _subflow_node_params(**overrides: Any) -> dict:
    params = {'scope': 'isolated'}
    params.update(copy.deepcopy(overrides))
    return params


def node_authoring_params(
    node_type: str,
    *,
    kind: str = '',
    **overrides: Any,
) -> dict:
    """Return detached defaults through the canonical node-type interface."""
    normalized_type = str(node_type or '').strip()
    if normalized_type == 'role':
        tier = overrides.pop('tier', DEFAULT_ROLE_TIER)
        return _role_node_params(tier=tier, **overrides)
    if normalized_type == 'control':
        return _control_node_params(kind, **overrides)
    if normalized_type == 'subflow':
        return _subflow_node_params(**overrides)
    raise ValueError(f'unknown orchestration node type: {node_type!r}')


def role_node_params(
    *,
    tier: str = DEFAULT_ROLE_TIER,
    **overrides: Any,
) -> dict:
    """Compatibility convenience for a newly-authored role node."""
    return node_authoring_params('role', tier=tier, **overrides)


def control_node_params(kind: str, **overrides: Any) -> dict:
    """Compatibility convenience for a newly-authored control node."""
    return node_authoring_params('control', kind=kind, **overrides)


def subflow_node_params(**overrides: Any) -> dict:
    """Compatibility convenience for a newly-authored subflow node."""
    return node_authoring_params('subflow', **overrides)


def all_control_node_params() -> dict[str, dict]:
    """Return all canonical control defaults as a detached mapping."""
    return {
        kind: node_authoring_params('control', kind=kind)
        for kind in CONTROL_NODE_DEFAULTS
    }


__all__ = [
    'node_authoring_params', 'role_node_params', 'control_node_params',
    'all_control_node_params', 'subflow_node_params',
]
