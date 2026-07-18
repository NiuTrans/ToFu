"""lib/memory/prefetch/_config.py — Tunables + resolved flags.

All prefetch hyperparameters and the feature-flag / deadline resolution
live here so the pipeline sub-modules (_query / _shortlist / _rerank /
_inject / _run) share a single source of truth without import cycles.

Tunables (all change requires user approval per CLAUDE.md §10 if adjusted
at runtime — the defaults below were agreed in the planning discussion
before implementation).
"""
from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


PREFETCH_BM25_TOP_N       = 40     # coarse-stage candidate pool — BM25's top-40 reliably
                                   # contains the true positives; 80 just doubled the
                                   # reranker payload (~50KB) for a step that drops most of it.
PREFETCH_MAX_INJECTED     = 5      # hard cap on memories injected
PREFETCH_MAX_BYTES        = 12_000 # hard cap on injected body bytes — sized so
                                   # PREFETCH_MAX_INJECTED median-length bodies (~2KB) fit
                                   # as FULL bodies instead of being truncated to titles.
PREFETCH_RECENT_TURNS_K   = 3      # number of user+assistant pairs used for query
PREFETCH_MIN_CANDIDATES   = 2      # below this, skip cheap-LLM step
PREFETCH_BODY_PREVIEW_LEN = 500    # chars of body shown to cheap model

# Bytes cap per-memory when building the context for the cheap model
_SUMMARY_BODY_CAP = 800

# Total bytes of recent conversational surface used as the query.
_MAX_QUERY_CHARS = 4_000

# ── Rerank wall-clock deadline (§10.1 hyperparameter) ──
# Hard upper bound on how long the cheap-LLM rerank may block the turn's
# critical path (round 0, before the main model's first token). On timeout
# we inject NOTHING — matching the existing no-fallback policy: we would
# rather add no memory than stall time-to-first-token or splice a noisy
# BM25 top-K. This is a WALL-CLOCK abandon, not dispatch_chat's per-attempt
# timeout: dispatch_chat cycles up to its full total budget (~90s for
# 'cheap') on 429 contention, so a per-attempt timeout alone cannot bound
# the call. We therefore run the rerank in a daemon worker and abandon it
# on deadline. 0 disables the bound (legacy behaviour: block indefinitely).
# Override per-deployment with MEMORY_PREFETCH_DEADLINE_MS.
try:
    PREFETCH_DEADLINE_MS = int(os.environ.get('MEMORY_PREFETCH_DEADLINE_MS', '800'))
    if PREFETCH_DEADLINE_MS < 0:
        PREFETCH_DEADLINE_MS = 0
except (ValueError, TypeError) as _e:
    logger.debug('[MemPrefetch] MEMORY_PREFETCH_DEADLINE_MS parse failed, using default: %s', _e)
    PREFETCH_DEADLINE_MS = 800

# Respect feature flag in the normal way (env > features.json > default).
try:
    from lib import _resolve_feature_flag  # type: ignore
    PREFETCH_ENABLED = _resolve_feature_flag('MEMORY_PREFETCH',
                                             'memory_prefetch', True)
except Exception as _e:  # pragma: no cover — defensive
    logger.warning('[MemPrefetch] Could not resolve feature flag: %s', _e)
    PREFETCH_ENABLED = True
