"""Launch-derived budgets for classic local PDF extraction.

Responsibility
--------------
Resolve process-pool idle reuse from the launch probe; only explicit positive
operator/caller values constrain waiting.
Text has no system quota; callers may request an explicit positive output cut.

Entry points
------------
``resolve_classic_pdf_budget`` returns the effective process budget.
``bounded_pdf_pages`` preserves caller ranges; zero means every page.
``bounded_pdf_text_chars`` retains the explicit output policy.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from runtime_guards import resolve_resource_budget

PDF_WORKER_IDLE_HARD_MAX_SECONDS = 86_400
PDF_IMAGE_WIDTH_DEFAULT = 1_024
_CLASSIC_PDF_ENVIRONMENT_NAMES = (
    'TOFU_DEPLOYMENT_MODE',
    'TOFU_PDF_PROCESSES',
    'TOFU_PDF_MAX_PAGES',
    'TOFU_PDF_PARSE_TIMEOUT',
    'TOFU_PDF_WORKER_IDLE_SECONDS',
)


@dataclass(frozen=True, slots=True)
class ClassicPdfBudget:
    """Idle process reuse, complete-text default and execution timing."""

    processes: int
    max_pages: int
    max_text_chars: int
    timeout_seconds: int
    worker_idle_seconds: int


def resolve_classic_pdf_budget(
    environment: Mapping[str, str] | None = None,
) -> ClassicPdfBudget:
    """Resolve classic parsing limits from one environment fingerprint."""
    if environment is None:
        fingerprint = tuple(
            os.environ.get(name, '')
            for name in _CLASSIC_PDF_ENVIRONMENT_NAMES
        )
        return _resolve_cached_classic_pdf_budget(fingerprint)
    return _resolve_classic_pdf_budget_from_environment(environment)


@lru_cache(maxsize=64)
def _resolve_cached_classic_pdf_budget(
    fingerprint: tuple[str, ...],
) -> ClassicPdfBudget:
    environment = {
        name: value
        for name, value in zip(_CLASSIC_PDF_ENVIRONMENT_NAMES, fingerprint)
        if value != ''
    }
    return _resolve_classic_pdf_budget_from_environment(environment)


def _resolve_classic_pdf_budget_from_environment(
    environment: Mapping[str, str],
) -> ClassicPdfBudget:
    """Resolve idle reuse and explicit waiting from one environment mapping."""
    processes = resolve_resource_budget(
        'TOFU_PDF_PROCESSES', environment,
        minimum=1, maximum=2**31 - 1)
    max_pages = 0  # retired system page quota; caller ranges remain explicit
    try:
        timeout_seconds = int(environment.get('TOFU_PDF_PARSE_TIMEOUT', '') or 0)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError('TOFU_PDF_PARSE_TIMEOUT must be a nonnegative integer') from exc
    if timeout_seconds < 0:
        raise ValueError('TOFU_PDF_PARSE_TIMEOUT must be a nonnegative integer')
    if str(environment.get(
            'TOFU_PDF_WORKER_IDLE_SECONDS', '')).strip() == '0':
        worker_idle_seconds = 0
    else:
        worker_idle_seconds = resolve_resource_budget(
            'TOFU_PDF_WORKER_IDLE_SECONDS',
            environment,
            maximum=PDF_WORKER_IDLE_HARD_MAX_SECONDS,
        )
    return ClassicPdfBudget(
        processes=processes,
        max_pages=max_pages,
        max_text_chars=sys.maxsize,
        timeout_seconds=timeout_seconds,
        worker_idle_seconds=worker_idle_seconds,
    )


def classic_pdf_worker_idle_seconds(
    environment: Mapping[str, str] | None = None,
) -> float:
    """Return finite idle residency; explicit zero keeps the pool resident."""
    return float(resolve_classic_pdf_budget(environment).worker_idle_seconds)


def bounded_pdf_pages(requested: object, budget: ClassicPdfBudget) -> int:
    """Zero means all pages; positive caller ranges are not resource-clamped."""
    del budget
    try:
        return max(0, int(requested or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def bounded_pdf_text_chars(requested: object, budget: ClassicPdfBudget) -> int:
    """Zero/malformed means complete text; positive caller limits remain exact."""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value <= 0:
        return budget.max_text_chars
    return value


def bounded_pdf_image_count(requested: object) -> int | None:
    """None selects every detected figure; zero disables optional images."""
    if requested is None:
        return None
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    return max(0, value)


def bounded_pdf_image_width(requested: object) -> int:
    """Resolve malformed widths to the default; preserve explicit pixel width."""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value <= 0:
        value = PDF_IMAGE_WIDTH_DEFAULT
    return value


__all__ = [
    'ClassicPdfBudget',
    'PDF_IMAGE_WIDTH_DEFAULT',
    'PDF_WORKER_IDLE_HARD_MAX_SECONDS',
    'bounded_pdf_image_count',
    'bounded_pdf_image_width',
    'bounded_pdf_pages',
    'bounded_pdf_text_chars',
    'classic_pdf_worker_idle_seconds',
    'resolve_classic_pdf_budget',
]
