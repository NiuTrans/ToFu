"""arXiv vertical — paper metadata via the arXiv Atom API."""

import re
import time
import xml.etree.ElementTree as ET

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _HEADERS, _TIMEOUT, logger

TYPE = 'arxiv'
DOMAIN = 'academic'

# Modern arXiv id: 2301.07041 / 2301.07041v2 (optionally prefixed "arXiv:").
_MODERN_RE = re.compile(r'(?:^|arxiv[:\s]+)(\d{4}\.\d{4,5}(?:v\d+)?)', re.IGNORECASE)
# Legacy id: hep-th/9901001, math.AG/0509025 (archive[.subclass]/YYMMnnn).
_LEGACY_RE = re.compile(
    r'(?:^|arxiv[:\s]+)([a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?)', re.IGNORECASE)


def detect(q):
    """Detect a modern or legacy arXiv identifier in the query."""
    m = _MODERN_RE.search(q)
    if m:
        return (TYPE, m.group(1), {})
    m = _LEGACY_RE.search(q)
    if m:
        return (TYPE, m.group(1), {})
    return None


def _parse_entry(entry, ns):
    """Extract one Atom <entry> into a plain paper dict.

    Shared by the id lookup and the free-text query path so both read the same
    Atom fields the same way — the parsing lives once.
    """
    raw_id = (entry.findtext('a:id', '', ns) or '').strip()
    m = re.search(r'/abs/(.+?)(?:v\d+)?$', raw_id)
    arxiv_id = m.group(1) if m else ''
    title = re.sub(r'\s+', ' ', (entry.findtext('a:title', '', ns) or '')).strip()
    summary = re.sub(r'\s+', ' ', (entry.findtext('a:summary', '', ns) or '')).strip()
    authors = [a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)]
    cat = entry.find('{http://arxiv.org/schemas/atom}primary_category')
    return {
        'arxiv_id': arxiv_id,
        'title': title,
        'authors': [a for a in authors if a],
        'summary': summary,
        'published': (entry.findtext('a:published', '', ns) or '')[:10],
        'primary_category': (cat.get('term') if cat is not None else ''),
        'pdf_url': f'https://arxiv.org/pdf/{arxiv_id}.pdf' if arxiv_id else '',
        'abs_url': f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else '',
    }


# ── Free-text neighbour search ────────────────────────────────────
#
# ★ arXiv query-syntax facts, MEASURED against the live API (2026-07-28) on six
# real research ideas. Each of these looked correct in a mock and returned ZERO
# for all six; they are pinned by offline assertions in
# tests/test_arxiv_query_syntax.py so the next caller cannot re-learn them the
# hard way:
#
#   1. A QUOTED multi-word value is an exact PHRASE match. `ti:"predictive
#      delta"` needs those two words adjacent in a title — and a NOVEL idea's
#      distinguishing phrase is by definition in no existing title. 0 results,
#      always. Identity terms must be separate unquoted terms.
#   2. Those terms must be OR-ed, not AND-ed. `ti:predictive AND ti:delta`
#      demands every identity word in one title: also 0/6 measured.
#   3. The DOMAIN leg stays a quoted phrase on purpose — it is shared field
#      vocabulary ("KV cache compression"), and phrase-matching it is what
#      keeps the search from drifting out of the field.
#
# Measured on the fielded form `(ti:a OR ti:b) AND all:"domain"`: 96% on-topic
# with 0% pairwise overlap across the six ideas, versus 0% on-topic / 80%
# overlap for a flat prose `all:` query.
#
# Deliberately NOT here: deciding WHICH terms are identity vs domain. That
# needs cross-item batch context (which terms several items share), so it is the
# caller's strategy layer. This function's contract is "give me terms + a domain
# constraint, get neighbours back".

#: Characters that break the arXiv query parser. An unescaped `*` in one real
#: idea produced an HTTP 500, which a bare `except` then swallowed into "no
#: prior art" — the most dangerous false negative for a novelty check.
_UNSAFE_QUERY_CHARS = re.compile(r'[*()\[\]{}"\'`,;:!?/\\|<>+=~^$#@&]')


def sanitize_terms(terms):
    """Return `terms` with parser-breaking characters removed.

    Accepts a string or an iterable of strings. Returns a list of clean,
    non-empty tokens (never None), so a caller cannot accidentally put prose
    or markup on the wire.
    """
    if isinstance(terms, str):
        terms = terms.split()
    out = []
    for t in (terms or []):
        clean = _UNSAFE_QUERY_CHARS.sub(' ', str(t or ''))
        out.extend(w for w in clean.split() if w)
    return out


def build_query(identity_terms, domain_terms=None, *, field='ti'):
    """Build an arXiv `search_query` string; returns ``(query, mode)``.

    ``mode`` is one of:
      * ``'fielded'`` — ``(ti:a OR ti:b) AND all:"domain"``: both legs present.
      * ``'domain'``  — ``all:"domain"``: no identity terms, field constraint only.
      * ``'terms'``   — ``all:"a b"``: no domain constraint available.
      * ``'empty'``   — nothing usable survived sanitization.

    ``field`` selects the identity leg's arXiv field (``ti`` = title,
    ``abs`` = abstract). Widening from title to abstract is how a caller can
    give a genuinely-new idea a second chance before concluding there is no
    prior art — measured: 2 of 6 real ideas had identity terms too new for ANY
    title and only found neighbours via the abstract leg.
    """
    ident = sanitize_terms(identity_terms)
    dom = ' '.join(sanitize_terms(domain_terms))
    if ident and dom:
        legs = ' OR '.join(f'{field}:{t}' for t in ident)
        return f'({legs}) AND all:"{dom}"', 'fielded'
    if dom:
        return f'all:"{dom}"', 'domain'
    if ident:
        # ★ NOT phrase-quoted — Fact 1 applies to this leg too. `all:"a b c d"`
        # demands those four words ADJACENT somewhere in the record, so a
        # multi-term free-text query matches almost nothing (measured: the
        # quoted form returned 0 for a real 5-term idea title that the unquoted
        # form answers with 5 papers). Unquoted terms are AND-ed by arXiv, which
        # is the free-text behaviour callers expect.
        return f'all:{" ".join(ident)}', 'terms'
    return '', 'empty'


#: arXiv rate-limits aggressively (HTTP 429) and occasionally times out.
#: Retry lives HERE, in the shared vertical, rather than in any one caller:
#: the novelty gate calls this directly (it has structured legs to pass and
#: must not flatten them through a free-text adapter), so putting backoff in
#: an adapter leaves the ONE caller whose correctness depends on retrieval
#: with no resilience at all. Measured: a burst of 429s made 3 of 6 real ideas
#: report an empty prior-art basis — which reads exactly like "nothing has been
#: published on this", the most dangerous false positive a novelty check can
#: produce.
_SEARCH_RETRIES = 3
_SEARCH_RETRY_SLEEP = 3.0


def search_by_query(identity_terms, domain_terms=None, *, field='ti',
                    max_results=5):
    """Find arXiv papers NEAR a set of terms. Returns a result envelope.

    ★ The return value deliberately distinguishes "asked properly, nothing
    matched" from "could not ask" — collapsing both into ``[]`` would strip the
    caller of the signal it needs to decide whether to widen the query. A
    novelty check that cannot tell those apart will report "no prior art" for a
    query that was merely too narrow.

    Returns::

        {
          'ok': bool,          # did the request itself succeed?
          'query': str,        # exactly what went on the wire
          'mode': str,         # build_query mode ('fielded'|'domain'|'terms'|'empty')
          'papers': list,      # parsed papers (possibly empty)
          'outcome': str,      # 'hits' | 'no_matches' | 'unusable_query' | 'request_failed'
          'error': str,        # populated only when outcome == 'request_failed'
        }

    ``outcome`` values:
      * ``hits``            — the query ran and matched at least one paper.
      * ``no_matches``      — the query ran and legitimately matched nothing;
                              widening the field (``ti`` → ``abs``) or dropping
                              the identity leg is the sensible next step.
      * ``unusable_query``  — nothing survived sanitization; NOT evidence about
                              the literature, and no request was made.
      * ``request_failed``  — transport/parse error; also NOT evidence.
    """
    query, mode = build_query(identity_terms, domain_terms, field=field)
    envelope = {'ok': False, 'query': query, 'mode': mode, 'papers': [],
                'outcome': 'unusable_query', 'error': ''}
    if mode == 'empty':
        logger.warning('[Vertical] arXiv query unusable after sanitization '
                       '(identity=%r domain=%r)', identity_terms, domain_terms)
        return envelope

    n = max(1, min(int(max_results or 5), 50))
    root = None
    for attempt in range(1, _SEARCH_RETRIES + 1):
        try:
            resp = base.http_get(
                'http://export.arxiv.org/api/query',
                params={'search_query': query, 'start': '0',
                        'max_results': str(n), 'sortBy': 'relevance',
                        'sortOrder': 'descending'},
                headers=_HEADERS, timeout=_TIMEOUT,
            )
            if not resp.ok:
                envelope.update(outcome='request_failed',
                                error=f'HTTP {resp.status_code}')
                logger.warning('[Vertical] arXiv query HTTP %s for %r '
                               '(attempt %d/%d)', resp.status_code, query,
                               attempt, _SEARCH_RETRIES)
            else:
                root = ET.fromstring(resp.text)
                envelope['error'] = ''
                break
        except Exception as e:
            envelope.update(outcome='request_failed',
                            error=f'{type(e).__name__}: {e}')
            logger.warning('[Vertical] arXiv query failed for %r '
                           '(attempt %d/%d): %s', query, attempt,
                           _SEARCH_RETRIES, e)
        if attempt < _SEARCH_RETRIES:
            time.sleep(_SEARCH_RETRY_SLEEP * attempt)  # linear backoff

    if root is None:
        return envelope
    if attempt > 1:
        logger.info('[Vertical] arXiv query %r recovered on attempt %d',
                    query, attempt)

    ns = {'a': 'http://www.w3.org/2005/Atom'}
    papers = [_parse_entry(en, ns) for en in root.findall('a:entry', ns)]
    papers = [p for p in papers if p.get('arxiv_id')]
    envelope.update(ok=True, papers=papers,
                    outcome='hits' if papers else 'no_matches')
    logger.info('[Vertical] arXiv query %r (%s) → %d paper(s) [%s]',
                query, mode, len(papers), envelope['outcome'])
    return envelope


def search(identifier, params):
    """Query arXiv API for paper details."""
    try:
        resp = base.http_get(
            'http://export.arxiv.org/api/query',
            params={'id_list': identifier, 'max_results': '1'},
            headers=_HEADERS, timeout=_TIMEOUT,
        )
        if not resp.ok:
            return None

        root = ET.fromstring(resp.text)
        ns = {'a': 'http://www.w3.org/2005/Atom'}
        entry = root.find('a:entry', ns)
        if entry is None:
            return None

        title = (entry.findtext('a:title', '', ns) or '').strip().replace('\n', ' ')
        summary = (entry.findtext('a:summary', '', ns) or '').strip()
        published = (entry.findtext('a:published', '', ns) or '')[:10]
        authors = [a.findtext('a:name', '', ns) for a in entry.findall('a:author', ns)]
        pdf_links = [link.get('href') for link in entry.findall('a:link', ns)
                     if link.get('type') == 'application/pdf']
        categories = [c.get('term') for c in entry.findall('a:category', ns) if c.get('term')]

        parts = [f'## {title}', f'**arXiv**: {identifier}']
        if published:
            parts.append(f'**Published**: {published}')
        if authors:
            parts.append(f'**Authors**: {", ".join(authors[:10])}')
        if categories:
            parts.append(f'**Categories**: {", ".join(categories[:5])}')
        if pdf_links:
            parts.append(f'**PDF**: {pdf_links[0]}')
        parts.append(f'\n**Abstract**: {summary}')

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': identifier,
                'content': '\n'.join(parts), 'source': 'arXiv'}
    except Exception as e:
        logger.warning('[Vertical] arXiv lookup failed for %s: %s', identifier, e)
        return None
