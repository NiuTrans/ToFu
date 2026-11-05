"""Optional scoring and lexical fallback for admitted search evidence.

Entry: rank_search_candidates. Borrowed scorers receive the existing remaining
budget, cancellation token and evidence groups. Ranking changes ordering only;
it never upgrades uncertain candidates or takes ownership of the scorer.
"""
from tofu_search.log import get_logger
from tofu_search.search.rerank import build_scoring_texts, rerank_by_bm25, rerank_by_scores

logger = get_logger(__name__)


def rank_search_candidates(query, relevant, unverified, max_results, *, config,
                           rerank, deadline_hit, budget_left):
    local_rerank_diag = {'status': 'disabled'}
    if not rerank:
        logger.debug('[Search] step6 skipped — caller passed rerank=False')
    else:
        local_scores = None
        scorer = getattr(config, 'local_reranker', None)
        if scorer is not None and (relevant or unverified):
            from tofu_search.lexical import query_subject
            from tofu_search.local_rerank import LocalRerankError
            budget = budget_left()
            if budget is not None and budget <= 0:
                deadline_hit = True
                local_rerank_diag['status'] = 'deadline_skipped'
            else:
                allowance = config.local_rerank_timeout
                if budget is not None:
                    allowance = min(allowance, budget)
                candidates = relevant + unverified
                try:
                    local_scores = scorer.score(query_subject(query), build_scoring_texts(query, candidates),
                        timeout=allowance, cancel_event=config.local_rerank_cancel_event,
                        group_sizes=[len(relevant), len(unverified)], diag=local_rerank_diag)
                    local_rerank_diag['status'] = 'applied'
                except (LocalRerankError, TimeoutError) as error:
                    local_rerank_diag['status'] = getattr(error, 'kind', 'timeout')
                    logger.warning('[Rerank] Local scoring unavailable (%s); using lexical ranking',
                                   local_rerank_diag['status'])
                    if budget_left() is not None and budget_left() <= 0:
                        deadline_hit = True
        relevant_count = len(relevant)
        relevant = (rerank_by_bm25(query, relevant, max_results) if local_scores is None else
                    rerank_by_scores(query, relevant, max_results, local_scores[:relevant_count]))
        # Discovery still needs useful ordering when lexical evidence is
        # uncertain. Keep every received candidate and its evidence status;
        # the existing preview selects from this ranked sidecar afterward.
        if unverified:
            unverified = (rerank_by_bm25(query, unverified, len(unverified)) if local_scores is None else
                          rerank_by_scores(query, unverified, len(unverified), local_scores[relevant_count:]))
    return relevant, unverified, local_rerank_diag, deadline_hit
