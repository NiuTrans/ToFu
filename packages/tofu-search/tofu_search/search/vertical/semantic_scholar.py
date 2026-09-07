"""Semantic Scholar vertical — related-work / citation graph for a paper."""

import os
import re

from tofu_search.search.vertical import base
from tofu_search.search.vertical.base import _HEADERS, _TIMEOUT, logger

TYPE = 'semantic_scholar'
DOMAIN = 'academic'


def detect(q):
    """Detect a Semantic Scholar related-work intent. Returns tuple or None.

    Triggers on phrases like 'papers related to X', 'papers similar to X',
    'papers citing X', 'what cites X'. The identifier is the target paper
    title or arXiv id; mode ('related' vs 'citations') goes into params.
    """
    m = re.search(
        r'\b(?:papers?|works?|research|studies)\s+'
        r'(?:that\s+)?(related\s+to|similar\s+to|citing|that\s+cite|building\s+on)\s+(.+)',
        q, re.IGNORECASE,
    )
    if not m:
        m = re.search(r'\b(?:what|which\s+papers?)\s+(cite[sd]?)\s+(.+)', q, re.IGNORECASE)
    if not m:
        return None

    verb = m.group(1).lower()
    target = m.group(2).strip().rstrip('?.!').strip()
    if len(target) < 3:
        return None

    mode = 'citations' if 'cite' in verb else 'related'
    return (TYPE, target, {'mode': mode})


def _ss_headers():
    """Semantic Scholar headers — include API key from env if available."""
    h = dict(_HEADERS)
    key = os.environ.get('SEMANTIC_SCHOLAR_API_KEY', '').strip()
    if key:
        h['x-api-key'] = key
    return h


def search(identifier, params):
    """Semantic Scholar: related-work / citation graph for a paper.

    ``mode='related'`` → relevance-ranked keyword search (papers like X).
    ``mode='citations'`` → resolve the target paper, then list newer papers
    that cite it. The target may be a title or an arXiv id.
    Keyless access is heavily rate-limited; set SEMANTIC_SCHOLAR_API_KEY to
    raise the ceiling. 429s degrade gracefully to None.
    """
    mode = (params or {}).get('mode', 'related')
    target = (identifier or '').strip()
    bse = 'https://api.semanticscholar.org/graph/v1'

    try:
        arxiv_m = re.search(r'\b(\d{4}\.\d{4,5})\b', target)

        if mode == 'citations':
            paper_ref = f'arXiv:{arxiv_m.group(1)}' if arxiv_m else None
            if not paper_ref:
                resp = base.http_get(f'{bse}/paper/search',
                                     params={'query': target, 'limit': 1, 'fields': 'title,paperId'},
                                     headers=_ss_headers(), timeout=_TIMEOUT)
                if not resp.ok:
                    logger.warning('[Vertical] S2 search HTTP %d for %r', resp.status_code, target)
                    return None
                hits = (resp.json() or {}).get('data', [])
                if not hits:
                    return None
                paper_ref = hits[0].get('paperId')

            resp = base.http_get(f'{bse}/paper/{paper_ref}/citations',
                                 params={'limit': 15, 'fields': 'title,year,citationCount,externalIds'},
                                 headers=_ss_headers(), timeout=_TIMEOUT)
            if not resp.ok:
                logger.warning('[Vertical] S2 citations HTTP %d for %s', resp.status_code, paper_ref)
                return None
            data = (resp.json() or {}).get('data', [])
            items = [d.get('citingPaper', {}) for d in data]
            heading = f'## Papers citing "{target}" (Semantic Scholar)'
        else:
            resp = base.http_get(f'{bse}/paper/search',
                                 params={'query': target, 'limit': 15,
                                         'fields': 'title,year,citationCount,externalIds,abstract'},
                                 headers=_ss_headers(), timeout=_TIMEOUT)
            if not resp.ok:
                logger.warning('[Vertical] S2 search HTTP %d for %r', resp.status_code, target)
                return None
            items = (resp.json() or {}).get('data', [])
            heading = f'## Papers related to "{target}" (Semantic Scholar)'

        items = [it for it in items if it.get('title')]
        items.sort(key=lambda it: it.get('citationCount', 0) or 0, reverse=True)
        if not items:
            return None

        parts = [heading, '']
        structured = []
        for it in items[:15]:
            title = (it.get('title') or '').strip().replace('\n', ' ')
            year = it.get('year') or ''
            cites = it.get('citationCount', 0) or 0
            ext = it.get('externalIds') or {}
            arxiv_id = ext.get('ArXiv')
            line = f'- **{title}** ({year}, cited {cites:,}×)'
            url = f'https://arxiv.org/abs/{arxiv_id}' if arxiv_id else ''
            if url:
                line += f'\n  {url}'
            parts.append(line)
            structured.append({
                'title': title,
                'snippet': (it.get('abstract') or '')[:240] if isinstance(it.get('abstract'), str) else '',
                'url': url,
                'year': year,
                'citations': cites,
                'arxiv_id': arxiv_id or '',
                'type': TYPE,
                'source': 'Semantic Scholar',
            })

        return {'domain': DOMAIN, 'type': TYPE, 'identifier': target,
                'content': '\n'.join(parts), 'source': 'Semantic Scholar',
                'items': structured}
    except Exception as e:
        logger.warning('[Vertical] Semantic Scholar lookup failed for %r: %s', identifier, e)
        return None
