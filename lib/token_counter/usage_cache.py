"""Tier 1 "last-known-good" usage cache.

After every successful LLM call, the provider tells us the exact
prompt-token count in the response's ``usage`` block. That's
authoritative — the same number the billing system charges.

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

import hashlib
import json
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
    # Full content digest of the measured prefix (legacy field name).
    tail_signature: str
    # Provider-reported hidden reasoning tokens produced by the successful
    # call but not present in that call's input count. They become input only
    # if the returned opaque reasoning block is appended and replayed on the
    # next request (Responses encrypted reasoning / Claude redacted thinking).
    opaque_replay_tokens: int = 0
    envelope_signature: str = ''


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


def _signature(messages: list, *, end: int | None = None) -> str:
    """Digest the complete prefix, without copying it or tokenizing text.

    Mutable containers cannot authorize reuse by identity or by a sampled tail:
    an early edit (including tool/media/native reasoning fields) changes input.
    Retain only a fixed-size digest, never historical prompt payloads.
    """
    digest = hashlib.sha256()
    end_index = len(messages) if end is None else end
    # Encoder configuration is request-local; each encode retains the same
    # canonical bytes and failure behavior without rebuilding it per message.
    encode = json.JSONEncoder(sort_keys=True, ensure_ascii=False,
                              separators=(',', ':')).encode
    for index in range(end_index):
        encoded = encode(messages[index])
        digest.update(encoded.encode('utf-8'))
        digest.update(b'\0')
    return digest.hexdigest()


def _envelope_signature(system: Any, tools: Any) -> str:
    return _signature([{'system': system or None, 'tools': tools or None}])


def record_usage(conv_id: str, *,
                 prompt_tokens: int,
                 model: str,
                 message_count: int,
                 messages: Optional[list] = None,
                 opaque_replay_tokens: int = 0,
                 system: Any = None, tools: Any = None) -> None:
    """Record a successful API call's ``prompt_tokens`` for ``conv_id``.

    Called from ``lib/llm/stream.py`` after each stream completes.
    ``messages`` is the message list sent *in that call* — used to
    compute the tail signature for staleness detection.
    """
    if not conv_id or not isinstance(prompt_tokens, int) or prompt_tokens < 0:
        return
    try:
        if (not isinstance(messages, list) or not messages
                or message_count != len(messages) or not model
                or len(str(model)) > _MAX_MODEL_CHARS):
            invalidate(conv_id)
            return
        sig = _signature(messages)
        envelope_sig = _envelope_signature(system, tools)
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
                envelope_signature=envelope_sig,
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

    When the full digest of the first ``cached.message_count``
    messages and the system/tools envelope match what we recorded, we trust the cached number
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

        # Measurements bind the exact model and request envelope, not a
        # tokenizer family or a conversation-wide historical yardstick.
        if not model or entry.model != model:
            return None
        if not messages or len(messages) < entry.message_count:
            return None
        try:
            if (entry.envelope_signature != _envelope_signature(system, tools)
                    or _signature(messages, end=entry.message_count)
                    != entry.tail_signature):
                return None
        except (TypeError, ValueError, OverflowError):
            return None

        # Estimate delta tokens for the appended suffix.
        from .heuristic import cheap_estimate_text
        suffix = messages[entry.message_count:]
        from .base import count_images, _IMAGE_TOKENS_DEFAULT
        delta_tokens = sum(
            cheap_estimate_text(txt)
            for txt in iter_message_texts(suffix)
        )
        delta_tokens += count_images(suffix) * _IMAGE_TOKENS_DEFAULT
        opaque_replay_tokens = (
            entry.opaque_replay_tokens
            if _suffix_replays_opaque_reasoning(suffix) else 0
        )

        # The validated envelope is already included in the measured prefix.
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
