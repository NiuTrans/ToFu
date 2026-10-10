"""Finite resource policy for background production image generation.

Responsibility: resolve the launch-probed per-job image-call fan-out and the
finite provider-429 allowance used by long-running production capabilities.
Capability recipes own prompts, asset counts, byte budgets, and fallbacks;
the image generator remains interactive-compatible when these optional
limits are not supplied.
"""

from __future__ import annotations

PRODUCTION_IMAGE_HARD_ERROR_ATTEMPTS = 2
PRODUCTION_IMAGE_MAX_FANOUT = 4
PRODUCTION_IMAGE_MAX_429_ATTEMPTS = 64

__all__ = [
    'PRODUCTION_IMAGE_HARD_ERROR_ATTEMPTS',
    'PRODUCTION_IMAGE_MAX_FANOUT',
    'PRODUCTION_IMAGE_MAX_429_ATTEMPTS',
    'production_image_dispatch_kwargs',
    'production_image_fanout',
    'production_image_max_429_attempts',
]


def _bounded_explicit(value: int, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f'{name} must be a positive integer')
    return min(maximum, value)


def production_image_fanout(value: int | None = None) -> int:
    """Return the per-job image-call fan-out under the shared hard cap."""
    if value is not None:
        return _bounded_explicit(
            value, name='image_fanout', maximum=PRODUCTION_IMAGE_MAX_FANOUT)
    from runtime_guards import resolve_resource_budget
    return resolve_resource_budget(
        'TOFU_PRODUCTION_IMAGE_FANOUT', maximum=PRODUCTION_IMAGE_MAX_FANOUT)


def production_image_max_429_attempts(value: int | None = None) -> int:
    """Return a finite image-provider 429 allowance under the hard cap."""
    if value is not None:
        return _bounded_explicit(
            value, name='max_429_attempts',
            maximum=PRODUCTION_IMAGE_MAX_429_ATTEMPTS)
    from runtime_guards import resolve_resource_budget
    return resolve_resource_budget(
        'TOFU_PRODUCTION_IMAGE_MAX_429_ATTEMPTS',
        maximum=PRODUCTION_IMAGE_MAX_429_ATTEMPTS)


def production_image_dispatch_kwargs(
        *, abort_check=None, max_429_attempts: int | None = None) -> dict:
    """Build finite optional arguments for ``generate_image``."""
    kwargs = {
        # generate_image counts retries *after* the first hard attempt.
        'max_retries': PRODUCTION_IMAGE_HARD_ERROR_ATTEMPTS - 1,
        'max_429_attempts': production_image_max_429_attempts(
            max_429_attempts),
    }
    if abort_check is not None:
        kwargs['abort_check'] = abort_check
    return kwargs
