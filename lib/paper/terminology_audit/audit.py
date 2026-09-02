"""Audit a finished report for missing or dangling glossary definitions.

The audit runs after the full report exists, eliminating the forward-generation
blind spot of an early glossary.  It is deterministic, conservative, and
non-destructive: findings become metadata, while failures return ``None`` and
never invalidate the report.  Sibling modules own acronym extraction, section
classification, and glossary parsing.
"""

from __future__ import annotations

import re

from lib.log import get_logger

from ._acronyms import (
    _WELL_KNOWN_ACRONYMS,
    _extract_acronyms,
    _is_common_word,
    _is_named_entity,
    _strip_code,
)
from ._glossary import _parse_glossary
from ._sections import (
    _CARD_HDR_RE,
    _GLOSSARY_HDR_RE,
    _is_inline_defined,
    _only_in_related_work,
    _split_sections,
)

logger = get_logger(__name__)

__all__ = ['build_terminology_audit']


def _find_missing_terms(sections, glossary_terms_ci, full_body=''):
    """Acronyms USED in the body but with NO glossary row AND no available meaning.

    Scans every section except the glossary itself and the Paper Card (metadata).
    A candidate is only a genuine gap — i.e. the reader has NO way to learn its
    meaning — when it survives all three precision suppressors:

      1. not a well-known, audience-level field acronym (``_WELL_KNOWN_ACRONYMS``);
      2. not expanded inline anywhere in the body (``_is_inline_defined``);
      3. not a related-work-only citation (``_only_in_related_work``).

    Returns a list of ``{term, section, evidence}`` — one entry per distinct
    surviving gap, recording the first section + line it appears in.
    """
    missing = []
    seen: set[str] = set()
    for header, body in sections:
        if _GLOSSARY_HDR_RE.search(header) or _CARD_HDR_RE.search(header):
            continue
        clean = _strip_code(body)
        for tok in _extract_acronyms(clean):
            key = tok.upper()
            if key in glossary_terms_ci or key in seen:
                continue
            seen.add(key)
            # Precision suppressors — the reader ALREADY has this term's meaning
            # (or it is a proper-noun label / ordinary word, not a concept the
            # glossary owes).
            if key in _WELL_KNOWN_ACRONYMS or _is_common_word(tok):
                continue
            if _is_named_entity(tok):
                continue
            if _is_inline_defined(tok, full_body):
                continue
            if _only_in_related_work(tok, sections):
                continue
            evidence = ''
            for line in clean.splitlines():
                if re.search(r'\b' + re.escape(tok) + r'\b', line):
                    evidence = line.strip()[:200]
                    break
            missing.append({
                'term': tok,
                'section': header or '(title)',
                'evidence': evidence,
            })
    return missing


def _find_dangling_refs(glossary, glossary_terms_ci, full_body=''):
    """Acronyms referenced INSIDE a glossary definition that have no meaning.

    For each glossary row, extract candidate acronyms from its definition cell;
    any that is neither the row's own term nor another glossary term is a
    candidate dangling reference. It is only a genuine dangle when the reader has
    no way to learn its meaning — so the same well-known + inline-definition
    suppressors apply (a definition that leans on ``BLEU`` or on a term the body
    expands inline does not strand the reader). Returns a list of
    ``{term, referencedTerm, definition}``.
    """
    dangling = []
    seen: set[tuple[str, str]] = set()
    for term, definition in glossary.items():
        own = term.upper()
        clean = _strip_code(definition)
        for tok in _extract_acronyms(clean):
            key = tok.upper()
            if key == own or key in glossary_terms_ci:
                continue
            if (key in _WELL_KNOWN_ACRONYMS or _is_common_word(tok)
                    or _is_named_entity(tok) or _is_inline_defined(tok, full_body)):
                continue
            pair = (own, key)
            if pair in seen:
                continue
            seen.add(pair)
            dangling.append({
                'term': term,
                'referencedTerm': tok,
                'definition': definition[:200],
            })
    return dangling


def build_terminology_audit(report_text: str) -> dict | None:
    """Audit a report's glossary for self-containment; return a card or ``None``.

    Returns ``None`` when there is nothing to flag — empty text, no glossary
    section, or a fully self-contained body. When at least one gap is found
    returns::

        {
          'glossaryCount': <int>,     # rows in the Core Terminology table
          'counts': {'missing': n, 'dangling': n},
          'missing':  [ {term, section, evidence}, ... ],
          'dangling': [ {term, referencedTerm, definition}, ... ],
        }

    The caller folds this into the persisted report meta so the frontend can
    render an "undefined terms" warning card — gated on ``payload is not None``.
    Best-effort: any internal failure logs a warning and yields ``None``.
    """
    if not report_text or not report_text.strip():
        return None
    try:
        sections = _split_sections(report_text)
        # Merge rows from EVERY glossary-matching section, not just the first.
        # A normal report has one "🔑 Core Terminology" table, so this is a
        # strict superset that leaves single-glossary bodies byte-identical.
        # It matters for the backfill pass: the appended "glossary backfill"
        # addendum is itself a glossary-matching section, so its rows count as
        # DEFINED here — which is exactly how a re-audit of body+addendum can
        # observe that a gap has been closed.
        glossary_bodies = [body for header, body in sections
                           if _GLOSSARY_HDR_RE.search(header)]
        if not glossary_bodies:
            logger.debug('[Paper:TermAudit] no Core Terminology section — skipping')
            return None

        glossary = {}
        for gb in glossary_bodies:
            glossary.update(_parse_glossary(gb))
        if not glossary:
            logger.debug('[Paper:TermAudit] glossary section has no parseable rows')
            return None
        glossary_terms_ci = {t.upper() for t in glossary}

        missing = _find_missing_terms(sections, glossary_terms_ci, report_text)
        dangling = _find_dangling_refs(glossary, glossary_terms_ci, report_text)
    except Exception as e:
        logger.warning('[Paper:TermAudit] audit failed (non-fatal): %s', e, exc_info=True)
        return None

    if not missing and not dangling:
        logger.info('[Paper:TermAudit] %d glossary rows, no gaps — no card', len(glossary))
        return None

    logger.info('[Paper:TermAudit] %d missing + %d dangling of %d glossary rows',
                len(missing), len(dangling), len(glossary))
    return {
        'glossaryCount': len(glossary),
        'counts': {'missing': len(missing), 'dangling': len(dangling)},
        'missing': missing,
        'dangling': dangling,
    }
