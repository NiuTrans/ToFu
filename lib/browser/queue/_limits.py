"""Resource-derived hard limits for browser clients and async poll waiters."""

from __future__ import annotations

from runtime_guards import resolve_resource_budget


MAX_COMMANDS_PER_POLL = 32
MAX_RESULTS_PER_POLL = 64


class BrowserPollCapacityExceeded(RuntimeError):
    """A bounded browser transport registry has no safe slot available."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def waiter_limits() -> tuple[int, int]:
    """Return ``(process, owner)`` waiter ceilings from the launch profile."""
    process_limit = resolve_resource_budget(
        'TOFU_BROWSER_POLL_MAX_WAITERS', minimum=4, maximum=1024)
    owner_limit = max(2, min(32, process_limit // 2 or 1))
    return process_limit, owner_limit


def client_registry_limits() -> tuple[int, int]:
    """Return ``(process, owner)`` live/recent device registry ceilings."""
    process_limit = resolve_resource_budget(
        'TOFU_BROWSER_CLIENT_REGISTRY_CAPACITY',
        minimum=16, maximum=8192)
    owner_limit = max(8, min(128, process_limit // 4 or 1))
    return process_limit, owner_limit


def login_capture_limits() -> tuple[int, int]:
    """Return process/owner ceilings for ten-minute login-capture threads."""
    poll_limit = resolve_resource_budget(
        'TOFU_BROWSER_POLL_MAX_INFLIGHT', minimum=4, maximum=1024)
    process_limit = max(2, min(64, poll_limit // 2))
    owner_limit = max(2, min(8, process_limit // 2))
    return process_limit, min(process_limit, owner_limit)


__all__ = [
    'BrowserPollCapacityExceeded',
    'MAX_COMMANDS_PER_POLL',
    'MAX_RESULTS_PER_POLL',
    'client_registry_limits',
    'login_capture_limits',
    'waiter_limits',
]
