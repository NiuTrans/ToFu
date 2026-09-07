"""Finite dispatch policy for background production model calls.

Responsibility: resolve the launch-probed upstream-429 allowance, enforce the
shared hard-error slot-attempt ceiling, and adapt a task event into the
``dispatch_chat`` cancellation seam.  Capability recipes own prompts, models,
stages, and quality fallbacks; this module owns only the common transport
budget.  It deliberately does not import an LLM implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

OPTIONAL_LLM_MAX_429_ATTEMPTS = 16
PRODUCTION_LLM_HARD_ERROR_ATTEMPTS = 2
PRODUCTION_LLM_MAX_429_ATTEMPTS = 64

__all__ = [
    'OPTIONAL_LLM_MAX_429_ATTEMPTS',
    'PRODUCTION_LLM_HARD_ERROR_ATTEMPTS',
    'PRODUCTION_LLM_MAX_429_ATTEMPTS',
    'abort_check_from_event',
    'optional_llm_dispatch_kwargs',
    'optional_llm_max_429_attempts',
    'production_llm_dispatch_kwargs',
    'production_llm_max_429_attempts',
]


def optional_llm_max_429_attempts(
        environment: Mapping[str, str] | None = None) -> int:
    """Return the finite 429 allowance for reconstructible enrichment.

    Project summaries, automatic titles, daily-report analysis, and optimizer
    proposals already have deterministic or empty fallbacks. Their transport
    allowance is therefore intentionally lower than the budget for an
    explicitly requested production deliverable.
    """
    from runtime_guards import resolve_resource_budget
    return resolve_resource_budget(
        'TOFU_OPTIONAL_LLM_MAX_429_ATTEMPTS',
        environment,
        maximum=OPTIONAL_LLM_MAX_429_ATTEMPTS,
    )


def optional_llm_dispatch_kwargs(
        environment: Mapping[str, str] | None = None) -> dict:
    """Build the common transport policy for reconstructible enrichment.

    Optional work has a deterministic/cached fallback, so it spends only the
    launch-profiled upstream-429 allowance and yields before transport when a
    provider/model family is already known to be contended. Callers still own
    the request-local strict billing-stop context because that context must
    surround the concrete dispatch, not unrelated prompt construction.
    """
    return {
        'max_429_attempts': optional_llm_max_429_attempts(environment),
        'defer_on_shared_contention': True,
    }


def production_llm_max_429_attempts(value: int | None = None) -> int:
    """Return a positive per-dispatch allowance under the shared hard cap."""
    if value is None:
        from runtime_guards import resolve_resource_budget
        return resolve_resource_budget(
            'TOFU_PRODUCTION_LLM_MAX_429_ATTEMPTS',
            maximum=PRODUCTION_LLM_MAX_429_ATTEMPTS)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('max_429_attempts must be a positive integer')
    return min(PRODUCTION_LLM_MAX_429_ATTEMPTS, value)


def abort_check_from_event(event) -> Callable[[], bool] | None:
    """Adapt an optional event without creating a polling seam when absent."""
    if event is None:
        return None
    return lambda: bool(event.is_set())


def production_llm_dispatch_kwargs(
        *, abort_check: Callable[[], bool] | None = None,
        max_429_attempts: int | None = None) -> dict:
    """Build the common finite kwargs for one background model dispatch."""
    kwargs = {
        'max_retries': PRODUCTION_LLM_HARD_ERROR_ATTEMPTS,
        'max_429_attempts': production_llm_max_429_attempts(
            max_429_attempts),
    }
    if abort_check is not None:
        kwargs['abort_check'] = abort_check
    return kwargs
