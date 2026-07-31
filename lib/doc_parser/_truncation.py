"""lib/doc_parser/_truncation.py — the single way to announce a truncation.

WHY THIS MODULE EXISTS
----------------------
Every extractor in this package can drop content, and each one used to phrase
that fact in its own words. Measured, those phrasings were actively
misleading:

  * ``Sheet "S" truncated at 1000 rows`` — kept 1,000 of 5,000 (80% loss),
    but a numerator with no denominator reads the same whether 20% or 99%
    survived.
  * ``Truncated at slide 48`` — of *200*. This one reads like "slide 48 had a
    problem" rather than "you received a quarter of the deck".
  * The ``.xlsx`` blank-run break emitted nothing at all.

A caller that cannot tell how much is missing cannot decide whether to act on
what it got. So the rule for this package is:

    **A truncation warning MUST state kept-vs-total and say the remainder was
    not read.**

Enforcement is structural, not editorial: every site calls
:func:`truncation_warning`, and ``tests/test_doc_parser_truncation_honesty.py``
walks the package AST asserting no extractor hand-rolls a truncation string.
That is the same fail-closed shape the MCP credential redactor uses — a new
format added next year cannot silently reintroduce a bare numerator, because
there is no other way to say it.

Note the deliberate asymmetry with ``degraded``-style flags: this is only
about *reporting*, never about suppressing the cut itself. The caps exist for
memory safety and stay exactly where they are.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# Units a truncation can be measured in. Anything not on this list is a
# reporting bug, not a new unit — add it here deliberately.
UNITS = ('rows', 'columns', 'chars', 'slides', 'sheets')


def truncation_warning(kept, total, unit, scope='', detail=''):
    """Build the one canonical truncation sentence.

    Args:
        kept: How much survived (int).
        total: How much existed (int), or 0/None when genuinely unknowable —
            in that case the sentence says so explicitly rather than quietly
            omitting the denominator, so "unknown" stays visible.
        unit: One of :data:`UNITS`.
        scope: Optional qualifier, e.g. a sheet name.
        detail: Optional extra clause appended before the final period.

    Returns:
        A string of the shape::

            Sheet "Q1": kept 1,000 of 5,000 rows; the rest was NOT read

    Never returns an empty string — a caller that decided to warn must
    produce a warning.
    """
    if unit not in UNITS:
        # Loud, not silent: an unknown unit means someone added a cut without
        # thinking about how to measure it.
        logger.warning('[DocParser] truncation_warning got unknown unit %r; '
                       'add it to UNITS deliberately', unit)

    prefix = f'{scope}: ' if scope else ''
    try:
        kept_s = f'{int(kept):,}'
    except (TypeError, ValueError) as e:
        logger.debug('[DocParser] non-numeric kept=%r: %s', kept, e)
        kept_s = str(kept)

    if total:
        try:
            total_s = f'{int(total):,}'
        except (TypeError, ValueError) as e:
            logger.debug('[DocParser] non-numeric total=%r: %s', total, e)
            total_s = str(total)
        core = f'kept {kept_s} of {total_s} {unit}'
    else:
        # Unknown denominators are stated, never hidden. "of an unknown total"
        # still tells the caller it is holding a fragment.
        core = f'kept {kept_s} {unit} of an unknown total'

    tail = f'; {detail}' if detail else ''
    return f'{prefix}{core}{tail}; the rest was NOT read'
