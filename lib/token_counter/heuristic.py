"""Tier 0 heuristic: CJK-aware char-level estimator.

This backend *always* works — no dependencies, no network. It serves
two purposes:

  1. Final fallback when every other tier fails.
  2. Cheap pre-filter that short-circuits expensive tiers when the
     context is clearly far below the model's limit.

Accuracy model: ``1 token / CJK char`` + ``1 token / dense-ASCII char``
(base64/hex/minified runs) + ``1 token / 3 other chars``.  Off by ±15 %
vs. tiktoken on mixed English/code, but crucially never *under*-counts
CJK (the failure mode that bit conv=mo4fr5xeup9ogp) nor high-entropy
base64 (the failure mode that bit conv=mq7y3irly1r4hu).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from lib.log import get_logger

from .base import (
    TokenCounter,
    count_images,
    iter_message_texts,
    _IMAGE_TOKENS_DEFAULT,
    _STRUCTURAL_OVERHEAD_TOKENS,
)

logger = get_logger(__name__)


# Range covers BMP CJK Unified Ideographs + CJK punctuation + fullwidth
# ASCII + hangul + katakana/hiragana. Each char ≈ 1 token in real BPE.
_CJK_LO = '\u3000'
_CJK_HI = '\uffef'

# High-entropy "dense" run: 40+ contiguous chars from the base64/hex/ID
# alphabet with NO whitespace. Real BPE tokenises this kind of content at
# roughly 1.0-1.6 chars/token (vs ~4 for English prose) because there are
# no reusable word-pieces. The flat ``other // 3`` model under-counts it by
# ~1.85x — the failure that let conv=mq7y3irly1r4hu sail past the proactive
# trigger at a heuristic 613K while the gateway saw 1.19M. We charge these
# runs at the BPE ceiling of 1 token/char: mathematically a tokenizer can
# never emit MORE tokens than characters, so this can over-count but never
# under-count — exactly the safe direction for a compaction trigger.
# Normal prose/code never matches (whitespace breaks the run every few chars).
_DENSE_RUN = re.compile(r'[A-Za-z0-9+/=]{40,}')

# Counting CJK chars with a Python per-char generator (``sum(1 for c in text
# if lo <= c <= hi)``) is the dominant cost of build_body on long
# conversations — it re-scans the whole transcript every turn. A single
# regex findall over the same range does the scan in C and is ~2x faster.
# Dense runs are pure ASCII (< U+3000) so they never overlap this range.
_CJK_FIND = re.compile(f'[{_CJK_LO}-{_CJK_HI}]+')

# A candidate run is only charged at the dense rate if it is genuinely
# HIGH-ENTROPY. A long run of one repeated char (``'x' * 12000``) matches the
# regex but is the OPPOSITE of base64 — real BPE merges repeats down to a
# handful of tokens, so charging 1/char would over-count ~8x. Require many
# distinct characters: base64 blobs use ~64 of the alphabet, hex uses 16,
# repeated/low-diversity filler uses a few. 16 cleanly separates them.
_DENSE_MIN_DISTINCT = 16


def cheap_estimate_text(text: str) -> int:
    """CJK-aware estimate for a single text blob.

    Three char classes:
      * CJK chars             → 1 token / char (never under-counts CJK).
      * dense base64/hex runs → 1 token / char (BPE ceiling; high entropy).
      * everything else       → 1 token / 3 chars (English/code prose).
    """
    if not text:
        return 0

    dense_chars = 0
    for m in _DENSE_RUN.finditer(text):
        run = m.group(0)
        # Only high-diversity runs get the dense rate; repeated/low-entropy
        # runs (e.g. 'x'*N) compress under real BPE and stay on the /3 path.
        if len(set(run)) >= _DENSE_MIN_DISTINCT:
            dense_chars += len(run)

    cjk = sum(len(m) for m in _CJK_FIND.findall(text))
    # Dense runs are pure ASCII, so they never overlap the CJK count.
    other = len(text) - cjk - dense_chars
    return cjk + dense_chars + (other // 3 + 1 if other else 0)


def cheap_estimate(messages: list, system: Any = None, tools: Any = None) -> int:
    """Cheap estimate for a full request."""
    text_tokens = sum(
        cheap_estimate_text(t)
        for t in iter_message_texts(messages, system, tools)
    )
    image_tokens = count_images(messages) * _IMAGE_TOKENS_DEFAULT
    return text_tokens + image_tokens + _STRUCTURAL_OVERHEAD_TOKENS


class HeuristicCounter(TokenCounter):
    """Final-fallback backend — always returns a number."""

    name = 'heuristic'
    confidence = 'approx'
    needs_network = False

    def supports(self, model: str) -> bool:
        return True  # universal fallback

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        return cheap_estimate(messages, system=system, tools=tools)


__all__ = ['HeuristicCounter', 'cheap_estimate', 'cheap_estimate_text']
