"""lib/memory/relevance/_tokenize.py — tokenizer + shared BM25 constants.

Latin + CJK tokenization used by every scorer in this package. CJK runs are
tokenized into overlapping bigrams (segmenter-free) so Chinese/Japanese
memories are searchable; the Latin stream splits on whitespace/punctuation and
drops stop words. Also hosts the BM25 hyper-parameters and the default top-K
shared by the memory-facing entrypoints.
"""

import re
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════

DEFAULT_TOP_K = 30
BM25_K1 = 1.5
BM25_B = 0.75

# Common English stop words — excluded from both query and document tokens
_STOP_WORDS = frozenset({
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'do', 'for',
    'from', 'has', 'have', 'he', 'in', 'is', 'it', 'its', 'of', 'on',
    'or', 'she', 'so', 'the', 'to', 'was', 'we', 'will', 'with', 'you',
    'that', 'this', 'not', 'but', 'they', 'what', 'all', 'if', 'can',
    'had', 'her', 'his', 'how', 'may', 'no', 'our', 'out', 'too',
    'use', 'when', 'who', 'new', 'get', 'set', 'one', 'two', 'any',
})

# Regex: split on whitespace + common punctuation (also treats every
# non-Latin char — including CJK — as a delimiter; CJK is handled separately).
_TOKENIZE_RE = re.compile(r'[^a-z0-9_]+')

# Contiguous runs of CJK characters: unified ideographs + extension-A
# (mirrors lib/text_lang._CJK_RE) plus Japanese kana, so Chinese/Japanese
# memories are searchable. Latin tokenization above strips these out, so
# without dedicated handling every CJK-only description was invisible to BM25.
_CJK_RUN_RE = re.compile(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+')


# ═══════════════════════════════════════════════════════
#  Tokenizer
# ═══════════════════════════════════════════════════════

def _cjk_tokens(text: str) -> list[str]:
    """Tokenize CJK text into overlapping bigrams (segmenter-free).

    Chinese/Japanese has no word boundaries, so a pure word-split yields
    nothing. Overlapping character bigrams are the standard lightweight
    substitute for a segmenter: ``中文海报`` → ``中文 文海 海报``. Applied
    symmetrically to query and documents, this gives robust partial-match
    recall. Isolated single CJK chars fall back to a unigram.
    """
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(text):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _tokenize(text: str) -> list[str]:
    """Tokenize text into lowercase tokens, removing stop words.

    Splits on whitespace and punctuation. Also splits snake_case and
    kebab-case identifiers into sub-tokens (e.g. 'flask_migration' →
    ['flask', 'migration']). CJK runs are tokenized into overlapping
    bigrams via :func:`_cjk_tokens` so Chinese/Japanese is searchable.
    """
    lowered = text.lower()
    # Replace hyphens and underscores with spaces for sub-token splitting
    lowered = lowered.replace('-', ' ').replace('_', ' ')
    tokens = _TOKENIZE_RE.split(lowered)
    out = [t for t in tokens if t and t not in _STOP_WORDS and len(t) > 1]
    out.extend(_cjk_tokens(lowered))
    return out


def _build_memory_doc(mem: dict[str, Any], include_body: bool = False) -> list[str]:
    """Build a token list from a memory's metadata (name + description + tags).

    Args:
        mem: Memory dict.
        include_body: If True, also tokenize the memory body for deeper matching.
    """
    parts = [
        mem.get('name', ''),
        mem.get('description', ''),
    ]
    tags = mem.get('tags', [])
    if isinstance(tags, list):
        parts.extend(tags)
    if include_body:
        body = mem.get('body', '')
        if body:
            # Limit body to first 2000 chars to keep tokenization fast
            parts.append(body[:2000])
    return _tokenize(' '.join(parts))
