"""Wire batch limits and launch-derived reconstructible route-cache sizing."""

from __future__ import annotations

from runtime_guards import resolve_resource_budget


MAX_COMMANDS_PER_POLL = 32
MAX_RESULTS_PER_POLL = 64


def route_cache_capacity() -> int:
    """Legacy setting sizes reconstructible affinity metadata, never admission."""
    process_limit = resolve_resource_budget(
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
        minimum=16, maximum=8192)
    return process_limit


__all__ = [
    'MAX_COMMANDS_PER_POLL',
    'MAX_RESULTS_PER_POLL',
    'route_cache_capacity',
]
