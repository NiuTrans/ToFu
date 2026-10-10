"""Canonical process and extraction budgets for local knowledge.

The launch-time resource manifest owns adaptive concurrency/page defaults.
Per-image validation remains explicit. Asset counts and
aggregate raw bytes never truncate a corpus; extraction owns a temporary spool.
Paid vision and enrichment lifetimes keep their separate authority.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from lib.log import get_logger
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

_MIB = 1024 * 1024


@dataclass(frozen=True, slots=True)
class KnowledgeVisualBudget:
    """Per-image validation policy; no corpus page or asset quota."""

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
    """Resolve per-image encoded/decoded validation without traversal quotas."""
    env = os.environ if environment is None else environment
    return KnowledgeVisualBudget(
        max_asset_bytes=_bounded_environment_int(
            'TOFU_KNOWLEDGE_MAX_ASSET_BYTES', 25 * _MIB,
            64 * 1024, 100 * _MIB, env),
        max_image_pixels=_bounded_environment_int(
            'TOFU_KNOWLEDGE_MAX_IMAGE_PIXELS', 40_000_000,
            1_000_000, 100_000_000, env),
    )


def knowledge_enrichment_workers() -> int:
    """Idle worker reuse target; independent owners run on demand."""
    return resolve_resource_budget(
        'TOFU_KNOWLEDGE_ENRICH_WORKERS', minimum=1, maximum=16)


def knowledge_enrichment_worker_idle_seconds() -> float:
    """Idle residency before a reconstructible enrichment worker retires."""
    if os.environ.get(
            'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', '').strip() == '0':
        return 0.0
    return float(resolve_resource_budget(
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', maximum=86_400))


__all__ = [
    'KnowledgeVisualBudget',
    'knowledge_enrichment_worker_idle_seconds',
    'knowledge_enrichment_workers',
    'resolve_knowledge_visual_budget',
]
