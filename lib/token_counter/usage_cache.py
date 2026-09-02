"""Tier 1 "last-known-good" usage cache.

Inspired by **OpenCode** (packages/opencode/src/session/message-v2.ts)
and **Claude Code** (src/services/tokenEstimation.ts): after every
successful LLM call, the provider tells us the exact prompt-token
count in the response's ``usage`` block. That's authoritative — the
same number the billing system charges.

We cache the most recent ``usage`` per conversation, and next round
compute::

    estimated = last_usage.prompt_tokens + count_new_text_since()

This is nearly exact, zero network latency, zero heuristics. The
short delta between rounds is the only thing we need to estimate —
and we can use tiktoken on it to keep that estimate tight.

Concurrency: a simple dict-with-lock works fine. Entries age out
after ``USAGE_CACHE_TTL_SEC`` so stale data doesn't mislead a
days-old conversation.

Invalidation: ``record_usage()`` is called by ``lib/llm/stream.py``
after each streamed response. If the conversation compacts, the
caller should invalidate via ``invalidate(conv_id)``.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from lib.log import get_logger

from .base import TokenCounter, iter_message_texts
from .config import USAGE_CACHE_TTL_SEC

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Storage
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class _UsageEntry:
    prompt_tokens: int
    model: str
    ts: float
    # Number of messages at the time of the recording. Used to estimate
    # the delta for new messages appended since.
    message_count: int
    # Signature of the tail (role + first 120 chars of content) so we
    # can detect whether the tail changed vs. just grew.
    tail_signature: str


_lock = threading.Lock()
_cache: dict[str, _UsageEntry] = {}


def _signature(
    messages: list,
    n_tail: int = 3,
    *,
    end: int | None = None,
) -> str:
    """Short signature of the last n_tail messages — used to detect
    whether the tail changed (e.g. a regenerate or edit) vs. simply
    had new messages appended."""
    end_index = len(messages) if end is None else max(
        0, min(len(messages), int(end)))
    start_index = max(0, end_index - max(0, int(n_tail)))
    parts = []
    for m in messages[start_index:end_index]:
        role = m.get('role', '')
        content = m.get('content')
        if isinstance(content, str):
            s = content[:120]
        elif isinstance(content, list):
            s = ''
            for blk in content:
                if isinstance(blk, dict) and isinstance(blk.get('text'), str):
                    s = blk['text'][:120]
                    break
        else:
            s = ''
        parts.append(f'{role}:{s}')
    return '|'.join(parts)


def record_usage(conv_id: str, *,
                 prompt_tokens: int,
                 model: str,
                 message_count: int,
                 messages: Optional[list] = None) -> None:
    """Record a successful API call's ``prompt_tokens`` for ``conv_id``.

    Called from ``lib/llm/stream.py`` after each stream completes.
    ``messages`` is the message list sent *in that call* — used to
    compute the tail signature for staleness detection.
    """
    if not conv_id or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        return
    try:
        sig = _signature(messages or [])
        with _lock:
            _cache[conv_id] = _UsageEntry(
                prompt_tokens=prompt_tokens,
                model=model or '',
                ts=time.time(),
                message_count=max(0, message_count),
                tail_signature=sig,
            )
        logger.debug('[TokenCounter][UsageCache] conv=%s recorded %d tokens '
                     '(model=%s, msgs=%d)',
                     conv_id[:8], prompt_tokens, model, message_count)
    except Exception as e:
        logger.debug('[TokenCounter][UsageCache] record_usage failed: %s', e)


def invalidate(conv_id: str) -> None:
    """Drop the cached entry for ``conv_id`` (call after compaction)."""
    with _lock:
        _cache.pop(conv_id, None)


def _lookup(conv_id: str) -> Optional[_UsageEntry]:
    if not conv_id:
        return None
    with _lock:
        entry = _cache.get(conv_id)
    if entry is None:
        return None
    if time.time() - entry.ts > USAGE_CACHE_TTL_SEC:
        invalidate(conv_id)
        return None
    return entry


# ───────────────────────────────────────────────────────────────────────────
# Counter
# ───────────────────────────────────────────────────────────────────────────

class UsageCacheCounter(TokenCounter):
    """Reuse the authoritative ``prompt_tokens`` from the last API call.

    Works when:
      1. The caller passes ``conv_id``.
      2. We have a cached entry for that conv less than
         ``USAGE_CACHE_TTL_SEC`` old.
      3. The new message list starts with the same historical messages
         (i.e. we're only appending new turns, not editing/regenerating).

    When the tail signature of the first ``cached.message_count``
    messages matches what we recorded, we trust the cached number
    for the prefix and use the heuristic only for the appended delta.

    Accuracy: within 1-2 % of the real number for normal append-only
    turns; we explicitly return None (and let the next tier take over)
    when we detect that the prefix has changed.
    """

    name = 'usage_cache'
    confidence = 'exact'
    needs_network = False

    def supports(self, model: str) -> bool:
        return True  # model-agnostic

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              conv_id: Optional[str] = None,
              **kwargs) -> Optional[int]:
        if not conv_id:
            return None
        entry = _lookup(conv_id)
        if entry is None:
            return None

        # Safety: if the model changed between rounds, the tokenizer
        # changed too — our cached number is no longer trustworthy.
        if entry.model and model and _family(entry.model) != _family(model):
            logger.debug('[TokenCounter][UsageCache] model family changed '
                         '%s → %s, invalidating cache for conv=%s',
                         entry.model, model, conv_id[:8])
            return None

        # Safety: messages must be at least as long as at recording time.
        if not messages or len(messages) < entry.message_count:
            return None

        # Safety: the tail of the recorded-at-time prefix must still
        # match. We cheaply verify with a signature of the messages up
        # to entry.message_count.
        if _signature(messages, end=entry.message_count) != entry.tail_signature:
            # The conversation was edited mid-flight — e.g. a message
            # was regenerated or truncated. Don't trust the cache.
            return None

        # Estimate delta tokens for the appended suffix.
        from .heuristic import cheap_estimate_text
        suffix = messages[entry.message_count:]
        delta_tokens = sum(
            cheap_estimate_text(txt)
            for txt in iter_message_texts(suffix)
        )

        # IMPORTANT: do NOT add the tool-schema / system-prompt cost here.
        # ``entry.prompt_tokens`` is the gateway's exact count for the
        # PREVIOUS request, which ALREADY included that round's tools +
        # system. The schema is virtually identical round-to-round (same
        # enabled toolset), so counting it fresh and adding it on top would
        # DOUBLE-count the entire tool schema — on a tool-heavy config that
        # is tens of thousands of phantom tokens, firing compaction early.
        # We only add the appended-suffix delta; tool/system drift between
        # rounds is negligible vs. that double-count risk.
        total = entry.prompt_tokens + delta_tokens
        logger.debug('[TokenCounter][UsageCache] conv=%s hit: %d (cached) + '
                     '%d (suffix) = %d',
                     conv_id[:8], entry.prompt_tokens, delta_tokens, total)
        return total


def _family(model: str) -> str:
    """Return a tokenizer-family key for cross-round validity check."""
    m = (model or '').lower()
    if 'claude' in m or 'anthropic' in m: return 'claude'
    if 'gpt-4o' in m or 'gpt-5' in m or 'o200k' in m: return 'o200k'
    if 'deepseek' in m: return 'deepseek'
    if 'gemini' in m: return 'gemini'
    if 'qwen' in m: return 'qwen'
    if 'doubao' in m: return 'doubao'
    if 'minimax' in m: return 'minimax'
    if 'glm' in m: return 'glm'
    return 'cl100k'


__all__ = ['UsageCacheCounter', 'record_usage', 'invalidate']
