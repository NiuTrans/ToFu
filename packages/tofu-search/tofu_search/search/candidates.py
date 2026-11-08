"""Invocation-local engine evidence accumulation under a canonical URL index.

Entry: CandidateAccumulator.add_batch. The orchestrator serializes calls under
its existing lock. No provider callback, fetch submission or worker lives here.
Provider rows are copied; a URL is published only after its evidence fields
validate, so one malformed engine cannot poison a later source's valid row.
"""
from tofu_search.generated_url_blocklist import url_is_blocked
from tofu_search.search.dedup import canonical_url_key
from tofu_search.search.quality import annotate_relevance, matches_site_constraints
from tofu_search.search.rerank import RRF_RANK_OFFSET


class CandidateAccumulator:
    def __init__(self, query, constraint_scopes, *, fetch_pages, max_chars):
        self.fetch_pages = fetch_pages
        self.max_chars = max_chars
        self.query = query
        self.constraint_scopes = constraint_scopes
        self.by_url = {}
        self.all_results = []
        self.unique_results = []
        self.constraint_rejected = 0
        self.blocklist_rejected = 0

    def add_batch(self, batch, engine_tag=''):
        """Merge received rows and return newly admitted representatives."""
        new_results = []
        admitted_batch = []
        for rank, r in enumerate(batch, 1):
            r = dict(r)
            r.pop('similar_snippets', None)
            r.pop('similar_snippets_omitted', None)
            # A provider-crawled body stands in for our own page fetch;
            # a snippets-only call (fetch_pages=False) never carries one.
            if r.get('full_content'):
                if self.fetch_pages:
                    r['full_content'] = str(r['full_content'])[:self.max_chars]
                else:
                    r.pop('full_content')
            if not matches_site_constraints(r.get('url') or '', self.constraint_scopes):
                self.constraint_rejected += 1
                continue
            # search.skip_domains is an admission rule, not only a fetch gate:
            # a blocked URL must never reach the model through a search
            # listing either, because the listing itself carries the page's
            # title, snippet and fetched excerpts (and its URL invites a
            # follow-up fetch). Config is read through the call binding.
            from tofu_search.config import get_config
            if url_is_blocked(r.get('url') or '', get_config().skip_domains):
                self.blocklist_rejected += 1
                continue
            admitted_batch.append(r)
            annotate_relevance(r, self.query)
            key = canonical_url_key(r['url'])
            if key not in self.by_url:
                source = engine_tag or r.get('source') or 'Unknown'
                r['sources'] = list(dict.fromkeys((r.get('sources') or []) + [source]))
                r['engine_count'] = max(len(r['sources']), int(r.get('engine_count') or 1))
                r['rrf_score'] = float(r.get('rrf_score') or 0.0) + 1.0 / (RRF_RANK_OFFSET + rank)
                r['source_rank'] = rank
                self.by_url[key] = r
                self.unique_results.append(r)
                new_results.append(r)
            else:
                published = self.by_url[key]
                # Validate a contribution in isolation. Fetch receipts already
                # reference the published row, so commit back to that identity
                # only after every metadata/relevance step succeeds.
                kept = dict(published)
                kept['sources'] = list(published.get('sources', [published.get('source', 'Unknown')]))
                source = engine_tag or r.get('source') or 'Unknown'
                sources = kept.setdefault('sources', [kept.get('source', 'Unknown')])
                if source not in sources:
                    sources.append(source)
                    kept['rrf_score'] = float(kept.get('rrf_score') or 0.0) + 1.0 / (RRF_RANK_OFFSET + rank)
                kept['engine_count'] = max(len(sources), int(kept.get('engine_count') or 1), int(r.get('engine_count') or 1))
                if len(r.get('snippet') or '') > len(kept.get('snippet') or ''):
                    kept['snippet'] = r.get('snippet') or ''
                if len(r.get('title') or '') > len(kept.get('title') or ''):
                    kept['title'] = r.get('title') or ''
                if r.get('full_content') and not kept.get('full_content'):
                    kept['full_content'] = r['full_content']
                annotate_relevance(kept, self.query)
                published.update(kept)
        self.all_results.extend(admitted_batch)
        return new_results

    def admit_deep_result(self, result):
        """Admit a fetched deep link against the original query and URL index.

        The caller appends to its post-dedup result list; deep links do not
        become additional raw engine receipts or gain invented engine weights.
        """
        result.pop('similar_snippets', None)
        result.pop('similar_snippets_omitted', None)
        if not matches_site_constraints(result.get('url') or '', self.constraint_scopes):
            self.constraint_rejected += 1
            return False
        annotate_relevance(result, self.query)
        key = canonical_url_key(result['url'])
        if key in self.by_url:
            return False
        self.by_url[key] = result
        return True
