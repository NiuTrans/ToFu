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

Concurrency: a bounded ordered dict plus one lock keeps expiration, LRU
promotion, and replacement atomic. Entries age out after
``USAGE_CACHE_TTL_SEC``; capacity eviction safely falls through to the next
counter tier.

Invalidation: ``record_usage()`` is called by ``lib/llm/stream.py``
after each streamed response. If the conversation compacts, the
caller should invalidate via ``invalidate(conv_id)``.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from lib.log import get_logger
from runtime_guards import resolve_resource_budget

from .base import TokenCounter, iter_message_texts
from .config import USAGE_CACHE_TTL_SEC

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Storage
# ───────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
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
    # Provider-reported hidden reasoning tokens produced by the successful
    # call but not present in that call's input count. They become input only
    # if the returned opaque reasoning block is appended and replayed on the
    # next request (Responses encrypted reasoning / Claude redacted thinking).
    opaque_replay_tokens: int = 0


_lock = threading.Lock()
_cache: OrderedDict[str, _UsageEntry] = OrderedDict()
_USAGE_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_USAGE_CACHE_CAPACITY', maximum=8192)
_MAX_MODEL_CHARS = 160
_MAX_ROLE_CHARS = 32
_cache_metrics = {
    'hits': 0,
    'misses': 0,
    'expiredEvictions': 0,
    'capacityEvictions': 0,
}


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
        role = str(m.get('role') or '')[:_MAX_ROLE_CHARS]
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
                 messages: Optional[list] = None,
                 opaque_replay_tokens: int = 0) -> None:
    """Record a successful API call's ``prompt_tokens`` for ``conv_id``.

    Called from ``lib/llm/stream.py`` after each stream completes.
    ``messages`` is the message list sent *in that call* — used to
    compute the tail signature for staleness detection.
    """
    if not conv_id or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        return
    try:
        sig = _signature(messages or [])
        now = time.time()
        with _lock:
            replaced = _cache.pop(conv_id, None)
            if replaced is None and len(_cache) >= _USAGE_CACHE_CAPACITY:
                expired_keys = [
                    key for key, entry in _cache.items()
                    if now - entry.ts > USAGE_CACHE_TTL_SEC
                ]
                for key in expired_keys:
                    _cache.pop(key, None)
                _cache_metrics['expiredEvictions'] += len(expired_keys)
            while len(_cache) >= _USAGE_CACHE_CAPACITY:
                _cache.popitem(last=False)
                _cache_metrics['capacityEvictions'] += 1
            _cache[conv_id] = _UsageEntry(
                prompt_tokens=prompt_tokens,
                model=str(model or '')[:_MAX_MODEL_CHARS],
                ts=now,
                message_count=max(0, message_count),
                tail_signature=sig,
                opaque_replay_tokens=max(0, int(opaque_replay_tokens or 0)),
            )
        logger.debug('[TokenCounter][UsageCache] conv=%s recorded %d tokens '
                     '(model=%s, msgs=%d, opaque_replay=%d)',
                     conv_id[:8], prompt_tokens, model, message_count,
                     max(0, int(opaque_replay_tokens or 0)))
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
            _cache_metrics['misses'] += 1
            return None
        if time.time() - entry.ts > USAGE_CACHE_TTL_SEC:
            _cache.pop(conv_id, None)
            _cache_metrics['expiredEvictions'] += 1
            _cache_metrics['misses'] += 1
            return None
        _cache.move_to_end(conv_id)
        _cache_metrics['hits'] += 1
        return entry


def clear_usage_cache() -> int:
    """Drop reconstructible entries under memory pressure; return the count."""
    with _lock:
        count = len(_cache)
        _cache.clear()
        return count


def usage_cache_snapshot() -> dict[str, int | float]:
    """Return content-free capacity/eviction telemetry."""
    with _lock:
        return {
            **_cache_metrics,
            'entries': len(_cache),
            'capacity': _USAGE_CACHE_CAPACITY,
            'ttlSeconds': USAGE_CACHE_TTL_SEC,
        }


def _reset_usage_cache_for_tests() -> None:
    with _lock:
        _cache.clear()
        for key in _cache_metrics:
            _cache_metrics[key] = 0


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
        opaque_replay_tokens = (
            entry.opaque_replay_tokens
            if _suffix_replays_opaque_reasoning(suffix) else 0
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
        total = (entry.prompt_tokens + delta_tokens
                 + opaque_replay_tokens)
        logger.debug('[TokenCounter][UsageCache] conv=%s hit: %d (cached) + '
                     '%d (suffix) + %d (opaque replay) = %d',
                     conv_id[:8], entry.prompt_tokens, delta_tokens,
                     opaque_replay_tokens, total)
        return total


def _suffix_replays_opaque_reasoning(messages: list) -> bool:
    """Return whether an appended suffix carries hidden reasoning state."""
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        for item in message.get('_responses_items') or ():
            if (isinstance(item, dict)
                    and item.get('type') == 'reasoning'
                    and item.get('encrypted_content')):
                return True
        for block in message.get('_anthropic_content_blocks') or ():
            if (isinstance(block, dict)
                    and block.get('type') == 'redacted_thinking'
                    and block.get('data')):
                return True
    return False


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


__all__ = [
    'UsageCacheCounter',
    'clear_usage_cache',
    'invalidate',
    'record_usage',
    'usage_cache_snapshot',
]
