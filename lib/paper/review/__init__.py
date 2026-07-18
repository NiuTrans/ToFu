"""Review Mode — peer-review report engine config (facade package).

Review Mode reuses the EXISTING paper-report engine/runtime/tools verbatim;
the ONLY review-specific pieces are (a) the system prompt (a venue-aware peer
review instead of an explainer report) and (b) the venue scorecard. To avoid
touching the DB schema, a review is persisted in the same ``paper_reports``
table under a COMPOSITE ``lang`` key ``review:<venue>:<uilang>`` (e.g.
``review:neurips:en``). ``parse_report_lang`` decodes that key back into
``(kind, venue, ui_lang)`` so the start route can pick the right prompt and the
real UI language without polluting the ordinary report cache keyed by plain
``'en'`` / ``'zh'``.

Single source of truth for the venue list is ``REVIEW_VENUES`` (see
``_lang``). Each venue carries its REAL review-form dimensions and rating scale
(NeurIPS's 1–10 + Soundness/Presentation/Contribution 1–4, ARR's
Soundness/Excitement 1–5, CVPR's strong-reject→strong-accept band, …) — Review
Mode deliberately does NOT flatten every venue onto one template, because the
authenticity of the scorecard is the whole point.

This module is a pure re-export FACADE: it split out of a single ``review.py``
into cohesive sub-modules while preserving ``from lib.paper.review import X``
and ``from .review import X`` byte-for-byte:

  * ``_lang``     — venue registry + composite-key language helpers.
  * ``_textproc`` — deterministic text-cleaning pipeline (smart quotes,
                    slop-dash removal, table/emphasis stripping, scorecard
                    relocation).
  * ``_prompts``  — venue-aware prompt builders + their large string constants.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Venue registry + composite-key language helpers ─────────────────────
from lib.paper.review._lang import (  # noqa: E402,F401
    DEFAULT_VENUE,
    REBUTTAL_LANG_PREFIX,
    REVIEW_LANG_PREFIX,
    REVIEW_VENUES,
    is_rebuttal_lang,
    is_review_family,
    is_review_lang,
    list_venues,
    make_rebuttal_lang,
    make_review_lang,
    parse_report_lang,
)

# ── Deterministic text-cleaning pipeline ────────────────────────────────
from lib.paper.review._textproc import (  # noqa: E402,F401
    _collapse_dangling_emphasis,
    _deslop_segment,
    _educate_segment,
    _split_scorecard,
    _strip_md_tables,
    finalize_rebuttal_body,
    finalize_review_body,
    parse_rebuttal_decision,
    rebuttal_decision_marker,
    scorecard_separator,
    smarten_quotes,
    strip_slop_dashes,
)

# ── Venue-aware prompt builders + their large string constants ──────────
from lib.paper.review._prompts import (  # noqa: E402,F401
    REBUTTAL_DECISION_MARKER,
    build_rebuttal_prompt,
    build_rebuttal_tool_instruction,
    build_review_prompt,
    build_review_tool_instruction,
)

__all__ = [
    # lang / venue registry
    'REVIEW_LANG_PREFIX',
    'DEFAULT_VENUE',
    'REVIEW_VENUES',
    'is_review_lang',
    'is_rebuttal_lang',
    'is_review_family',
    'REBUTTAL_LANG_PREFIX',
    'parse_report_lang',
    'make_review_lang',
    'make_rebuttal_lang',
    'list_venues',
    # text-cleaning pipeline
    'smarten_quotes',
    'strip_slop_dashes',
    'scorecard_separator',
    'finalize_review_body',
    'finalize_rebuttal_body',
    'parse_rebuttal_decision',
    'rebuttal_decision_marker',
    # prompt builders
    'build_review_prompt',
    'build_review_tool_instruction',
    'build_rebuttal_prompt',
    'build_rebuttal_tool_instruction',
    'REBUTTAL_DECISION_MARKER',
]
