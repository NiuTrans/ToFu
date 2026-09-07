"""Canonical launch-probed budgets for local swarm execution.

This module is the only place where swarm code turns the runtime resource
manifest into domain hard ceilings. Callers may lower or raise launch defaults
through environment overrides, but never make thread pools, accepted agent
work, retries, or the live-session registry unbounded.
"""

from __future__ import annotations

import os

from runtime_guards import resolve_resource_budget


def swarm_global_workers() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_GLOBAL_WORKERS', minimum=1, maximum=32)


def swarm_max_parallel() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_MAX_PARALLEL', minimum=1, maximum=16)


def swarm_max_agents_per_wave() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_MAX_AGENTS_PER_WAVE', minimum=1, maximum=32)


def swarm_max_agents_per_session() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_MAX_AGENTS_PER_SESSION', minimum=1, maximum=128)


def swarm_max_retries() -> int:
    raw = os.environ.get('TOFU_SWARM_MAX_RETRIES', '').strip()
    if raw == '0':
        return 0
    return resolve_resource_budget(
        'TOFU_SWARM_MAX_RETRIES', minimum=1, maximum=4)


def swarm_session_capacity() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_SESSION_CAPACITY', minimum=1, maximum=64)


__all__ = [
    'swarm_global_workers',
    'swarm_max_agents_per_session',
    'swarm_max_agents_per_wave',
    'swarm_max_parallel',
    'swarm_max_retries',
    'swarm_session_capacity',
]
