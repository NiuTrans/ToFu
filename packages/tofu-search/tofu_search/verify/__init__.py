"""tofu_search.verify — low-cost reference / citation verification.

Detects likely-hallucinated citations in a paper or ``.bib`` file by checking
each reference against authoritative free catalogues (CrossRef, arXiv) with
ZERO LLM calls. Emits a three-state verdict per citation —
``verified`` / ``suspicious`` / ``unverifiable`` — with a strict
anti-false-positive discipline (only a concrete identifier that definitively
fails to resolve is ever ``suspicious``; a missing title hit is a coverage
gap, never fabrication).

Public API::

    from tofu_search.verify import verify_bibtex, verify_references, summarize

    results = verify_bibtex(open('refs.bib').read())
    summary = summarize(results)
    if summary['has_suspicious']:
        ...  # surface a report card
"""

from tofu_search.verify.citations import (
    SUSPICIOUS,
    UNVERIFIABLE,
    VERIFIED,
    summarize,
    verify_bibtex,
    verify_citation,
    verify_citations,
    verify_references,
)
from tofu_search.verify.parse import (
    Citation,
    extract_citations_from_text,
    extract_identifiers,
    parse_bibtex,
    parse_references,
)

__all__ = [
    'VERIFIED',
    'SUSPICIOUS',
    'UNVERIFIABLE',
    'verify_citation',
    'verify_citations',
    'verify_bibtex',
    'verify_references',
    'summarize',
    'Citation',
    'parse_bibtex',
    'parse_references',
    'extract_identifiers',
    'extract_citations_from_text',
]
