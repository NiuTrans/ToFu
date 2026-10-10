"""Academic discovery adapters; entry point AcademicDiscovery is request-scoped.

Uses the canonical HTTP client and existing Semantic Scholar auth headers.
The display-oriented vertical drops DOI/paper IDs and swallows source failures,
so graph payloads are decoded here rather than via its rendered search result.
No adapter is instantiated implicitly for an injected plain search callable.
"""
from __future__ import annotations

import json
import re
import time

from collections import deque
from itertools import islice
from urllib.parse import quote, unquote

MAX_API_REQUESTS = 18
MAX_RESPONSE_BYTES = 1024 * 1024
API_TIMEOUT_SECONDS = 12
S2_BASE = 'https://api.semanticscholar.org/graph/v1'
CROSSREF_BASE = 'https://api.crossref.org/works'


def normalize_doi(value):
    if not isinstance(value, str):
        return ''
    value = unquote(value.strip()).lower()
    value = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '', value)
    return value if len(value) <= 256 and re.fullmatch(r'10\.\d{4,9}/[^\s]+', value) else ''


def nonnegative_integer(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def bibliometrics(item):
    year = nonnegative_integer(item.get('year'))

    if year is None:
        published = item.get('published')
        if isinstance(published, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T.*)?', published):
            year = int(published[:4])
    count = nonnegative_integer(item.get('citation_count'))
    source = str(item.get('metadata_source') or item.get('source') or '')[:80]
    return {'year': year if year is not None and year <= 9999 else None,
            'venue': item.get('venue', '')[:500] if isinstance(item.get('venue'), str) else '',
            'citation_count': count, 'metadata_source': source,
            'citation_count_source': str(item.get('citation_count_source') or source)[:80] if count is not None else ''}


def semantic_record(item):
    ext = item.get('externalIds') if isinstance(item.get('externalIds'), dict) else {}
    publication_venue = item.get('publicationVenue')
    publication_venue = publication_venue if isinstance(publication_venue, dict) else {}
    return {'arxiv_id': ext.get('ArXiv', ''), 'doi': ext.get('DOI', ''),
            'semantic_scholar_id': item.get('paperId', ''),
            'title': item.get('title', ''), 'abstract': item.get('abstract', ''),
            'year': item.get('year'), 'source': 'semantic_scholar',
            'venue': item.get('venue') or publication_venue.get('name', ''),
            'citation_count': nonnegative_integer(item.get('citationCount'))}


def crossref_record(item):
    titles = item.get('title') or []
    date = item.get('published') or item.get('issued') or {}
    parts = (date.get('date-parts') or []) if isinstance(date, dict) else []
    year = parts[0][0] if parts and isinstance(parts[0], list) and parts[0] else None
    venues = item.get('container-title') or []
    return {'doi': item.get('DOI', ''),
            'title': titles[0] if isinstance(titles, list) and titles else '',
            'abstract': item.get('abstract', ''), 'source': 'crossref',
            'year': year, 'venue': venues[0] if isinstance(venues, list) and venues else '',
            'citation_count': nonnegative_integer(item.get('is-referenced-by-count'))}


class AcademicDiscovery:
    """Sequential, finite multi-source search and real forward/backward edges.

    Pass ``search=adapter.search`` to discover_corpus; it picks up this owner's
    citation_search. Tests can inject get_json to avoid all HTTP and sleeps.
    Status receipts distinguish successful empty results from unavailable APIs.
    """

    def __init__(self, arxiv_search, *, abort_check=None, get_json=None):
        self.arxiv_search = arxiv_search
        self.abort_check = abort_check
        self.get_json = get_json
        self.requests = 0
        self.statuses = deque(maxlen=64)
        self._last_request = 0.0

    def _json(self, url, params):
        if self.abort_check and self.abort_check():
            raise InterruptedError('discovery cancelled')
        if self.requests >= MAX_API_REQUESTS:
            raise RuntimeError('academic request budget exhausted')
        self.requests += 1
        if self.get_json:
            return self.get_json(url, params)
        from lib.http_client import http_get
        from tofu_search.search.vertical.semantic_scholar import _ss_headers
        # Fixed authorities only, no followed redirects or metadata-supplied URLs.
        if not (url.startswith(S2_BASE + '/') or url == CROSSREF_BASE):
            raise ValueError('unsupported academic endpoint')
        while time.monotonic() - self._last_request < 1.0:
            if self.abort_check and self.abort_check():
                raise InterruptedError('discovery cancelled')
            time.sleep(0.05)
        self._last_request = time.monotonic()
        headers = _ss_headers() if url.startswith(S2_BASE + '/') else None
        with http_get(url, params=params, headers=headers, timeout=API_TIMEOUT_SECONDS,
                      allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise RuntimeError(f'academic HTTP {response.status_code}')
            body = bytearray()
            started = time.monotonic()
            for chunk in response.iter_content(chunk_size=16384):
                if self.abort_check and self.abort_check():
                    raise InterruptedError('discovery cancelled')
                if time.monotonic() - started > API_TIMEOUT_SECONDS:
                    raise TimeoutError('academic response deadline')
                if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError('academic response too large')
                body.extend(chunk)
            return json.loads(body)

    def _source(self, source, operation, fetch):
        if self.abort_check and self.abort_check():
            raise InterruptedError('discovery cancelled')
        try:
            rows = fetch()
        except InterruptedError:
            raise
        except Exception as exc:
            self.statuses.append({'source': source, 'operation': operation,
                                  'status': 'unavailable', 'error': type(exc).__name__})
            return []
        self.statuses.append({'source': source, 'operation': operation,
                              'status': 'available', 'retrieved': len(rows)})
        return rows

    def search(self, query, *, max_results):
        limit = max(0, min(40, max_results))
        if not limit:
            return []
        # Reserve source shares so arXiv cannot consume every metadata slot.
        share = max(1, limit // 3)
        rows = self._source('arxiv', 'search', lambda: [
            dict(hit, source='arxiv') for hit in islice(
                iter(self.arxiv_search(query, max_results=share) or ()), share)
            if isinstance(hit, dict)])
        rows += self._source('semantic_scholar', 'search', lambda: [
            semantic_record(hit) for hit in self._json(S2_BASE + '/paper/search', {
                'query': query[:2000], 'limit': share,
                'fields': 'title,abstract,year,externalIds,venue,publicationVenue,citationCount'})['data'][:share]])
        remaining = max(1, limit - 2 * share)
        rows += self._source('crossref', 'search', lambda: [
            crossref_record(hit) for hit in self._json(CROSSREF_BASE, {
                'query': query[:2000], 'rows': remaining})['message']['items'][:remaining]])
        return rows[:limit]

    def citation_search(self, paper, *, direction, max_results):
        if direction not in ('references', 'citations'):
            raise ValueError('invalid citation direction')
        ref = paper.get('semantic_scholar_id')
        if not ref and paper.get('doi'):
            ref = 'DOI:' + paper['doi']
        if not ref and paper.get('arxiv_id'):
            ref = 'ARXIV:' + paper['arxiv_id']
        if not ref:
            return []
        limit = max(0, min(40, max_results))
        if not limit:
            return []
        key = 'citedPaper' if direction == 'references' else 'citingPaper'
        return self._source('semantic_scholar', direction, lambda: [
            semantic_record(hit[key]) for hit in self._json(
                S2_BASE + '/paper/' + quote(ref, safe='') + '/' + direction,
                {'limit': limit, 'fields': 'title,abstract,year,externalIds,venue,publicationVenue,citationCount'})['data'][:limit]
            if isinstance(hit.get(key), dict)])

    def status_snapshot(self):
        return list(self.statuses)


def default_discovery_adapter(arxiv_search, *, abort_check=None):
    """Explicit recipe hook: network policy is chosen by the caller, not mocks."""
    return AcademicDiscovery(arxiv_search, abort_check=abort_check)
