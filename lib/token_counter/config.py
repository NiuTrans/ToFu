"""Environment-driven configuration for the token counter.

All knobs live here so tests can monkey-patch from one place.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)


def _env_float(key: str, default: float) -> float:
    try:
        v = os.environ.get(key)
        if v is None or v == '':
            return default
        return float(v)
    except (TypeError, ValueError) as e:
        logger.debug('[token_counter.config] _env_float(%s) failed: %s', key, e)
        return default


def _env_str(key: str, default: str) -> str:
    v = os.environ.get(key)
    return v.strip() if isinstance(v, str) and v.strip() else default


# ───── forced-mode override ─────────────────────────────────────────────

MODE = _env_str('TOFU_TOKEN_COUNTER', 'auto').lower()
"""'auto' | 'api' | 'tiktoken' | 'heuristic' | 'usage_cache'.

'auto' (default) → walk the resolver's ordered list per-model.
Any other value forces that specific backend (for debugging / A-B).
"""


# ───── Tier 3 upstream API ──────────────────────────────────────────────

API_TIMEOUT = _env_float('TOFU_TOKEN_COUNTER_API_TIMEOUT', 10.0)
"""Timeout (seconds) for Anthropic / Gemini count_tokens calls."""

API_THRESHOLD = _env_float('TOFU_TOKEN_COUNTER_API_THRESHOLD', 0.50)
"""Only call the expensive API tier when the cheap estimate exceeds
this fraction of the model's context limit.

At 50 %+ the cheap estimate is close enough to the limit that the
extra 300-1000 ms round trip is worth paying for bit-exact numbers.
Below that, tiktoken / heuristic already answer "nowhere near full".
"""


# ───── Tier 1 usage-cache (inspired by OpenCode's MessageV2.tokens) ─────

USAGE_CACHE_TTL_SEC = _env_float('TOFU_TOKEN_COUNTER_CACHE_TTL', 3600.0)
"""How long a recorded ``usage`` sample stays authoritative. After
this, we treat it as stale and recompute via a lower tier."""


__all__ = [
    'MODE', 'API_TIMEOUT', 'API_THRESHOLD', 'USAGE_CACHE_TTL_SEC',
]
