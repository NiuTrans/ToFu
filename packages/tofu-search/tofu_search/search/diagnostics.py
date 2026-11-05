"""Final search evidence diagnostics shared by delivery adapters.

Entry: attach_search_diagnostics. Consumes received engine and candidate facts
without starting work, changing evidence, or treating missing matches as proof
of absence. Public SearchResultList identity remains with the orchestrator.
"""
from tofu_search.log import get_logger

logger = get_logger(__name__)


def attach_search_diagnostics(final_results, *, query, engines_run, engine_counts,
                               engine_errors, engine_empty, engine_unavailable,
                               engine_warnings, deadline_hit, deadline_secs,
                               constraint_scopes, constraint_rejected, unverified,
                               unverified_preview, irrelevant_urls, fallback_attempts):
    if not final_results:
        total_engines = len(engines_run)
        errored = len(engine_errors)
        empty = len(engine_empty)
        if errored == total_engines:
            reason = 'network_error'
            reason_detail = 'All %d search engines failed due to network errors.' % total_engines
        elif errored > 0 and errored >= empty:
            reason = 'partial_network_error'
            failed_names = ', '.join(sorted(engine_errors.keys()))
            reason_detail = (
                '%d/%d engines had network errors (%s); the rest returned no matches.'
                % (errored, total_engines, failed_names)
            )
        else:
            reason = 'no_matches'
            reason_detail = (
                'The available engines supplied no matching results; coverage is not exhaustive.'
            )
        if engine_unavailable and not engine_counts:
            reason = 'partial_network_error'
            reason_detail = 'Some engines were unavailable; available engines supplied no usable evidence.'
        if constraint_rejected or unverified or irrelevant_urls:
            reason = 'no_usable_evidence'
            reason_detail = 'Returned candidates did not establish query-relevant evidence; this is not proof of absence.'
        if deadline_hit:
            reason = 'deadline'
            reason_detail = (
                'The search wall-clock budget (%ds) expired before any page '
                'could be fetched and cleaned (slow/unreachable hosts).' % deadline_secs
            )
        diag = {
            'reason': reason,
            'reason_detail': reason_detail,
            'engine_errors': engine_errors,
            'engine_empty': engine_empty,
            'engine_ok': list(engine_counts.keys()),
            'deadline_hit': deadline_hit,
        }
        final_results._search_diag = diag
        logger.warning('[Search] 0 final results — diag: reason=%s errors=%s empty=%s query=%r',
                       reason, list(engine_errors.keys()), engine_empty, query[:80])

    diag = dict(getattr(final_results, '_search_diag', None) or {})
    diag.update({
        'engine_ok': list(engine_counts), 'engine_empty': engine_empty,
        'engine_errors': engine_errors, 'engine_unavailable': engine_unavailable,
        'engine_warnings': engine_warnings,
        'deadline_hit': deadline_hit,
        'site_constraints': [scope for _, scope in constraint_scopes],
        'constraint_rejected': constraint_rejected,

        'fallback_attempts': fallback_attempts,
        'weak_match_count': sum(r.get('relevance') == 'weak_match' for r in unverified),
        'unverified_count': len(unverified),
        'unverified_candidates': [
            {'url': r['url'], 'title': str(r.get('title') or '')[:160],
             **{key: r[key] for key in ('similar_snippets', 'similar_snippets_omitted') if key in r}}
            for r in unverified_preview],
        'usable_count': len(final_results),
        'source_coverage': 'unknown',
    })
    final_results._search_diag = diag
