"""Launch-derived budgets for classic local PDF extraction.

Responsibility
--------------
Resolve process-pool residency, unfinished work, page CPU, text output, and
wall-clock ceilings from the one launch-time system-resource probe.  Callers
may lower limits; operator overrides remain inside explicit hard ceilings.

Entry points
------------
``resolve_classic_pdf_budget`` returns the effective process budget.
``bounded_pdf_pages`` and ``bounded_pdf_text_chars`` clamp request values.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache

from runtime_guards import resolve_resource_budget


_MIB = 1024 * 1024
PDF_PROCESS_HARD_MAX = 16
PDF_PARSE_CAPACITY_HARD_MAX = 64
PDF_PAGE_HARD_MAX = 4_096
PDF_TEXT_HARD_MAX_MIB = 64
PDF_TIMEOUT_HARD_MAX_SECONDS = 3_600
PDF_WORKER_IDLE_HARD_MAX_SECONDS = 86_400
PDF_IMAGE_HARD_MAX = 64
PDF_IMAGE_WIDTH_DEFAULT = 1_024
PDF_IMAGE_WIDTH_HARD_MAX = 2_048
_CLASSIC_PDF_ENVIRONMENT_NAMES = (
    'TOFU_DEPLOYMENT_MODE',
    'TOFU_PDF_PROCESSES',
    'TOFU_PDF_PARSE_CAPACITY',
    'TOFU_PDF_MAX_PAGES',
    'TOFU_PDF_MAX_TEXT_MIB',
    'TOFU_PDF_PARSE_TIMEOUT',
    'TOFU_PDF_WORKER_IDLE_SECONDS',
)


@dataclass(frozen=True, slots=True)
class ClassicPdfBudget:
    """Finite compressed-input concurrency and derived-output envelope."""

    processes: int
    unfinished_capacity: int
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
    """Apply domain hard ceilings to one explicit environment mapping."""
    processes = resolve_resource_budget(
        'TOFU_PDF_PROCESSES', environment,
        minimum=1, maximum=PDF_PROCESS_HARD_MAX)
    unfinished_capacity = resolve_resource_budget(
        'TOFU_PDF_PARSE_CAPACITY', environment,
        minimum=1, maximum=PDF_PARSE_CAPACITY_HARD_MAX)
    max_pages = resolve_resource_budget(
        'TOFU_PDF_MAX_PAGES', environment,
        minimum=1, maximum=PDF_PAGE_HARD_MAX)
    max_text_mib = resolve_resource_budget(
        'TOFU_PDF_MAX_TEXT_MIB', environment,
        minimum=1, maximum=PDF_TEXT_HARD_MAX_MIB)
    timeout_seconds = resolve_resource_budget(
        'TOFU_PDF_PARSE_TIMEOUT', environment,
        minimum=1, maximum=PDF_TIMEOUT_HARD_MAX_SECONDS)
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
        unfinished_capacity=max(processes, unfinished_capacity),
        max_pages=max_pages,
        max_text_chars=max_text_mib * _MIB,
        timeout_seconds=timeout_seconds,
        worker_idle_seconds=worker_idle_seconds,
    )


def classic_pdf_worker_idle_seconds(
    environment: Mapping[str, str] | None = None,
) -> float:
    """Return finite idle residency; explicit zero keeps the pool resident."""
    return float(resolve_classic_pdf_budget(environment).worker_idle_seconds)


def bounded_pdf_pages(requested: object, budget: ClassicPdfBudget) -> int:
    """Use the policy default for zero/malformed input; otherwise clamp."""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value <= 0:
        return budget.max_pages
    return min(value, budget.max_pages)


def bounded_pdf_text_chars(requested: object, budget: ClassicPdfBudget) -> int:
    """Use the policy default for zero/malformed input; otherwise clamp."""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value <= 0:
        return budget.max_text_chars
    return min(value, budget.max_text_chars)


def bounded_pdf_image_count(requested: object) -> int:
    """Preserve zero as image-disable while bounding retained image objects."""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    return max(0, min(PDF_IMAGE_HARD_MAX, value))


def bounded_pdf_image_width(requested: object) -> int:
    """Resolve malformed widths to the default and clamp pixel expansion."""
    try:
        value = int(requested or 0)
    except (TypeError, ValueError, OverflowError):
        value = 0
    if value <= 0:
        value = PDF_IMAGE_WIDTH_DEFAULT
    return min(PDF_IMAGE_WIDTH_HARD_MAX, value)


__all__ = [
    'ClassicPdfBudget',
    'PDF_IMAGE_HARD_MAX',
    'PDF_IMAGE_WIDTH_DEFAULT',
    'PDF_IMAGE_WIDTH_HARD_MAX',
    'PDF_PAGE_HARD_MAX',
    'PDF_PARSE_CAPACITY_HARD_MAX',
    'PDF_PROCESS_HARD_MAX',
    'PDF_TEXT_HARD_MAX_MIB',
    'PDF_TIMEOUT_HARD_MAX_SECONDS',
    'PDF_WORKER_IDLE_HARD_MAX_SECONDS',
    'bounded_pdf_image_count',
    'bounded_pdf_image_width',
    'bounded_pdf_pages',
    'bounded_pdf_text_chars',
    'classic_pdf_worker_idle_seconds',
    'resolve_classic_pdf_budget',
]
