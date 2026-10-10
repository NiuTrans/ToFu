"""Tier 2 tiktoken backend (OpenAI official tokenizer).

Exact for: GPT-4o, GPT-4, GPT-3.5, o1/o3/o4.
Close-enough for: Qwen, MiniMax, Doubao, GLM, Gemini (BPE with similar
vocabularies — typically within ±10 %).

Encoding choice:
  ``o200k_base`` → GPT-4o, GPT-5, o-series
  ``cl100k_base`` → GPT-4 / 3.5 / everything else
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from runtime_guards import resolve_resource_budget

from lib.log import get_logger

from .base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX,
    TokenCounter,
    count_images,
    iter_message_texts,
    _IMAGE_TOKENS_DEFAULT,
    _STRUCTURAL_OVERHEAD_TOKENS,
)
from .heuristic import cheap_estimate_text as _cheap_estimate_text

logger = get_logger(__name__)


_lock = threading.Lock()
_encoders: dict[str, Any] = {}
_available: Optional[bool] = None

# Repeated model rounds count byte-identical tool-schema and compaction blocks.
# Keep only a cryptographic digest and integer exact/heuristic results:
# prompt/schema text never becomes resident cache state. The launch-time
# resource profile bounds the entry count for personal and distributed
# deployments, with a hard ceiling at this consumer boundary.
_TEXT_COUNT_CACHE_MIN_CHARS = 4_096
_TEXT_COUNT_REUSABLE_MIN_CHARS = 512
_TEXT_COUNT_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_TOKEN_COUNT_CACHE_CAPACITY', maximum=4_096)


@dataclass(frozen=True, slots=True)
class _TextCountCacheEntry:
    """Content-free exact/heuristic counts for one reusable text digest."""

    exact_tokens: int | None = None
    heuristic_tokens: int | None = None


_TextCountCacheKey = tuple[str, int, bytes]
_HEURISTIC_CACHE_SCOPE = 'entropy-heuristic/v1'
# ``encoding_for_model`` has exactly these two outputs. Listing them here lets
# the model-independent entropy estimator reuse whichever tokenizer-scoped
# entry was touched immediately before it, without a second cache or index.
_TOKENIZER_CACHE_SCOPES = ('o200k_base', 'cl100k_base')
_text_count_cache: OrderedDict[
    _TextCountCacheKey, _TextCountCacheEntry
] = OrderedDict()
_text_count_cache_lock = threading.Lock()
_text_count_cache_metrics = {
    'hits': 0,
    'misses': 0,
    'heuristicHits': 0,
    'heuristicMisses': 0,
    'evictions': 0,
}


def _get_encoder(name: str):
    """Return a cached tiktoken encoder, or None if tiktoken isn't installed."""
    global _available
    if _available is False:
        return None
    with _lock:
        if name in _encoders:
            return _encoders[name]
        try:
            import tiktoken  # type: ignore
            enc = tiktoken.get_encoding(name)
            _encoders[name] = enc
            _available = True
            return enc
        except ImportError:
            logger.info('[TokenCounter] tiktoken not installed — Tier 2 unavailable')
            _available = False
            return None
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken.get_encoding(%s) failed: %s',
                           name, e)
            return None


def encoding_for_model(model: str) -> str:
    """Pick the best tiktoken encoding for a model id."""
    m = (model or '').lower()
    if 'gpt-4o' in m or 'gpt-5' in m or re.search(r'\bo[134]\b', m):
        return 'o200k_base'
    return 'cl100k_base'


def _text_count_cache_key(
    text: str,
    encoding: str,
    *,
    reusable: bool,
) -> _TextCountCacheKey | None:
    cache_min_chars = (
        _TEXT_COUNT_REUSABLE_MIN_CHARS
        if reusable else _TEXT_COUNT_CACHE_MIN_CHARS
    )
    if len(text) < cache_min_chars:
        return None
    return (
        encoding,
        len(text),
        hashlib.sha256(
            text.encode('utf-8', errors='surrogatepass')
        ).digest(),
    )


def _put_text_count_cache_locked(
    cache_key: _TextCountCacheKey,
    entry: _TextCountCacheEntry,
) -> None:
    """Publish one entry while holding ``_text_count_cache_lock``."""
    _text_count_cache[cache_key] = entry
    _text_count_cache.move_to_end(cache_key)
    capacity = max(1, int(_TEXT_COUNT_CACHE_CAPACITY))
    while len(_text_count_cache) > capacity:
        _text_count_cache.popitem(last=False)
        _text_count_cache_metrics['evictions'] += 1


def _heuristic_cache_candidates(
    cache_key: _TextCountCacheKey,
) -> tuple[_TextCountCacheKey, ...]:
    """Return every bounded scope that can carry this model-free estimate."""
    _scope, char_count, digest = cache_key
    return tuple(
        (scope, char_count, digest)
        for scope in (*_TOKENIZER_CACHE_SCOPES, _HEURISTIC_CACHE_SCOPE)
    )


def _find_heuristic_entry_locked(
    cache_key: _TextCountCacheKey,
) -> tuple[_TextCountCacheKey | None, int | None]:
    """Find the shared heuristic value while holding the cache lock."""
    first_existing_key = None
    for candidate in _heuristic_cache_candidates(cache_key):
        entry = _text_count_cache.get(candidate)
        if entry is None:
            continue
        if first_existing_key is None:
            first_existing_key = candidate
        if entry.heuristic_tokens is not None:
            return candidate, entry.heuristic_tokens
    return first_existing_key, None


def _put_exact_count_locked(
    cache_key: _TextCountCacheKey,
    exact_tokens: int,
) -> None:
    """Merge an exact result with any model-independent heuristic entry."""
    existing = _text_count_cache.get(cache_key)
    heuristic_tokens = (
        existing.heuristic_tokens if existing is not None else None)
    if heuristic_tokens is None:
        _heuristic_key, heuristic_tokens = _find_heuristic_entry_locked(
            cache_key)

    # A standalone heuristic scan may have created the neutral carrier first.
    # The tokenizer-scoped exact entry supersedes it, keeping one capacity slot.
    neutral_key = (
        _HEURISTIC_CACHE_SCOPE, cache_key[1], cache_key[2])
    neutral = _text_count_cache.get(neutral_key)
    if (neutral_key != cache_key and neutral is not None
            and neutral.exact_tokens is None):
        _text_count_cache.pop(neutral_key, None)

    _put_text_count_cache_locked(
        cache_key,
        _TextCountCacheEntry(
            exact_tokens=exact_tokens,
            heuristic_tokens=heuristic_tokens,
        ),
    )


def count_text(text: str, model: str = '', *, reusable: bool = False) -> int:
    """Exact-ish count for a single text blob (public API)."""
    if not text:
        return 0
    encoding = encoding_for_model(model)
    cache_key = _text_count_cache_key(
        text, encoding, reusable=reusable)
    if cache_key is not None:
        with _text_count_cache_lock:
            cached = _text_count_cache.get(cache_key)
            if cached is not None and cached.exact_tokens is not None:
                _text_count_cache.move_to_end(cache_key)
                _text_count_cache_metrics['hits'] += 1
                return cached.exact_tokens
            _text_count_cache_metrics['misses'] += 1

    enc = _get_encoder(encoding)
    if enc is None:
        return 0  # caller should fall back to heuristic
    try:
        result = len(enc.encode(text, disallowed_special=()))
    except Exception as e:
        logger.debug('[TokenCounter] tiktoken encode failed: %s', e)
        return 0
    if cache_key is not None:
        with _text_count_cache_lock:
            _put_exact_count_locked(cache_key, result)
    return result


def cached_cheap_estimate_text(
    text: str,
    *,
    reusable: bool = False,
) -> int:
    """Return the entropy estimate, reusing the bounded text-digest cache.

    The estimate is model-independent, so it can share either tokenizer scope.
    Only the digest, length, and integer counts survive the call. ``reusable``
    has the same caller-proof semantics and admission threshold as
    :func:`count_text`.
    """
    if not text:
        return 0
    neutral_key = _text_count_cache_key(
        text, _HEURISTIC_CACHE_SCOPE, reusable=reusable)
    if neutral_key is None:
        return _cheap_estimate_text(text)

    with _text_count_cache_lock:
        cache_key, cached = _find_heuristic_entry_locked(neutral_key)
        if cached is not None:
            assert cache_key is not None
            _text_count_cache.move_to_end(cache_key)
            _text_count_cache_metrics['heuristicHits'] += 1
            return cached
        _text_count_cache_metrics['heuristicMisses'] += 1

    result = _cheap_estimate_text(text)
    with _text_count_cache_lock:
        # Re-select after computing outside the lock: a concurrent exact count
        # may now offer the better carrier for the same content digest.
        cache_key, raced = _find_heuristic_entry_locked(neutral_key)
        if raced is not None:
            return raced
        target_key = cache_key or neutral_key
        existing = _text_count_cache.get(target_key)
        _put_text_count_cache_locked(
            target_key,
            _TextCountCacheEntry(
                exact_tokens=(
                    existing.exact_tokens if existing is not None else None),
                heuristic_tokens=result,
            ),
        )
    return result


def _count_request_texts_with_cache(
    texts: list[str],
    *,
    model: str,
    encoder: Any,
    selected_text_token_counts: dict[int, int | None] | None = None,
) -> int:
    """Count one repeated request, batching short text and unique misses.

    Request history proves that its large prefix is reusable across model
    rounds. SHA-256 keys retain no prompt text after this call. Cache lookup and
    fill take the existing bounded lock; hashing and tokenizer work do not.
    """
    encoding = encoding_for_model(model)
    keyed_texts = [
        (
            text,
            _text_count_cache_key(text, encoding, reusable=True),
        )
        for text in texts
    ]
    short_texts: list[str] = []
    pending: OrderedDict[
        _TextCountCacheKey, tuple[str, int, list[int]]
    ] = OrderedDict()
    total = 0
    with _text_count_cache_lock:
        for text, cache_key in keyed_texts:
            text_identity = id(text)
            selected_identity = (
                text_identity
                if (selected_text_token_counts is not None
                    and text_identity in selected_text_token_counts)
                else None
            )
            if cache_key is None:
                short_texts.append(text)
                continue
            cached = _text_count_cache.get(cache_key)
            if cached is not None and cached.exact_tokens is not None:
                _text_count_cache.move_to_end(cache_key)
                _text_count_cache_metrics['hits'] += 1
                total += cached.exact_tokens
                if selected_identity is not None:
                    selected_text_token_counts[selected_identity] = (
                        cached.exact_tokens)
                continue
            duplicate = pending.get(cache_key)
            if duplicate is not None:
                selected_identities = duplicate[2]
                if selected_identity is not None:
                    selected_identities.append(selected_identity)
                pending[cache_key] = (
                    duplicate[0], duplicate[1] + 1, selected_identities)
                _text_count_cache_metrics['hits'] += 1
                continue
            pending[cache_key] = (
                text,
                1,
                [selected_identity] if selected_identity is not None else [],
            )
            _text_count_cache_metrics['misses'] += 1

    batch_texts = [
        *short_texts,
        *(text for text, _count, _selected in pending.values()),
    ]
    if not batch_texts:
        return total
    encoded = encoder.encode_batch(batch_texts, disallowed_special=())
    short_count = len(short_texts)
    for text, token_ids in zip(short_texts, encoded[:short_count]):
        count = len(token_ids)
        total += count
        text_identity = id(text)
        if (selected_text_token_counts is not None
                and text_identity in selected_text_token_counts):
            selected_text_token_counts[text_identity] = count

    pending_counts: list[tuple[_TextCountCacheKey, int]] = []
    for (cache_key, (_text, occurrences, selected_identities)), token_ids in zip(
        pending.items(),
        encoded[short_count:],
    ):
        count = len(token_ids)
        total += count * occurrences
        if selected_text_token_counts is not None:
            for text_identity in selected_identities:
                selected_text_token_counts[text_identity] = count
        pending_counts.append((cache_key, count))

    if pending_counts:
        with _text_count_cache_lock:
            for cache_key, count in pending_counts:
                _put_exact_count_locked(cache_key, count)
    return total


def text_count_cache_snapshot() -> dict[str, int]:
    """Return bounded, content-free cache telemetry for diagnostics/tests."""
    with _text_count_cache_lock:
        return {
            **_text_count_cache_metrics,
            'entries': len(_text_count_cache),
            'capacity': max(1, int(_TEXT_COUNT_CACHE_CAPACITY)),
            'minChars': max(0, int(_TEXT_COUNT_CACHE_MIN_CHARS)),
            'reusableMinChars': max(0, int(_TEXT_COUNT_REUSABLE_MIN_CHARS)),
        }


def _reset_text_count_cache_for_tests() -> None:
    with _text_count_cache_lock:
        _text_count_cache.clear()
        for key in _text_count_cache_metrics:
            _text_count_cache_metrics[key] = 0


class TiktokenCounter(TokenCounter):
    """Local universal tokenizer (OpenAI-exact, rest ±10 %)."""

    name = 'tiktoken'
    # ``confidence`` is refined per-model in ``count()``.
    confidence = 'good'
    needs_network = False

    def supports(self, model: str) -> bool:
        # Always supported as long as tiktoken is installed.
        return _get_encoder('cl100k_base') is not None

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        enc = _get_encoder(encoding_for_model(model))
        if enc is None:
            return None
        try:
            total = 0
            per_msg_overhead = 4  # role token + separators (OpenAI chat format)
            texts = list(iter_message_texts(messages, system, tools))
            measurement_out = kwargs.get('measurement_out')
            selected_text_token_counts = None
            if isinstance(measurement_out, dict):
                selected_text_token_counts = {}
                for message in messages or ():
                    if (not isinstance(message, dict)
                            or message.get('role') != 'tool'):
                        continue
                    content = message.get('content')
                    if not isinstance(content, str):
                        continue
                    if len(selected_text_token_counts) >= (
                            REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX):
                        break
                    selected_text_token_counts[id(content)] = None
            if texts:
                total += _count_request_texts_with_cache(
                    texts,
                    model=model,
                    encoder=enc,
                    selected_text_token_counts=selected_text_token_counts,
                )
            total += per_msg_overhead * (len(messages) if messages else 0)
            total += count_images(messages) * _IMAGE_TOKENS_DEFAULT
            total += _STRUCTURAL_OVERHEAD_TOKENS
            if (selected_text_token_counts is not None
                    and all(count is not None
                            for count in selected_text_token_counts.values())):
                measurement_out[
                    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY
                ] = {
                    identity: int(count)
                    for identity, count in selected_text_token_counts.items()
                    if count is not None
                }
            return total
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken count failed: %s', e)
            return None


__all__ = [
    'TiktokenCounter', 'count_text', 'cached_cheap_estimate_text',
    'encoding_for_model',
    'text_count_cache_snapshot',
]
