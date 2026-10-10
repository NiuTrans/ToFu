"""lib/memory/prefetch/_config.py — Tunables + resolved flags.

All prefetch hyperparameters and the feature-flag resolution live here so the
local pipeline sub-modules (_query / _shortlist / _inject / _run) share a
single source of truth without import cycles.

Tunables (all change requires user approval per CLAUDE.md §10 if adjusted
at runtime — the defaults below were agreed in the planning discussion
before implementation).
"""
from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


PREFETCH_BM25_TOP_N       = 20     # metadata-only local candidate pool
PREFETCH_MAX_INJECTED     = 2      # precision-first automatic evidence cap
PREFETCH_MAX_TOKENS       = 1_500  # whole rendered <relevant_memories> block
PREFETCH_MAX_BYTES        = 6_000  # defensive wire cap if token counting fails
PREFETCH_RECENT_TURNS_K   = 3      # profile-consolidation history helper
# Defensive cap shared by current-request and profile-history extraction.
_MAX_QUERY_CHARS = 4_000

# Respect feature flag in the normal way (env > features.json > default).
try:
    from lib import _resolve_feature_flag  # type: ignore
    PREFETCH_ENABLED = _resolve_feature_flag('MEMORY_PREFETCH',
                                             'memory_prefetch', True)
except Exception as _e:  # pragma: no cover — defensive
    logger.warning('[MemPrefetch] Could not resolve feature flag: %s', _e)
    PREFETCH_ENABLED = True
