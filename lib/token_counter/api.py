"""Public façade for the token_counter package.

End users (compaction pipeline, UI /api/tokens/count, etc.) should
import from ``lib.token_counter`` and never touch
individual backend files.

Public entry points:
  :func:`count_tokens` — full request count (messages + system + tools).
  :func:`count_text`   — fast single-string count.
  :func:`record_usage` — feed the last response's usage into the cache.
  :func:`invalidate`   — drop the cache entry for a conv (after compact).
"""

from __future__ import annotations

import time
from typing import Any, Optional

from lib.log import get_logger

from .base import CountResult
from .config import API_THRESHOLD, MODE
from .heuristic import cheap_estimate, cheap_estimate_text
from .resolver import force_backend, resolve
from .tiktoken_counter import count_text as _tiktoken_count_text
from .usage_cache import invalidate, record_usage

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# count_tokens
# ───────────────────────────────────────────────────────────────────────────

def count_tokens(messages: list, *,
                 model: str,
                 system: Any = None,
                 tools: Any = None,
                 conv_id: Optional[str] = None,
                 context_limit: Optional[int] = None,
                 mode: str = 'auto',
                 api_base_url: Optional[str] = None,
                 api_key: Optional[str] = None) -> dict:
    """Count tokens for a full request. **The public entry point.**

    Args:
        messages: OpenAI-shape message list.
        model:    Target model id (drives backend choice).
        system:   Optional system prompt (string or content blocks).
        tools:    Optional tool schema list (OpenAI function format).
        conv_id:  Conversation id. When provided, the usage-cache
                  backend can reuse the last response's exact count.
        context_limit: Model context window in tokens. When set,
                  network tiers are skipped if the cheap estimate is
                  below ``API_THRESHOLD × context_limit`` — avoids
                  the round-trip for small contexts.
        mode:     Force a specific backend: ``'auto'``, ``'api'``,
                  ``'anthropic'``, ``'gemini'``, ``'tiktoken'``,
                  ``'deepseek'``, ``'hf'``, ``'usage_cache'``,
                  ``'heuristic'``. Defaults to env-configured MODE.
        api_base_url / api_key: Needed by the upstream API backends
                  (Anthropic / Gemini). Ignored by local backends.

    Returns:
        ``{'tokens': int, 'method': str, 'elapsed_ms': int,
          'confidence': 'exact'|'good'|'approx'}``.

    The function never raises — it always returns at least the
    heuristic count.
    """
    t0 = time.time()
    effective_mode = (mode or 'auto').lower()
    if effective_mode == 'auto':
        effective_mode = MODE

    # --- Which backends do we try? ---
    if effective_mode == 'auto':
        candidates = resolve(model)
    else:
        candidates = force_backend(effective_mode)

    # --- Cheap pre-estimate, used to gate network tiers ---
    cheap_pre = cheap_estimate(messages, system=system, tools=tools)

    # --- Walk the list ---
    kwargs = {
        'system': system,
        'tools': tools,
        'conv_id': conv_id,
        'api_base_url': api_base_url,
        'api_key': api_key,
        'context_limit': context_limit,
    }
    for counter in candidates:
        # Skip network tiers when the cheap estimate is far below the
        # context limit (saves the round-trip).
        if counter.needs_network and context_limit and effective_mode == 'auto':
            if cheap_pre < context_limit * API_THRESHOLD:
                logger.debug(
                    '[TokenCounter] Skipping %s (needs network, cheap=%d < %.0f%% of %d)',
                    counter.name, cheap_pre, API_THRESHOLD*100, context_limit)
                continue

        if not counter.supports(model):
            continue

        try:
            n = counter.count(messages, model=model, **kwargs)
        except Exception as e:
            # TokenCounter.count() must never raise, but guard anyway.
            logger.warning('[TokenCounter] Backend %s raised: %s',
                           counter.name, e, exc_info=True)
            n = None
        if n is not None and n >= 0:
            return CountResult(
                tokens=n,
                method=counter.name,
                elapsed_ms=int((time.time() - t0) * 1000),
                confidence=counter.confidence,
            ).as_dict()

    # Should be impossible (heuristic always succeeds), but defend anyway.
    return CountResult(
        tokens=cheap_pre,
        method='heuristic',
        elapsed_ms=int((time.time() - t0) * 1000),
        confidence='approx',
    ).as_dict()


# ───────────────────────────────────────────────────────────────────────────
# count_text — fast single-string API
# ───────────────────────────────────────────────────────────────────────────

def count_text(text: str, *, model: str = '') -> int:
    """Cheap count for a single text blob. Uses tiktoken when available,
    heuristic otherwise. Used by code paths that count individual tool
    results / args without building a full message list.
    """
    if not text:
        return 0
    # Try tiktoken first (exact for OpenAI, good for others)
    n = _tiktoken_count_text(text, model)
    if n > 0:
        return n
    return cheap_estimate_text(text)


__all__ = [
    'count_tokens', 'count_text',
    'record_usage', 'invalidate',
]
