"""Low-cost citation verifier — reuses the free CrossRef / arXiv APIs.

Detects likely-hallucinated references in a paper or ``.bib`` file WITHOUT any
LLM call: every check is one (or two) free HTTP GETs against an authoritative
catalogue. The output is deliberately a THREE-state verdict, not a binary one,
because "we couldn't find it" is NOT the same as "it's fake":

- ``verified``     — an authoritative record matches the claim.
- ``suspicious``   — HIGH-confidence contradiction (a claimed concrete
                     identifier definitively does not resolve, or resolves to a
                     *different* paper than the one claimed).
- ``unverifiable`` — we could not confirm OR refute (no identifier, coverage
                     gap, book/dataset/software, non-English, transport error,
                     rate-limit). The safe default — NEVER report these as
                     hallucinations.

Anti-false-positive discipline (the whole point):
  * Only Tier-1 claims (a concrete DOI or arXiv id) can ever be ``suspicious``.
  * A title-only claim is ``verified`` on a strong match, otherwise
    ``unverifiable`` — a missing CrossRef title hit is treated as a coverage
    gap, never as evidence of fabrication.
  * Rate-limit / timeout / parse failure degrades to ``unverifiable``
    (mirrors the vertical handlers' graceful-None semantics).

The HTTP seam is :func:`tofu_search.search.vertical.base.http_get` so tests
patch exactly one function and never touch the network.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

from tofu_search.log import get_logger
from tofu_search.search.vertical import base
from tofu_search.search.vertical.semantic_scholar import _ss_headers
from tofu_search.verify.parse import Citation, parse_bibtex, parse_references

logger = get_logger(__name__)

__all__ = [
    'VERIFIED', 'SUSPICIOUS', 'UNVERIFIABLE',
    'verify_citation', 'verify_citations', 'verify_bibtex', 'verify_references',
    'summarize',
]

VERIFIED = 'verified'
SUSPICIOUS = 'suspicious'
UNVERIFIABLE = 'unverifiable'

# Title-similarity gates (normalized SequenceMatcher ratio in [0, 1]).
_STRONG_SIM = 0.90   # at/above → titles are "the same paper"
_WEAK_SIM = 0.55     # below → titles are clearly different (used only to flag
                     # a Tier-1 id that resolves to an UNRELATED paper)

_TIMEOUT = 10
_MAX_WORKERS = 4
_CROSSREF_WORKS = 'https://api.crossref.org/works'
_ARXIV_API = 'http://export.arxiv.org/api/query'
_S2_SEARCH = 'https://api.semanticscholar.org/graph/v1/paper/search'
_ATOM_NS = {'a': 'http://www.w3.org/2005/Atom'}


def _headers() -> dict:
    """CrossRef polite-pool header — include a mailto when configured."""
    ua = 'Mozilla/5.0 (compatible; TofuVerify/1.0)'
    mailto = os.environ.get('CROSSREF_MAILTO', '').strip()
    if mailto:
        ua += f' (mailto:{mailto})'
    return {'User-Agent': ua, 'Accept': 'application/json'}


_NORM_RE = re.compile(r'[^a-z0-9]+')


def _normalize_title(t: str) -> str:
    """Lowercase, strip accents-insensitively to alnum tokens, collapse space."""
    if not t:
        return ''
    return _NORM_RE.sub(' ', t.lower()).strip()


def _title_similarity(a: str, b: str) -> float:
    """Normalized similarity of two titles in [0, 1].

    This is the load-bearing gate: a Tier-1 identifier is ``verified`` only
    when the catalogue title it resolves to matches the claimed title (or no
    title was claimed). Disabling this function (forcing 0.0) collapses a
    matching DOI claim to ``suspicious`` — see the negative-control test.
    """
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()


def _surnames(authors: list) -> set:
    """Lowercased surname tokens from a list of 'First Last' / 'Last, First'."""
    out = set()
    for a in authors or []:
        a = (a or '').strip()
        if not a:
            continue
        surname = a.split(',')[0].strip() if ',' in a else a.split()[-1]
        surname = re.sub(r'[^a-z]', '', surname.lower())
        if len(surname) >= 2:
            out.add(surname)
    return out


def _result(state: str, cit: Citation, *, tier=None, method='none', **evidence) -> dict:
    return {
        'state': state,
        'tier': tier,
        'method': method,
        'citation': cit.to_dict(),
        'evidence': evidence,
    }


# ── Tier 1: DOI ──────────────────────────────────────────────────────────────

def _verify_doi(cit: Citation) -> dict | None:
    """CrossRef ``/works/{doi}``. Distinguishes 404 (suspicious) from error."""
    doi = cit.doi
    try:
        resp = base.http_get(f'{_CROSSREF_WORKS}/{doi}', headers=_headers(), timeout=_TIMEOUT)
    except Exception as e:
        logger.warning('[verify] DOI transport error for %s: %s', doi, e)
        return _result(UNVERIFIABLE, cit, tier=1, method='doi',
                       reason='CrossRef request failed', doi=doi)

    status = getattr(resp, 'status_code', 0)
    if status in (404, 410):
        return _result(SUSPICIOUS, cit, tier=1, method='doi',
                       reason='DOI does not resolve on CrossRef',
                       http_status=status, doi=doi,
                       checked=f'{_CROSSREF_WORKS}/{doi}')
    if not getattr(resp, 'ok', False):
        return _result(UNVERIFIABLE, cit, tier=1, method='doi',
                       reason=f'CrossRef returned HTTP {status}',
                       http_status=status, doi=doi)
    try:
        msg = (resp.json() or {}).get('message', {})
    except Exception as e:
        logger.warning('[verify] DOI JSON parse failed for %s: %s', doi, e)
        return _result(UNVERIFIABLE, cit, tier=1, method='doi',
                       reason='CrossRef JSON parse failed', doi=doi)

    found_title = (msg.get('title') or [''])[0] or ''
    if cit.title and found_title:
        sim = _title_similarity(cit.title, found_title)
        if sim < _WEAK_SIM:
            return _result(SUSPICIOUS, cit, tier=1, method='doi',
                           reason='DOI resolves to an unrelated title',
                           http_status=status, doi=doi,
                           claimed_title=cit.title, matched_title=found_title,
                           title_similarity=round(sim, 3),
                           checked=f'{_CROSSREF_WORKS}/{doi}')
        return _result(VERIFIED, cit, tier=1, method='doi',
                       reason='DOI resolves and title matches',
                       http_status=status, doi=doi,
                       matched_title=found_title, title_similarity=round(sim, 3),
                       checked=f'{_CROSSREF_WORKS}/{doi}')
    return _result(VERIFIED, cit, tier=1, method='doi',
                   reason='DOI resolves', http_status=status, doi=doi,
                   matched_title=found_title, checked=f'{_CROSSREF_WORKS}/{doi}')


# ── Tier 1: arXiv ─────────────────────────────────────────────────────────────

def _verify_arxiv(cit: Citation) -> dict | None:
    """arXiv Atom API. Empty <entry> → the id does not exist → suspicious."""
    aid = cit.arxiv_id
    try:
        resp = base.http_get(_ARXIV_API, params={'id_list': aid, 'max_results': '1'},
                             headers=_headers(), timeout=_TIMEOUT)
    except Exception as e:
        logger.warning('[verify] arXiv transport error for %s: %s', aid, e)
        return _result(UNVERIFIABLE, cit, tier=1, method='arxiv',
                       reason='arXiv request failed', arxiv_id=aid)
    if not getattr(resp, 'ok', False):
        return _result(UNVERIFIABLE, cit, tier=1, method='arxiv',
                       reason=f'arXiv returned HTTP {getattr(resp, "status_code", 0)}',
                       arxiv_id=aid)
    try:
        root = ET.fromstring(resp.text)
    except Exception as e:
        logger.warning('[verify] arXiv XML parse failed for %s: %s', aid, e)
        return _result(UNVERIFIABLE, cit, tier=1, method='arxiv',
                       reason='arXiv XML parse failed', arxiv_id=aid)

    entry = root.find('a:entry', _ATOM_NS)
    if entry is None:
        return _result(SUSPICIOUS, cit, tier=1, method='arxiv',
                       reason='arXiv id returns no record', arxiv_id=aid,
                       checked=f'{_ARXIV_API}?id_list={aid}')
    found_title = (entry.findtext('a:title', '', _ATOM_NS) or '').strip().replace('\n', ' ')
    if cit.title and found_title:
        sim = _title_similarity(cit.title, found_title)
        if sim < _WEAK_SIM:
            return _result(SUSPICIOUS, cit, tier=1, method='arxiv',
                           reason='arXiv id resolves to an unrelated title',
                           arxiv_id=aid, claimed_title=cit.title,
                           matched_title=found_title, title_similarity=round(sim, 3),
                           checked=f'{_ARXIV_API}?id_list={aid}')
        return _result(VERIFIED, cit, tier=1, method='arxiv',
                       reason='arXiv id resolves and title matches', arxiv_id=aid,
                       matched_title=found_title, title_similarity=round(sim, 3),
                       checked=f'{_ARXIV_API}?id_list={aid}')
    return _result(VERIFIED, cit, tier=1, method='arxiv',
                   reason='arXiv id resolves', arxiv_id=aid,
                   matched_title=found_title, checked=f'{_ARXIV_API}?id_list={aid}')


# ── Tier 2: title-only (fuzzy) ────────────────────────────────────────────────

def _verify_title(cit: Citation) -> dict:
    """Title-only verification: CrossRef first, Semantic Scholar fallback.

    CrossRef has poor coverage of arXiv preprints and ML/NLP conference papers
    (NeurIPS/ICLR/ACL) — exactly the corpus this detector polices. So when
    CrossRef yields no ``verified``, fall back to Semantic Scholar's
    ``paper/search`` (which DOES index preprints). Either catalogue producing
    a strong title match → ``verified``; neither matching → ``unverifiable``.

    Discipline: a title-only claim is NEVER ``suspicious``. Coverage gaps,
    books, datasets, software, non-English titles and rate-limits all
    legitimately fail to match and degrade to ``unverifiable``.
    """
    cr = _verify_title_crossref(cit)
    if cr['state'] == VERIFIED:
        return cr
    # CrossRef miss → try Semantic Scholar (preprint/conference coverage).
    s2 = _verify_title_s2(cit)
    if s2 is not None and s2['state'] == VERIFIED:
        return s2
    # Neither catalogue confirmed — keep the CrossRef coverage-gap verdict
    # (already UNVERIFIABLE, never SUSPICIOUS).
    return cr


def _verify_title_crossref(cit: Citation) -> dict:
    """CrossRef bibliographic search → VERIFIED on a (corroborated) strong
    match, else UNVERIFIABLE. Author-surname overlap and year RESCUE a
    borderline match up to ``verified`` (corroboration), never condemn."""
    title = cit.title
    try:
        resp = base.http_get(_CROSSREF_WORKS,
                             params={'query.bibliographic': title, 'rows': 5,
                                     'select': 'title,author,issued,DOI,score'},
                             headers=_headers(), timeout=_TIMEOUT)
    except Exception as e:
        logger.warning('[verify] title transport error for %r: %s', title[:60], e)
        return _result(UNVERIFIABLE, cit, tier=2, method='crossref_title',
                       reason='CrossRef request failed')
    if not getattr(resp, 'ok', False):
        return _result(UNVERIFIABLE, cit, tier=2, method='crossref_title',
                       reason=f'CrossRef returned HTTP {getattr(resp, "status_code", 0)}')
    try:
        items = (resp.json() or {}).get('message', {}).get('items', [])
    except Exception as e:
        logger.warning('[verify] title JSON parse failed for %r: %s', title[:60], e)
        return _result(UNVERIFIABLE, cit, tier=2, method='crossref_title',
                       reason='CrossRef JSON parse failed')

    best = None
    best_sim = 0.0
    for it in items:
        cand = (it.get('title') or [''])[0] or ''
        sim = _title_similarity(title, cand)
        if sim > best_sim:
            best_sim, best = sim, it
    if best is None:
        return _result(UNVERIFIABLE, cit, tier=2, method='crossref_title',
                       reason='no CrossRef candidates', checked=_CROSSREF_WORKS)

    best_title = (best.get('title') or [''])[0] or ''
    cand_authors = []
    for a in best.get('author', []) or []:
        nm = f"{a.get('given', '')} {a.get('family', '')}".strip()
        if nm:
            cand_authors.append(nm)
    yr = ''
    dp = (best.get('issued') or {}).get('date-parts', [[]])
    if dp and dp[0]:
        yr = str(dp[0][0])
    author_overlap = bool(_surnames(cit.authors) & _surnames(cand_authors))
    year_match = bool(cit.year and yr and cit.year == yr)

    common = dict(checked=_CROSSREF_WORKS, matched_title=best_title,
                  matched_doi=best.get('DOI', ''), title_similarity=round(best_sim, 3),
                  author_overlap=author_overlap, year_match=year_match)
    if best_sim >= _STRONG_SIM:
        return _result(VERIFIED, cit, tier=2, method='crossref_title',
                       reason='strong title match', **common)
    if best_sim >= _WEAK_SIM and (author_overlap or year_match):
        return _result(VERIFIED, cit, tier=2, method='crossref_title',
                       reason='title match corroborated by author/year', **common)
    return _result(UNVERIFIABLE, cit, tier=2, method='crossref_title',
                   reason='no strong title match (treated as coverage gap, not fabrication)',
                   **common)


def _verify_title_s2(cit: Citation) -> dict | None:
    """Semantic Scholar ``paper/search`` fallback for the title-only path.

    Returns a ``verified`` verdict ONLY on a strong title match (optionally
    corroborated by year, which is all S2 cheaply gives us); otherwise
    ``unverifiable``. Returns ``None`` on transport/parse/rate-limit failure
    so the coordinator keeps the CrossRef verdict. It can NEVER return
    ``suspicious`` — S2 coverage gaps are not evidence of fabrication.
    """
    title = cit.title
    try:
        resp = base.http_get(_S2_SEARCH,
                             params={'query': title, 'limit': 5,
                                     'fields': 'title,year,externalIds'},
                             headers=_ss_headers(), timeout=_TIMEOUT)
    except Exception as e:
        logger.warning('[verify] S2 title transport error for %r: %s', title[:60], e)
        return None
    if getattr(resp, 'status_code', 0) == 429:
        logger.info('[verify] S2 rate-limited for %r — degrade to CrossRef verdict', title[:60])
        return None
    if not getattr(resp, 'ok', False):
        return None
    try:
        items = (resp.json() or {}).get('data', []) or []
    except Exception as e:
        logger.warning('[verify] S2 JSON parse failed for %r: %s', title[:60], e)
        return None

    best = None
    best_sim = 0.0
    for it in items:
        sim = _title_similarity(title, it.get('title') or '')
        if sim > best_sim:
            best_sim, best = sim, it
    if best is None:
        return _result(UNVERIFIABLE, cit, tier=2, method='s2_title',
                       reason='no Semantic Scholar candidates', checked=_S2_SEARCH)

    best_title = best.get('title') or ''
    yr = str(best.get('year')) if best.get('year') else ''
    year_match = bool(cit.year and yr and cit.year == yr)
    common = dict(checked=_S2_SEARCH, matched_title=best_title,
                  title_similarity=round(best_sim, 3), year_match=year_match,
                  source='Semantic Scholar')
    if best_sim >= _STRONG_SIM:
        return _result(VERIFIED, cit, tier=2, method='s2_title',
                       reason='strong title match on Semantic Scholar', **common)
    if best_sim >= _WEAK_SIM and year_match:
        return _result(VERIFIED, cit, tier=2, method='s2_title',
                       reason='title match on Semantic Scholar corroborated by year', **common)
    return _result(UNVERIFIABLE, cit, tier=2, method='s2_title',
                   reason='no strong Semantic Scholar match (coverage gap, not fabrication)',
                   **common)


# ── public entry points ───────────────────────────────────────────────────────

def verify_citation(cit: Citation) -> dict:
    """Verify a single citation → a three-state verdict dict.

    Tier selection: a concrete DOI wins (most authoritative), then an arXiv
    id, then a title-only fuzzy check. With neither id nor title the verdict
    is ``unverifiable`` (Tier 3 — nothing to check against).
    """
    if cit.doi:
        out = _verify_doi(cit)
        if out is not None:
            return out
    if cit.arxiv_id:
        out = _verify_arxiv(cit)
        if out is not None:
            return out
    if cit.title and len(_normalize_title(cit.title)) >= 6:
        return _verify_title(cit)
    return _result(UNVERIFIABLE, cit, tier=3, method='none',
                   reason='no DOI/arXiv id and no usable title to verify')


def verify_citations(citations: list, *, max_workers: int = _MAX_WORKERS) -> list:
    """Verify many citations concurrently (bounded pool). Order preserved."""
    cits = list(citations)
    if not cits:
        return []
    results: list = [None] * len(cits)
    workers = max(1, min(max_workers, len(cits)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(verify_citation, c): i for i, c in enumerate(cits)}
        for fut in as_completed(futs):
            i = futs[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # pragma: no cover - defensive
                logger.error('[verify] verify_citation crashed: %s', e, exc_info=True)
                results[i] = _result(UNVERIFIABLE, cits[i], reason='verifier crashed')
    return results


def verify_bibtex(bibtex_text: str, **kw) -> list:
    """Parse a ``.bib`` string and verify every entry."""
    return verify_citations(parse_bibtex(bibtex_text), **kw)


def verify_references(references_text: str, **kw) -> list:
    """Parse a freeform reference blob and verify every entry."""
    return verify_citations(parse_references(references_text), **kw)


def summarize(results: list) -> dict:
    """Aggregate verdicts → counts + the suspicious sublist (for a report card).

    The product surfaces gate display on ``summary['suspicious'] != []`` — an
    all-clear (or only-unverifiable) run renders nothing.
    """
    counts = {VERIFIED: 0, SUSPICIOUS: 0, UNVERIFIABLE: 0}
    suspicious = []
    for r in results:
        counts[r['state']] = counts.get(r['state'], 0) + 1
        if r['state'] == SUSPICIOUS:
            suspicious.append(r)
    return {
        'total': len(results),
        'counts': counts,
        'suspicious': suspicious,
        'has_suspicious': bool(suspicious),
    }
