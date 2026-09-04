"""URL and content deduplication for search results."""

import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from tofu_search.log import get_logger

logger = get_logger(__name__)

__all__ = ['canonical_url_key', 'dedup_by_content', 'dedup_by_full_content']

_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uac00-\ud7af\u3040-\u30ff]')
_PERCENT_ESCAPE_RE = re.compile(r'%([0-9a-fA-F]{2})')
_UNRESERVED = frozenset(
    'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~')
_TRACKING_QUERY_KEYS = frozenset({
    'dclid', 'fbclid', 'gclid', 'gbraid', 'msclkid', 'mc_cid', 'mc_eid',
    's_cid', 'vero_conv', 'vero_id', 'wbraid', 'yclid', '_hsenc', '_hsmi',
})


def _decode_unreserved(match: re.Match) -> str:
    char = chr(int(match.group(1), 16))
    return char if char in _UNRESERVED else match.group(0).upper()


def canonical_url_key(url: str) -> str:
    """Return a conservative canonical key for cross-engine URL deduplication.

    Fragments and well-known analytics parameters never identify different page
    content, while scheme, default ports, parameter order and percent-encoding
    routinely differ across engines. Meaningful query parameters are preserved.
    Unlike the old 150-character truncation, this never conflates two long URLs
    that only diverge near the end.
    """
    raw = (url or '').strip()
    if not raw:
        return ''
    try:
        parts = urlsplit(raw)
        host = (parts.hostname or '').lower().rstrip('.')
        if not host:
            return raw.lower().rstrip('/')
        try:
            port = parts.port
        except ValueError:
            port = None
        if port and not ((parts.scheme.lower() == 'http' and port == 80)
                         or (parts.scheme.lower() == 'https' and port == 443)):
            host = f'{host}:{port}'

        path = _PERCENT_ESCAPE_RE.sub(_decode_unreserved, parts.path or '/')
        if path != '/':
            path = path.rstrip('/')

        query = []
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            lowered = key.lower()
            if lowered.startswith('utm_') or lowered in _TRACKING_QUERY_KEYS:
                continue
            query.append((key, value))
        query.sort(key=lambda pair: (pair[0], pair[1]))
        suffix = f'?{urlencode(query, doseq=True)}' if query else ''
        return f'{host}{path}{suffix}'
    except Exception:
        return raw.lower().split('#', 1)[0].rstrip('/')


def _text_to_shingles(text: str) -> set[str]:
    """Convert text into a set of shingles (word tokens + CJK char bigrams).

    For Latin text, splits on whitespace after lowering + stripping punctuation.
    For CJK text (Chinese/Japanese/Korean), uses overlapping 2-char bigrams
    since CJK has no word-boundary spaces.
    """
    text = text.lower()
    tokens = set()

    # Extract CJK character bigrams
    cjk_chars = _CJK_RE.findall(text)
    if cjk_chars:
        for i in range(len(cjk_chars) - 1):
            tokens.add(cjk_chars[i] + cjk_chars[i + 1])
        # Also add individual CJK chars for short texts
        if len(cjk_chars) < 6:
            tokens.update(cjk_chars)

    # Extract Latin words
    latin = _CJK_RE.sub(' ', text)
    latin = re.sub(r'[^\w\s]', ' ', latin)
    for w in latin.split():
        if len(w) > 1:  # skip single letters
            tokens.add(w)

    return tokens


def _jaccard(a: set, b: set) -> float:
    """Jaccard similarity between two sets."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def dedup_by_content(results: list[dict], threshold: float = 0.45) -> list[dict]:
    """Remove near-duplicate results based on title+snippet word overlap.

    Uses Jaccard similarity on word sets. When two results are similar,
    keeps the one that appeared first (earlier engine = higher priority).
    O(n²) but n ≤ ~40, so < 1ms in practice.

    Args:
        results: URL-deduplicated search results.
        threshold: Jaccard similarity above which two results are duplicates.

    Returns:
        Deduplicated results list (order preserved).
    """
    if len(results) <= 1:
        return results

    # Pre-compute shingle sets for each result (supports CJK + Latin)
    shingle_sets = []
    for r in results:
        title = (r.get('title') or '').strip()
        snippet = (r.get('snippet') or '').strip()
        shingle_sets.append(_text_to_shingles(f'{title} {snippet}'))

    keep = []
    keep_indices = []
    removed = 0
    for i, r in enumerate(results):
        is_dup = False
        for ki in keep_indices:
            sim = _jaccard(shingle_sets[i], shingle_sets[ki])
            if sim >= threshold:
                is_dup = True
                removed += 1
                break
        if not is_dup:
            keep.append(r)
            keep_indices.append(i)

    if removed:
        logger.info('[ContentDedup] %d->%d results (removed %d near-duplicates, threshold=%.2f)',
                    len(results), len(keep), removed, threshold)
    return keep


def _full_text_shingles(text: str) -> set[str]:
    """Word/CJK trigrams for near-copy detection, bounded for predictable cost."""
    if not text:
        return set()
    # The head contains the article identity; the tail helps distinguish pages
    # with a shared site template. The extractor's link inventory is excluded.
    marker = text.find('--- Page Links ---')
    if marker >= 0:
        text = text[:marker]
    if len(text) > 28_000:
        text = text[:24_000] + '\n' + text[-4_000:]
    lowered = text.lower()
    latin = re.findall(r'[a-z0-9_]{2,}', _CJK_RE.sub(' ', lowered))
    cjk = _CJK_RE.findall(lowered)
    shingles = {
        'w:' + '\x1f'.join(latin[i:i + 3])
        for i in range(max(0, len(latin) - 2))
    }
    shingles.update(
        'c:' + ''.join(cjk[i:i + 3])
        for i in range(max(0, len(cjk) - 2))
    )
    return shingles


def _merge_duplicate_evidence(kept: dict, duplicate: dict) -> None:
    sources = []
    for result in (kept, duplicate):
        for source in result.get('sources') or [result.get('source')]:
            if source and source not in sources:
                sources.append(source)
    if sources:
        kept['sources'] = sources
        kept['engine_count'] = max(
            len(sources), kept.get('engine_count', 1), duplicate.get('engine_count', 1))
    duplicate_url = duplicate.get('url')
    if duplicate_url and duplicate_url != kept.get('url'):
        urls = kept.setdefault('duplicate_urls', [])
        if duplicate_url not in urls:
            urls.append(duplicate_url)


def dedup_by_full_content(results: list[dict], threshold: float = 0.78) -> list[dict]:
    """Remove fetched mirrors/syndicated copies before LLM filtering and output.

    Snippet dedup runs before fetching; this second pass catches different URLs
    whose extracted articles are actual near-copies. Snippet-only candidates are
    retained because there is no content evidence on which to collapse them.
    """
    if len(results) <= 1:
        return results
    fingerprints = [_full_text_shingles(r.get('full_content') or '') for r in results]
    kept: list[dict] = []
    kept_indices: list[int] = []
    removed = 0
    for idx, result in enumerate(results):
        fp = fingerprints[idx]
        duplicate_of = None
        if fp:
            for kept_pos, kept_idx in enumerate(kept_indices):
                other = fingerprints[kept_idx]
                if other and _jaccard(fp, other) >= threshold:
                    duplicate_of = kept_pos
                    break
        if duplicate_of is None:
            kept.append(result)
            kept_indices.append(idx)
        else:
            removed += 1
            _merge_duplicate_evidence(kept[duplicate_of], result)
    if removed:
        logger.info('[FullContentDedup] %d->%d results (removed %d mirrors, threshold=%.2f)',
                    len(results), len(kept), removed, threshold)
    return kept
