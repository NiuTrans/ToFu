"""Grounding — the anti-hallucination gate for the recommend engine.

No card is ever surfaced unless its arXiv ID resolves through the existing
``search_arxiv`` / ``fetch_arxiv_title`` path to a real paper. A title the model
produced but that cannot be grounded is dropped, logged at debug, and never
rendered.

The arXiv seams (``search_arxiv`` / ``fetch_arxiv_title``) — and the
``_ground_candidate`` helper the correction path re-enters — are resolved
THROUGH the package facade at call time (``import lib.paper.recommend_engine as
_pkg``) so a test patching ``re_mod.search_arxiv`` / ``re_mod.fetch_arxiv_title``
/ ``re_mod._ground_candidate`` bites here exactly as it did in the original
single-module layout.
"""

import re

from lib.log import get_logger

from ..arxiv import _extract_arxiv_id

logger = get_logger(__name__)

# The model may over-propose; we ground up to this many before stopping so a
# few hallucinated titles don't starve the real ones.
_GROUND_ATTEMPT_MULTIPLIER = 2
_GROUND_SEARCH_DEPTH = 5

# Tokens too generic to count toward a title-match (a lone "models" / "learning"
# overlap must not ground an unrelated paper).
_STOPWORDS = frozenset({
    'a', 'an', 'the', 'of', 'for', 'and', 'or', 'to', 'in', 'on', 'via', 'with',
    'is', 'are', 'be', 'model', 'models', 'learning', 'network', 'networks',
    'neural', 'deep', 'using', 'towards', 'toward', 'new', 'from',
})


def _detect_lang(text):
    """Rough UI-language guess for the date-anchor clause: zh if any CJK char."""
    for ch in (text or ''):
        if '\u4e00' <= ch <= '\u9fff':
            return 'zh'
    return 'en'


def _norm_id(arxiv_id):
    """Strip a version suffix so ``2502.09992v3`` and ``2502.09992`` compare equal."""
    from ..arxiv import normalize_arxiv_id
    return normalize_arxiv_id(arxiv_id)


def _title_tokens(s):
    """Significant lowercase token set of a title (stopwords removed)."""
    toks = set(re.findall(r'[a-z0-9]+', (s or '').lower()))
    return {t for t in toks if t not in _STOPWORDS and len(t) > 1}


def _title_grounded(claimed_title, real_title):
    """Is ``real_title`` a plausible match for the model's ``claimed_title``?

    Guards against ``search_arxiv`` returning a tangential top hit for a title
    the model half-remembered. Requires a meaningful significant-token overlap
    (not a single generic word).
    """
    ct = _title_tokens(claimed_title)
    rt = _title_tokens(real_title)
    if not ct or not rt:
        return False
    shared = ct & rt
    # Need at least 2 shared significant tokens AND >=40% of the (shorter) claim.
    need = max(2, round(0.4 * len(ct)))
    return len(shared) >= min(need, len(ct)) and len(shared) >= 2


def _card_from_result(result, why='', venue=''):
    """Build a recommend card (search_arxiv result + why/venue annotations)."""
    card = dict(result)
    card['why'] = (why or '').strip()
    card['venue'] = (venue or '').strip()
    return card


def _ground_candidate(cand):
    """Resolve one model-proposed candidate to a REAL arXiv paper, or None.

    Grounding path (reuses the existing arXiv seam only):
      1. Search arXiv by the proposed title → full metadata. If the model gave
         an arxiv_id, prefer the search hit whose id matches; otherwise accept
         the top hit only when its title plausibly matches (``_title_grounded``).
      2. Fallback: if the model gave an arxiv_id that search didn't surface,
         verify the id directly via ``fetch_arxiv_title`` and build a
         title-only card.
      3. Otherwise drop (logged at debug) — an ungrounded card is NEVER shown.

    The arXiv seams are resolved through the package facade so tests patching
    ``re_mod.search_arxiv`` / ``re_mod.fetch_arxiv_title`` bite here.
    """
    import lib.paper.recommend_engine as _pkg
    search_arxiv = _pkg.search_arxiv
    fetch_arxiv_title = _pkg.fetch_arxiv_title

    title = (cand.get('title') or '').strip()
    raw_id = cand.get('arxiv_id')
    claimed_id = _extract_arxiv_id(raw_id) if raw_id else None
    why = cand.get('why') or ''
    venue = cand.get('venue') or ''

    results = search_arxiv(title, max_results=_GROUND_SEARCH_DEPTH) if title else []

    # 1a. Exact id match among search hits (strongest signal).
    if claimed_id:
        for r in results:
            if _norm_id(r.get('arxiv_id')) == _norm_id(claimed_id):
                return _card_from_result(r, why, venue)

    # 1b. Title-plausible top hit.
    if results and _title_grounded(title, results[0].get('title', '')):
        return _card_from_result(results[0], why, venue)

    # 2. Verify a claimed id directly (title-only card) when search missed it.
    if claimed_id:
        real_title = fetch_arxiv_title(claimed_id)
        if real_title:
            logger.debug('[Paper:Recommend] Grounded %s via direct id verify (search missed)',
                         claimed_id)
            return _card_from_result({
                'arxiv_id': claimed_id,
                'title': real_title,
                'authors': [],
                'summary': '',
                'published': '',
                'primary_category': '',
                'pdf_url': f'https://arxiv.org/pdf/{claimed_id}.pdf',
                'abs_url': f'https://arxiv.org/abs/{claimed_id}',
            }, why, venue)

    logger.debug('[Paper:Recommend] Dropped ungrounded candidate: %.120s (claimed id=%s)',
                 title or '(no title)', claimed_id)
    return None


def _ground_correction(correction, seen_ids):
    """Ground the optional correction block.

    The ``note`` (interpretive prose) is kept as-is; the offered ``paper``, if
    any, must ground through the same path or it is dropped (logged) — the
    banner then shows the note without a bogus paper offer.

    ``_ground_candidate`` is re-entered through the facade so a test patching
    ``re_mod._ground_candidate`` also governs the correction paper.
    """
    if not isinstance(correction, dict):
        return None
    note = (correction.get('note') or '').strip()
    if not note:
        return None
    out = {'note': note, 'paper': None}
    paper = correction.get('paper')
    if isinstance(paper, dict) and (paper.get('title') or paper.get('arxiv_id')):
        import lib.paper.recommend_engine as _pkg
        grounded = _pkg._ground_candidate(paper)
        if grounded and _norm_id(grounded['arxiv_id']) not in seen_ids:
            out['paper'] = grounded
        elif not grounded:
            logger.debug('[Paper:Recommend] Correction paper failed grounding: %.120s',
                         paper.get('title') or paper.get('arxiv_id') or '(unknown)')
    return out
