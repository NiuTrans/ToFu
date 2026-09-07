"""Canonical process and extraction budgets for local knowledge.

The launch-time resource manifest owns adaptive concurrency/page defaults.
This module prevents environment overrides from creating unbounded paid vision
calls, resident owner schedulers, PDF traversal, or visual-byte amplification.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from lib.log import get_logger
from lib.pdf_parser.policy import resolve_classic_pdf_budget
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class KnowledgeVisualBudget:
    """Finite local visual extraction envelope for one document."""

    pdf_max_pages: int
    pdf_ocr_max_pages: int
    max_assets: int
    max_total_bytes: int
    max_asset_bytes: int
    max_image_pixels: int


def _bounded_environment_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
    environment: Mapping[str, str],
) -> int:
    raw = environment.get(name, str(default))
    try:
        return max(minimum, min(int(raw), maximum))
    except (TypeError, ValueError, OverflowError) as exc:
        logger.debug('[Knowledge] invalid %s=%r; using %d: %s',
                     name, raw, default, exc)
        return default


def resolve_knowledge_visual_budget(
    environment: Mapping[str, str] | None = None,
) -> KnowledgeVisualBudget:
    """Resolve one document's visual envelope from the classic page policy."""
    env = os.environ if environment is None else environment
    classic_pdf = resolve_classic_pdf_budget(environment)
    return KnowledgeVisualBudget(
        pdf_max_pages=min(
            classic_pdf.max_pages,
            _bounded_environment_int(
                'TOFU_KNOWLEDGE_VISUAL_MAX_PAGES', 80, 1, 500, env),
        ),
        pdf_ocr_max_pages=min(
            classic_pdf.max_pages,
            _bounded_environment_int(
                'TOFU_KNOWLEDGE_OCR_MAX_PAGES', 80, 1, 500, env),
        ),
        max_assets=_bounded_environment_int(
            'TOFU_KNOWLEDGE_MAX_VISUAL_ASSETS', 160, 1, 1_000, env),
        max_total_bytes=_bounded_environment_int(
            'TOFU_KNOWLEDGE_MAX_VISUAL_BYTES', 160 * _MIB,
            _MIB, 1024 * _MIB, env),
        max_asset_bytes=_bounded_environment_int(
            'TOFU_KNOWLEDGE_MAX_ASSET_BYTES', 25 * _MIB,
            64 * 1024, 100 * _MIB, env),
        max_image_pixels=_bounded_environment_int(
            'TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS', 40_000_000,
            1_000_000, 100_000_000, env),
    )


def knowledge_enrichment_workers() -> int:
    """Maximum concurrent asset descriptions across every corpus owner."""
    return resolve_resource_budget(
        'TOFU_KNOWLEDGE_ENRICH_WORKERS', minimum=1, maximum=16)


def knowledge_enrichment_owner_capacity() -> int:
    """Maximum owners retained by the process-local fair scheduler."""
    return resolve_resource_budget(
        'TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY', minimum=1, maximum=512)


def knowledge_enrichment_worker_idle_seconds() -> float:
    """Idle residency before a reconstructible enrichment worker retires."""
    if os.environ.get(
            'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', '').strip() == '0':
        return 0.0
    return float(resolve_resource_budget(
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', maximum=86_400))


__all__ = [
    'KnowledgeVisualBudget',
    'knowledge_enrichment_owner_capacity',
    'knowledge_enrichment_worker_idle_seconds',
    'knowledge_enrichment_workers',
    'resolve_knowledge_visual_budget',
]
