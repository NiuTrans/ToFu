"""Pluggable resource indexing for self-evolution path planning.

Responsibility: one indexer interface returning contract-shaped resource
references, plus the first implementation — the history indexer, which mines
the owner's own settled episodes (the cheapest, highest-prior resource).
Entry points: ``HistoryIndexer`` / ``index_all``. Dependencies: the
goals/episodes stores; no network, no model calls.

An indexer returns dicts matching the plan contract's ``resource_refs``
items: ``ref_id`` / ``indexer`` / ``locator`` / ``relevance`` /
``provenance``. Project, academic, and web adapters attach here later; the
interface is the seam, and ``index_all`` merges and bounds their output.
"""
from __future__ import annotations

import re

from lib.log import get_logger

from .episodes import list_episodes
from .goals import list_goals

logger = get_logger(__name__)

MAX_INDEX_RESULTS = 32

_WORD_RE = re.compile(r'[a-z0-9]{2,}')
_CJK_RUN_RE = re.compile(r'[一-鿿]{2,}')

_STOPWORDS = frozenset({
    'the', 'and', 'for', 'with', 'that', 'this', 'from', 'into', 'are',
    'was', 'were', 'has', 'have', 'had', 'not', 'but', 'all', 'can',
    'our', 'out', 'use', 'using', 'used', 'how', 'what', 'when', 'then',
})


def _terms(text: str) -> set[str]:
    """Lowercase alnum words plus CJK bigrams; honest recall for zh/en."""
    lowered = (text or '').lower()
    terms = {w for w in _WORD_RE.findall(lowered) if w not in _STOPWORDS}
    for run in _CJK_RUN_RE.findall(text or ''):
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


class HistoryIndexer:
    """Mine the owner's settled episodes for paths that already ran."""

    name = 'history'

    def index(self, query: str, *, user_id, limit: int = 8) -> list[dict]:
        query_terms = _terms(query)
        if not query_terms:
            return []
        goals = {g.get('goal_id'): g
                 for g in list_goals(user_id=user_id, limit=200)}
        scored = []
        for episode in list_episodes(user_id=user_id, limit=200):
            if episode.get('status') not in ('success', 'partial', 'failure'):
                continue
            goal = goals.get(episode.get('goal_id')) or {}
            text = f"{goal.get('statement', '')} {goal.get('domain', '')}"
            overlap = query_terms & _terms(text)
            if not overlap:
                continue
            relevance = round(len(overlap) / len(query_terms), 4)
            scored.append((relevance, episode, goal))
        scored.sort(key=lambda item: (-item[0], item[1]['episode_id']))
        bounded = max(1, min(int(limit or 8), MAX_INDEX_RESULTS))
        return [{
            'ref_id': f"episode:{episode['episode_id']}",
            'indexer': 'history',
            'locator': episode['episode_id'],
            'relevance': relevance,
            'provenance': (f"local {episode['status']} episode on "
                           f"{goal.get('domain', 'unknown')}"),
        } for relevance, episode, goal in scored[:bounded]]


def index_all(query: str, indexers, *, user_id, limit: int = 16) -> list[dict]:
    """Fan out over indexers, merge by relevance, bound the result."""
    merged = []
    for indexer in indexers:
        merged.extend(indexer.index(query, user_id=user_id,
                                    limit=MAX_INDEX_RESULTS))
    merged.sort(key=lambda ref: (-float(ref.get('relevance') or 0),
                                 str(ref.get('ref_id') or '')))
    bounded = max(1, min(int(limit or 16), MAX_INDEX_RESULTS))
    return merged[:bounded]


_GITHUB_SEARCH_URL = 'https://api.github.com/search/repositories'
_GITHUB_TIMEOUT_S = 10.0
_GITHUB_MAX_BYTES = 1024 * 1024


def _github_fetch(url: str, params: dict) -> dict:
    """One bounded read against the fixed GitHub search authority.

    Same posture as academic discovery: fixed endpoint, no redirects, capped
    body, explicit timeout. Unauthenticated search suffices for ranking
    candidates; no credential is read or sent.
    """
    import json as _json

    from lib.http_transport.client import http_get
    if url != _GITHUB_SEARCH_URL:
        raise ValueError('unsupported project-index endpoint')
    with http_get(url, params=params,
                  headers={'Accept': 'application/vnd.github+json'},
                  timeout=_GITHUB_TIMEOUT_S, allow_redirects=False,
                  stream=True) as response:
        if response.status_code != 200:
            raise RuntimeError(f'github search HTTP {response.status_code}')
        body = bytearray()
        for chunk in response.iter_content(chunk_size=16384):
            if len(body) + len(chunk) > _GITHUB_MAX_BYTES:
                raise ValueError('github search response too large')
            body.extend(chunk)
        return _json.loads(body)


class ProjectIndexer:
    """Similar public projects via GitHub repository search (fail-soft)."""

    name = 'project'

    def __init__(self, fetch=None):
        self._fetch = fetch or _github_fetch

    def index(self, query: str, *, user_id, limit: int = 8) -> list[dict]:
        bounded = max(1, min(int(limit or 8), MAX_INDEX_RESULTS))
        try:
            payload = self._fetch(_GITHUB_SEARCH_URL, {
                'q': (query or '')[:256], 'sort': 'stars',
                'order': 'desc', 'per_page': bounded})
        except Exception as exc:
            # Fail-soft indexer: a fetch failure yields no refs, never an error.
            logger.debug('[Evolution:Indexers] project search failed: %s', exc)
            return []
        items = [item for item in (payload.get('items') or [])[:bounded]
                 if isinstance(item, dict) and item.get('full_name')]
        scores = [float(item.get('score') or 0.0) for item in items]
        top = max(scores) if scores else 0.0
        refs = []
        for item, score in zip(items, scores):
            refs.append({
                'ref_id': f"gh:{item['full_name']}"[:320],
                'indexer': 'project',
                'locator': str(item.get('html_url') or '')[:2000],
                'relevance': round(score / top, 4) if top > 0 else 0.0,
                'provenance': (f"github search, {int(item.get('stargazers_count') or 0)}"
                               ' stars')[:500],
            })
        return refs


class AcademicIndexer:
    """Related papers through the existing bounded academic discovery."""

    name = 'academic'

    def __init__(self, search=None):
        self._search = search

    def _default_search(self, query: str, max_results: int) -> list[dict]:
        from lib.paper.arxiv import search_arxiv
        from lib.research.literature import default_discovery_adapter
        return default_discovery_adapter(search_arxiv).search(
            query, max_results=max_results)

    def index(self, query: str, *, user_id, limit: int = 8) -> list[dict]:
        bounded = max(1, min(int(limit or 8), MAX_INDEX_RESULTS))
        search = self._search or self._default_search
        try:
            rows = search(query, max_results=bounded)
        except Exception as exc:
            # Fail-soft indexer: a search failure yields no refs, never an error.
            logger.debug('[Evolution:Indexers] academic search failed: %s', exc)
            return []
        refs = []
        for position, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            identity = (row.get('arxiv_id') or row.get('doi')
                        or row.get('semantic_scholar_id') or '')
            if not identity:
                continue
            locator = (f'https://arxiv.org/abs/{identity}'
                       if row.get('arxiv_id')
                       else f'https://doi.org/{identity}')
            refs.append({
                'ref_id': f"paper:{identity}"[:320],
                'indexer': 'academic',
                'locator': locator[:2000],
                'relevance': round(1.0 - position / max(1, bounded), 4),
                'provenance': (f"{row.get('source', 'academic')} metadata: "
                               f"{str(row.get('title') or '')[:200]}")[:500],
            })
            if len(refs) >= bounded:
                break
        return refs
