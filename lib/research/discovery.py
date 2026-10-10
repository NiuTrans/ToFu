"""Bounded literature discovery over injected search adapters.

Owns query diversification, metadata deduplication and discovery receipts; it
never owns paper persistence or asserts that search saturation proves novelty.
The recipe supplies authenticated transport seams and cancellation. All source
metadata remains untrusted evidence, not execution instructions.
"""
from __future__ import annotations

import re
from itertools import islice

from lib.log import get_logger
from lib.research.contracts import (
    MAX_RESEARCH_DISCOVERY_QUERIES,
    MAX_RESEARCH_DISCOVERY_RESULTS_PER_QUERY,
    RESEARCH_DISCOVERY_STALE_ROUNDS,
)

logger = get_logger(__name__)


def query_families(direction: str) -> list[dict]:
    """Keep the exact query and add explicitly scoped competing perspectives.

    Free text only: the arXiv adapter rejects authored field/boolean syntax.
    Short directions retain their topic; long research questions are reduced
    to content terms for the complementary passes, never for durable identity.
    """
    words = re.findall(r"[\w-]+", direction.lower())
    stop = {'the', 'a', 'an', 'of', 'in', 'on', 'for', 'to', 'and', 'or',
            'which', 'what', 'how', 'can', 'does', 'without', 'under', 'with',
            'is', 'are', 'that', 'this', 'than', 'do', 'we', 'it'}
    terms = list(dict.fromkeys(word for word in words if word not in stop))
    topic = ' '.join(terms[:16]) or direction
    variants = [
        ('direct', direction),
        ('mechanism', topic + ' mechanism analysis'),
        ('baseline', topic + ' benchmark comparison'),
        ('counterevidence', topic + ' failure limitations'),
        ('survey', topic + ' survey review'),
        ('robustness', topic + ' robustness generalization'),
    ]
    seen = set()
    result = []
    for family, query in variants:
        query = query[:2000]
        if query.casefold() not in seen:
            seen.add(query.casefold())
            result.append({'family': family, 'query': query})
    return result[:MAX_RESEARCH_DISCOVERY_QUERIES]


#: Baseline citation-expansion calls for the default 60-paper corpus; larger
#: targets scale the graph pass linearly (bounded below) so a 200-paper run
#: carries a real Connected-Papers-style edge set instead of eight lookups.
MAX_CITATION_CALLS = 8
CITATION_CALLS_PER_TARGET = 0.13
MAX_CITATION_DEPTH = 2
MAX_CORE_PAPERS = 12


def max_citation_calls(target: int) -> int:
    """Graph-call budget for a corpus target; floor stays the baseline budget."""
    return max(MAX_CITATION_CALLS, int(round(target * CITATION_CALLS_PER_TARGET)))


def discover_corpus(query: str, *, target: int, search, abort_check=None,
                    citation_search=None, on_progress=None) -> dict:
    """Bound metadata and real graph expansion independently; never infer novelty.

    Only an explicit citation seam or a bound academic adapter enables graph
    calls. Plain mocked searches cannot accidentally activate network access.
    Graph mode reserves half the target for a target-scaled call budget
    across two hops.
    """
    from lib.paper.arxiv import normalize_arxiv_id
    from lib.research.contracts import MAX_RESEARCH_SEED_PAPERS
    from lib.research.literature import normalize_doi, bibliometrics

    target = max(0, min(MAX_RESEARCH_SEED_PAPERS, int(target)))
    citation_budget = max_citation_calls(target)
    owner = getattr(search, '__self__', None)
    if citation_search is None:
        citation_search = getattr(owner, 'citation_search', None)
    rows, aliases, rounds, edges = {}, {}, [], []
    stale, graph_calls = 0, 0
    stop_reason = 'query_budget'
    aborted = lambda: bool(abort_check and abort_check())

    def report(round_receipt):
        if on_progress and not aborted():
            try:
                on_progress({'type': 'search_round', 'stage': 'harvest',
                             'round': dict(round_receipt),
                             'counts': {'discovered': len(rows)}})
            except Exception as e:
                # Observers cannot fail scientific work.
                logger.debug('[Discovery] progress observer dropped: %s', e)

    def accept(hit, provenance):
        if not isinstance(hit, dict):
            return None, False
        aid = normalize_arxiv_id(hit.get('arxiv_id')) or ''
        doi = normalize_doi(hit.get('doi'))
        sid = hit.get('semantic_scholar_id', '')
        sid = sid if isinstance(sid, str) and re.fullmatch(r'[a-zA-Z0-9]{1,64}', sid) else ''
        ids = ([f'doi:{doi}'] if doi else []) + ([f'arxiv:{aid}'] if aid else [])
        ids += [f's2:{sid}'] if sid else []
        if not ids:
            return None, False
        existing = list(dict.fromkeys(aliases[i] for i in ids if i in aliases))
        key = existing[0] if existing else ids[0]
        added = not existing
        if added:
            rows[key] = {'paper_id': key, 'arxiv_id': '', 'doi': '',
                         'semantic_scholar_id': '', 'title': '', 'abstract': '',
                         'query_families': [], 'retrieval_provenance': [],
                         'reading_status': 'metadata_only', 'depth': provenance.get('depth', 0)}
        row = rows[key]
        def merge_metadata(metadata):
            for field in ('year', 'venue', 'metadata_source'):
                if row.get(field) in (None, '') and metadata.get(field) not in (None, ''):
                    row[field] = metadata[field]
            if row.get('citation_count') is None and metadata.get('citation_count') is not None:
                row['citation_count'] = metadata['citation_count']
                row['citation_count_source'] = metadata.get('citation_count_source', '')
        # Bridge metadata may unify previously separate DOI/arXiv nodes.
        for duplicate in existing[1:]:
            other = rows.pop(duplicate)
            merge_metadata(other)
            for field in ('arxiv_id', 'doi', 'semantic_scholar_id', 'title', 'abstract'):
                row[field] = row[field] or other[field]
            row['retrieval_provenance'].extend(other['retrieval_provenance'])
            row['query_families'] = list(dict.fromkeys(row['query_families'] + other['query_families']))
            row['depth'] = min(row['depth'], other['depth'])
            for alias, old in list(aliases.items()):
                if old == duplicate:
                    aliases[alias] = key
            for edge in edges:
                for field in ('from', 'to'):
                    if edge[field] == duplicate:
                        edge[field] = key
        for identifier in ids:
            aliases[identifier] = key
        for field, value in (('arxiv_id', aid), ('doi', doi), ('semantic_scholar_id', sid),
                             ('title', hit.get('title')), ('abstract', hit.get('abstract') or hit.get('summary'))):
            if not row[field] and isinstance(value, str):
                row[field] = value.strip()[:2000 if field == 'abstract' else 1000]
        merge_metadata(bibliometrics(hit))
        for field, default in (('year', None), ('venue', ''), ('citation_count', None),
                               ('metadata_source', ''), ('citation_count_source', '')):
            row.setdefault(field, default)
        row['ingest_supported'] = bool(row['arxiv_id'])
        receipt = dict(provenance, source=str(hit.get('source') or 'injected_search')[:80])
        if receipt not in row['retrieval_provenance']:
            row['retrieval_provenance'].append(receipt)
        family = provenance.get('family')
        if family and family not in row['query_families']:
            row['query_families'].append(family)
        return key, added

    families = query_families(query)
    if target <= MAX_RESEARCH_DISCOVERY_RESULTS_PER_QUERY:
        families = families[:1]
    seed_target = max(1, target // 2) if citation_search and target else target
    for planned in families:
        if aborted():
            stop_reason = 'aborted'
            break
        limit = min(MAX_RESEARCH_DISCOVERY_RESULTS_PER_QUERY, seed_target - len(rows))
        if limit <= 0:
            stop_reason = 'corpus_budget' if len(rows) >= target else 'seed_budget'
            break
        count = added = 0
        error = ''
        try:
            for hit in islice(iter(search(planned['query'], max_results=limit) or ()), limit):
                if aborted():
                    stop_reason = 'aborted'
                    break
                count += 1
                _, new = accept(hit, dict(planned, operation='search', depth=0))
                added += new
        except InterruptedError:
            stop_reason = 'aborted'
        except Exception as exc:
            logger.debug('discovery round failed for %r: %s',
                         planned['query'], type(exc).__name__)
            error = type(exc).__name__
        rounds.append(dict(planned, retrieved=count, new_ids=added, error=error))
        report(rounds[-1])
        if stop_reason == 'aborted':
            break
        if len(rows) >= target:
            stop_reason = 'corpus_budget'
            break
        stale = stale + 1 if added == 0 and not error else 0
        if stale >= RESEARCH_DISCOVERY_STALE_ROUNDS:
            stop_reason = 'diminishing_discovery'
            break

    terms = set(re.findall(r'[\w-]+', query.casefold()))
    def relevance(row):
        words = set(re.findall(r'[\w-]+', (row['title'] + ' ' + row['abstract']).casefold()))
        return len(terms & words) / max(1, len(terms))

    visited = set()
    if citation_search and stop_reason != 'aborted' and len(rows) < target:
        stop_reason = 'graph_exhausted'
        while graph_calls < citation_budget and len(rows) < target:
            if aborted():
                stop_reason = 'aborted'
                break
            candidates = [r for r in rows.values() if r['depth'] < MAX_CITATION_DEPTH
                          and r['paper_id'] not in visited]
            if not candidates:
                break
            candidates.sort(key=lambda r: (-relevance(r), r['depth'], r['paper_id']))
            core = candidates[0]
            visited.add(core['paper_id'])
            for direction in ('references', 'citations'):
                if aborted():
                    stop_reason = 'aborted'
                    break
                if graph_calls >= citation_budget or len(rows) >= target:
                    break
                graph_calls += 1
                count = added = 0
                error = ''
                limit = min(MAX_RESEARCH_DISCOVERY_RESULTS_PER_QUERY, target - len(rows))
                try:
                    hits = citation_search(dict(core), direction=direction, max_results=limit)
                    for hit in islice(iter(hits or ()), limit):
                        if aborted():
                            stop_reason = 'aborted'
                            break
                        count += 1
                        key, new = accept(hit, {'operation': direction,
                            'parent_id': core['paper_id'], 'depth': core['depth'] + 1})
                        added += new
                        parent = aliases.get(core['paper_id'], core['paper_id'])
                        if key and key != parent:
                            edge = {'from': parent if direction == 'references' else key,
                                    'to': key if direction == 'references' else parent,
                                    'relation': 'cites'}
                            if edge not in edges:
                                edges.append(edge)
                except InterruptedError:
                    stop_reason = 'aborted'
                except Exception as exc:
                    logger.debug('citation %s round failed for %s: %s',
                                 direction, core['paper_id'],
                                 type(exc).__name__)
                    error = type(exc).__name__
                rounds.append({'family': direction, 'parent_id': core['paper_id'],
                               'retrieved': count, 'new_ids': added, 'error': error})
                report(rounds[-1])
                if stop_reason == 'aborted':
                    break
            if stop_reason == 'aborted':
                break
        if stop_reason != 'aborted':
            stop_reason = ('corpus_budget' if len(rows) >= target else
                           'graph_budget' if graph_calls >= citation_budget else 'graph_exhausted')
    papers = sorted(rows.values(), key=lambda row: -relevance(row))
    for index, paper in enumerate(papers):
        for receipt in paper['retrieval_provenance']:
            if 'parent_id' in receipt:
                receipt['parent_id'] = aliases.get(receipt['parent_id'], receipt['parent_id'])
        paper['relevance_score'] = relevance(paper)
        paper['selected_core'] = index < MAX_CORE_PAPERS
    try:
        statuses = list(islice(owner.status_snapshot(), 64)) if callable(getattr(owner, 'status_snapshot', None)) else []
    except Exception as exc:
        logger.debug('source status snapshot unavailable: %s',
                     type(exc).__name__)
        statuses = [{'source': 'discovery', 'operation': 'status_snapshot',
                     'status': 'unavailable', 'error': type(exc).__name__}]
    canonical_edges = []
    for edge in edges:
        if edge['from'] != edge['to'] and edge not in canonical_edges:
            canonical_edges.append(edge)
    return {'papers': papers, 'rounds': rounds, 'citation_edges': canonical_edges,
            'core_paper_ids': [p['paper_id'] for p in papers[:MAX_CORE_PAPERS]],
            'source_statuses': statuses, 'graph_calls': graph_calls,
            'graph_status': 'enabled' if citation_search else 'not_configured',
            'discovered_count': len(rows), 'target_count': target,
            'stop_reason': stop_reason, 'coverage_complete': False,
            'coverage_note': 'Bounded academic metadata and citation discovery; absence is not evidence of novelty.'}
