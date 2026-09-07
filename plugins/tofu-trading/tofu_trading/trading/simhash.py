"""lib/trading/simhash.py — SimHash content fingerprinting for near-duplicate detection.

SimHash is a locality-sensitive hash (LSH) that maps text to a 64-bit fingerprint.
Near-duplicate texts have fingerprints with low Hamming distance (few differing bits).

This means two articles about the same event — but reworded by different outlets,
with different formatting, or with source attribution appended — will still produce
similar fingerprints (Hamming distance ≤ 3).

Algorithm:
  1. Tokenize text into character n-grams (n=3 for CJK robustness)
  2. Hash each shingle to a 64-bit value
  3. Build a weighted bit-vector: for each hash, +1 per '1' bit, -1 per '0' bit
  4. Final fingerprint: bit i = 1 if vector[i] > 0 else 0

Why n-grams instead of word tokens?
  - CJK text has no word boundaries (no spaces between Chinese characters)
  - Character 3-grams naturally capture word-level semantics in Chinese
  - Robust to minor edits (a single character change only affects 3 shingles)
"""

import re

# FNV-1a 64-bit hash parameters
_FNV_OFFSET = 0xcbf29ce484222325
_FNV_PRIME = 0x100000001b3
_MASK64 = (1 << 64) - 1


def _fnv1a_64(data: bytes) -> int:
    """FNV-1a 64-bit hash — fast, well-distributed, no external deps."""
    h = _FNV_OFFSET
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & _MASK64
    return h


def _tokenize(text: str, n: int = 3) -> list[str]:
    """Extract character n-grams from normalized text.

    Normalization:
      - Lowercase
      - Strip punctuation, symbols, whitespace
      - Keep CJK characters, letters, digits
    """
    # Normalize: lowercase, keep alphanumeric + CJK, collapse
    text = text.lower()
    text = re.sub(r'[^\w\u4e00-\u9fff\u3400-\u4dbf]', '', text)
    if len(text) < n:
        return [text] if text else []
    return [text[i:i + n] for i in range(len(text) - n + 1)]


def compute_simhash(text: str, n: int = 3) -> int:
    """Compute a 64-bit SimHash fingerprint for the given text.

    Args:
        text: Input text (any language, handles CJK natively)
        n: Character n-gram size (default 3)

    Returns:
        64-bit integer fingerprint
    """
    if not text:
        return 0

    shingles = _tokenize(text, n)
    if not shingles:
        return 0

    # Weighted bit-vector accumulation
    v = [0] * 64
    for shingle in shingles:
        h = _fnv1a_64(shingle.encode('utf-8'))
        for i in range(64):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1

    # Threshold: positive → 1, else → 0
    fingerprint = 0
    for i in range(64):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Count differing bits between two 64-bit fingerprints.

    Returns 0 for identical texts, up to 64 for completely different texts.
    Typical thresholds:
      - ≤ 3: near-duplicate (same article, minor edits/reformatting)
      - ≤ 6: very similar (same event, different wording)
      - ≤ 10: related (same topic, different angle)
    """
    return bin(a ^ b).count('1')


# ── DB storage helpers ──
# PostgreSQL BIGINT is signed 64-bit (max 2^63 - 1).
# SimHash is unsigned 64-bit.  ~50% of hashes have bit 63 set and
# overflow signed storage, causing OverflowError.  Convert to/from signed.

def to_signed64(h: int) -> int:
    """Convert unsigned 64-bit SimHash → signed 64-bit for DB storage."""
    return h - (1 << 64) if h >= (1 << 63) else h


def to_unsigned64(h: int) -> int:
    """Convert signed 64-bit (from DB) → unsigned 64-bit SimHash."""
    return h + (1 << 64) if h < 0 else h

