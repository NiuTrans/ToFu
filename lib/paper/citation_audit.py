"""Citation-hallucination audit for generated paper reports.

The report engine produces free-form Markdown; there is no structured
bibliography object. But the report PROMPTS require the model to cite concrete
``arXiv:<id>`` / DOI identifiers inline (the Paper Card row and the Research
Landscape section). Those inline identifiers are the cleanest, least-fragile
extraction seam — and the lowest-false-positive one, because a model-emitted
identifier that fails to resolve is a *deterministic* hallucination signal
(Tier-1), whereas scraping prose titles would be fragile and false-positive
prone.

This module:
  1. harvests every distinct DOI / arXiv id the report cites
     (``tofu_search.verify.extract_citations_from_text`` — reuses the SAME
     canonical identifier regexes the vertical-search package uses), then
  2. verifies them via ``tofu_search.verify.verify_citations`` (zero LLM —
     free CrossRef / arXiv lookups), and
  3. returns a card payload ONLY when at least one citation is ``suspicious``.

The whole operation is best-effort: any failure is logged and yields ``None``
(no card), never an exception — report generation must never break because the
audit hiccupped. ``unverifiable`` entries are NEVER surfaced as hallucinations.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['build_citation_audit']


def build_citation_audit(report_text: str) -> dict | None:
    """Audit the identifiers a report cites; return a card payload or ``None``.

    Returns ``None`` when there is nothing to flag — no identifiers, an
    all-clear run, or only ``unverifiable`` results (coverage gaps, never
    fabrication). When at least one citation is ``suspicious`` returns::

        {
          'total':   <int>,    # identifiers actually checked
          'counts':  {'verified': n, 'suspicious': n, 'unverifiable': n},
          'suspicious': [ {identifier, kind, reason, checked, ...evidence}, ... ],
        }

    The caller folds this into the persisted report meta so the frontend can
    render a "citation integrity" card — gated on ``payload is not None``.
    """
    if not report_text:
        return None
    try:
        from lib.search_runtime import ensure_search_runtime
        ensure_search_runtime()
        from tofu_search.verify import (
            extract_citations_from_text,
            summarize,
            verify_citations,
        )
    except Exception as e:
        logger.warning('[Paper:CiteAudit] tofu_search.verify unavailable: %s', e)
        return None

    try:
        cits = extract_citations_from_text(report_text)
        if not cits:
            logger.debug('[Paper:CiteAudit] no inline identifiers to verify')
            return None
        results = verify_citations(cits)
        summary = summarize(results)
    except Exception as e:
        logger.warning('[Paper:CiteAudit] verification failed: %s', e, exc_info=True)
        return None

    if not summary.get('has_suspicious'):
        logger.info('[Paper:CiteAudit] %d identifiers checked, none suspicious '
                    '(counts=%s) — no card', summary.get('total', 0),
                    summary.get('counts'))
        return None

    suspicious = []
    for r in summary['suspicious']:
        cit = r.get('citation', {})
        ev = r.get('evidence', {})
        identifier = cit.get('doi') or cit.get('arxiv_id') or cit.get('title') or '(unknown)'
        kind = 'DOI' if cit.get('doi') else ('arXiv' if cit.get('arxiv_id') else 'title')
        suspicious.append({
            'identifier': identifier,
            'kind': kind,
            'reason': ev.get('reason', ''),
            'checked': ev.get('checked', ''),
            'claimedTitle': ev.get('claimed_title', ''),
            'matchedTitle': ev.get('matched_title', ''),
        })

    logger.info('[Paper:CiteAudit] %d suspicious of %d checked (counts=%s)',
                len(suspicious), summary['total'], summary['counts'])
    return {
        'total': summary['total'],
        'counts': summary['counts'],
        'suspicious': suspicious,
    }
