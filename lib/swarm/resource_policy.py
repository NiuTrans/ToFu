"""Launch sizing hints and explicit retry policy for swarm execution.

Thread reuse and terminal-memory cache sizing follow the launch probe. Accepted
children, waves and live sessions are demand-driven. Retry policy remains
finite and does not change the user's inherited model/tool/token authority.
"""

from __future__ import annotations

import os

from runtime_guards import resolve_resource_budget


def swarm_max_parallel() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_MAX_PARALLEL', minimum=1, maximum=64)


def swarm_max_retries() -> int:
    raw = os.environ.get('TOFU_SWARM_MAX_RETRIES', '').strip()
    if raw == '0':
        return 0
    return resolve_resource_budget(
        'TOFU_SWARM_MAX_RETRIES', minimum=1, maximum=4)


def swarm_terminal_cache_target() -> int:
    return resolve_resource_budget(
        'TOFU_SWARM_SESSION_CAPACITY', minimum=1, maximum=128)


__all__ = [
    'swarm_max_parallel',
    'swarm_max_retries',
    'swarm_terminal_cache_target',
]
