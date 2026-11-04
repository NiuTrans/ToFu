"""Typed local context-admission failures shared by execution and display.

This module declares evidence only; it owns no reader, provider, retry or store.
Consumers refuse model admission when a required current snapshot is unavailable.
"""

from lib.llm.llm_errors import RequestScopedError


class MessageInputScopeError(RequestScopedError, PermissionError):
    """Local input scope denial: no route retry, fallback, or health penalty."""

    failure_stage = 'before_dispatch'


class TurnRequirementsDeadlineError(TimeoutError):
    """The local requirement reader did not finish before its admission deadline."""

    failure_stage = 'turn_requirements'
    provider_reached = False

    def __init__(self, timeout_seconds: float):
        self.timeout_seconds = timeout_seconds
        super().__init__(
            'Current task requirements could not be read before the context deadline')
