"""Base types and registry for the token-counter package.

Each backend implements :class:`TokenCounter`. The resolver
(``resolver.py``) returns an ordered list of backends per model; the
public ``count_tokens()`` walks that list and returns the first
successful result.

Design goal: **modular**. Adding a new backend is a single file +
one line in ``resolver.py``; nothing else changes. Every backend
must degrade gracefully (return ``None`` on failure, never raise).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Return type
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class CountResult:
    """Outcome of a single :meth:`TokenCounter.count` invocation.

    Attributes:
        tokens:      Token count (int, >= 0).
        method:      Name of the backend that produced the count
                     (``'anthropic_api'`` / ``'tiktoken'`` / …).
        elapsed_ms:  Wall-clock time to compute.
        confidence:  ``'exact'`` | ``'good'`` | ``'approx'`` — how far
                     this is from the real on-the-wire number. Used by
                     callers (e.g. compaction) that want to budget more
                     conservatively when they're fed a rough estimate.
    """
    tokens: int
    method: str
    elapsed_ms: int
    confidence: str = 'good'

    def as_dict(self) -> dict:
        return {
            'tokens': self.tokens,
            'method': self.method,
            'elapsed_ms': self.elapsed_ms,
            'confidence': self.confidence,
        }


# ───────────────────────────────────────────────────────────────────────────
# Backend ABC
# ───────────────────────────────────────────────────────────────────────────

class TokenCounter(ABC):
    """Abstract base for a single counting strategy.

    Subclass contract:
      - :attr:`name` is a stable short identifier.
      - :attr:`confidence` describes the typical accuracy of this
        backend (``'exact'``/``'good'``/``'approx'``).
      - :attr:`needs_network` is True for tiers that hit the internet;
        the dispatcher may skip them when offline or when the cheap
        estimate is far under the context limit.
      - :meth:`supports` returns True iff this backend can meaningfully
        count tokens for the given model.
      - :meth:`count` returns an int token count, or ``None`` on any
        failure. MUST NOT raise.
    """

    name: str = 'base'
    confidence: str = 'good'
    needs_network: bool = False

    @abstractmethod
    def supports(self, model: str) -> bool:  # pragma: no cover - interface
        ...

    @abstractmethod
    def count(self,
              messages: list,
              *,
              model: str,
              system: Any = None,
              tools: Any = None,
              **kwargs) -> Optional[int]:  # pragma: no cover - interface
        ...


# ───────────────────────────────────────────────────────────────────────────
# Shared helpers — text iteration, image counting, CJK pre-filter
# ───────────────────────────────────────────────────────────────────────────

_IMAGE_TOKENS_DEFAULT = 800
"""Conservative vision-token estimate per image (matches compaction.py)."""

_STRUCTURAL_OVERHEAD_TOKENS = 400
"""Tokens we reserve for the implicit request overhead the counter
never sees (beta headers, tool_choice, cache_control markers, …)."""

REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY = (
    '_reusableTextTokenCountsByIdentity')
"""Call-local exact text counts; never retain, serialize, or audit this map."""

REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX = 4_096
"""Hard ceiling for call-local immutable-string identity reuse hints."""


def iter_message_texts(messages: list, system: Any = None, tools: Any = None):
    """Yield every text blob we want to count, in rough wire order.

    Walks the OpenAI-shape message list + optional system / tools
    blocks. Ignores base64 image URLs (those get a fixed per-image
    token fee added separately by the caller) but counts alt text /
    placeholders inside tool results.
    """
    import json as _json

    # System prompt — string or list of content blocks
    if system:
        if isinstance(system, str):
            yield system
        elif isinstance(system, list):
            for blk in system:
                if isinstance(blk, dict) and isinstance(blk.get('text'), str):
                    yield blk['text']

    # Tool schemas are JSON-encoded on the wire
    if tools:
        try:
            yield _json.dumps(tools, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            logger.debug('[TokenCounter] tool schema json.dumps failed: %s', e)

    # Messages
    for msg in messages or ():
        content = msg.get('content')
        if isinstance(content, str):
            yield content
        elif isinstance(content, list):
            for blk in content:
                if not isinstance(blk, dict):
                    continue
                btype = blk.get('type')
                if btype in ('text', 'input_text', 'output_text'):
                    txt = blk.get('text') or ''
                    if isinstance(txt, str):
                        yield txt
                elif btype == 'tool_result':
                    sub = blk.get('content')
                    if isinstance(sub, str):
                        yield sub
                    elif isinstance(sub, list):
                        for sb in sub:
                            if isinstance(sb, dict) and isinstance(sb.get('text'), str):
                                yield sb['text']
                # image_url → image-fee path (applied elsewhere)

        # reasoning_content is billed on some providers, inert on others;
        # always count it since it's present in the wire payload.
        r = msg.get('reasoning_content')
        if isinstance(r, str):
            yield r
        elif isinstance(r, list):
            for blk in r:
                if isinstance(blk, dict) and isinstance(blk.get('text'), str):
                    yield blk['text']

        # tool_calls — the arguments string and name are tokenised
        for tc in msg.get('tool_calls') or ():
            fn = tc.get('function') or {}
            args = fn.get('arguments')
            if isinstance(args, str):
                yield args
            name = fn.get('name')
            if isinstance(name, str):
                yield name


def count_images(messages: list) -> int:
    """Count ``image_url`` blocks across messages (fixed per-image fee)."""
    n = 0
    for msg in messages or ():
        content = msg.get('content')
        if not isinstance(content, list):
            continue
        for blk in content:
            if isinstance(blk, dict) and blk.get('type') == 'image_url':
                n += 1
    return n


__all__ = [
    'CountResult', 'TokenCounter',
    'iter_message_texts', 'count_images',
    '_IMAGE_TOKENS_DEFAULT', '_STRUCTURAL_OVERHEAD_TOKENS',
    'REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY',
    'REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX',
]
