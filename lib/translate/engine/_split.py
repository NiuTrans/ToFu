"""Sentence-completeness helpers for the translation engine.

Isolates the pure text-classification primitives the truncation detector
relies on: the set of characters a *complete* translation may legitimately
end on, and the ``_ends_midsentence`` predicate built on top of it.

These are intentionally dependency-free (no I/O, no LLM/MT imports) so the
engine's retry loop can call ``_ends_midsentence`` cheaply in its hot path.
"""

from lib.log import get_logger

logger = get_logger(__name__)


# Characters that legitimately END a complete translation. A result whose
# last non-space char is NOT one of these ended mid-sentence — the dominant
# silent-truncation mode where a cheap model stops early with
# finish_reason=stop (NOT length) and the byte count still clears the ratio
# floor. Covers CJK full-width terminators + Latin sentence enders + closing
# brackets/quotes/fences a complete segment can legitimately end on.
_SENTENCE_END_CHARS = frozenset(
    '。．.！!？?…～~：:；;、,，)）]】》」』”"\'`’*_>')


def _ends_midsentence(text: str) -> bool:
    """True when ``text`` does NOT end on a sentence terminator/closer.

    A complete translation ends on punctuation or a closing bracket/fence; a
    body that ends on a bare word/identifier char stopped mid-generation. Pure;
    empty / whitespace-only → False (the empty check handles that separately).
    """
    t = (text or '').rstrip()
    if not t:
        return False
    return t[-1] not in _SENTENCE_END_CHARS
