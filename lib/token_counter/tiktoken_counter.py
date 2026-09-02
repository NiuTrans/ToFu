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
from typing import Any, Optional

from runtime_guards import resolve_resource_budget

from lib.log import get_logger

from .base import (
    TokenCounter,
    count_images,
    iter_message_texts,
    _IMAGE_TOKENS_DEFAULT,
    _STRUCTURAL_OVERHEAD_TOKENS,
)

logger = get_logger(__name__)


_lock = threading.Lock()
_encoders: dict[str, Any] = {}
_available: Optional[bool] = None

# Repeated model rounds count byte-identical tool-schema and compaction blocks.
# Keep only a cryptographic digest and integer result: prompt/schema text never
# becomes resident cache state.  The launch-time resource profile bounds the
# entry count for personal and distributed deployments, with a hard ceiling at
# this consumer boundary.
_TEXT_COUNT_CACHE_MIN_CHARS = 4_096
_TEXT_COUNT_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_TOKEN_COUNT_CACHE_CAPACITY', maximum=4_096)
_text_count_cache: OrderedDict[tuple[str, int, bytes], int] = OrderedDict()
_text_count_cache_lock = threading.Lock()


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


def count_text(text: str, model: str = '') -> int:
    """Exact-ish count for a single text blob (public API)."""
    if not text:
        return 0
    encoding = encoding_for_model(model)
    cache_key: tuple[str, int, bytes] | None = None
    if len(text) >= _TEXT_COUNT_CACHE_MIN_CHARS:
        cache_key = (
            encoding,
            len(text),
            hashlib.sha256(text.encode('utf-8')).digest(),
        )
        with _text_count_cache_lock:
            cached = _text_count_cache.get(cache_key)
            if cached is not None:
                _text_count_cache.move_to_end(cache_key)
                return cached

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
            _text_count_cache[cache_key] = result
            _text_count_cache.move_to_end(cache_key)
            capacity = max(1, int(_TEXT_COUNT_CACHE_CAPACITY))
            while len(_text_count_cache) > capacity:
                _text_count_cache.popitem(last=False)
    return result


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
            if texts:
                # encode_batch is ~2-3× faster than looping
                for ids in enc.encode_batch(texts, disallowed_special=()):
                    total += len(ids)
            total += per_msg_overhead * (len(messages) if messages else 0)
            total += count_images(messages) * _IMAGE_TOKENS_DEFAULT
            total += _STRUCTURAL_OVERHEAD_TOKENS
            return total
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken count failed: %s', e)
            return None


__all__ = ['TiktokenCounter', 'count_text', 'encoding_for_model']
