"""Public peer-review language, prompt, and deterministic text API.

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

The implementation is partitioned by responsibility and resolved lazily so
route registration does not initialize deterministic text processing or the
shared language cascade:

  * ``_lang``     — venue registry + composite-key language helpers.
  * ``_textproc`` — deterministic text-cleaning pipeline (smart quotes,
                    slop-dash removal, table/emphasis stripping, scorecard
                    relocation).
  * ``_prompts``  — venue-aware prompt builders + their large string constants.
"""

from importlib import import_module

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


_EXPORT_MODULES = {
    # Venue registry and composite language keys.
    'REVIEW_LANG_PREFIX': 'lib.paper.review._lang',
    'DEFAULT_VENUE': 'lib.paper.review._lang',
    'REVIEW_VENUES': 'lib.paper.review._lang',
    'is_review_lang': 'lib.paper.review._lang',
    'is_rebuttal_lang': 'lib.paper.review._lang',
    'is_review_family': 'lib.paper.review._lang',
    'REBUTTAL_LANG_PREFIX': 'lib.paper.review._lang',
    'parse_report_lang': 'lib.paper.review._lang',
    'make_review_lang': 'lib.paper.review._lang',
    'make_rebuttal_lang': 'lib.paper.review._lang',
    'list_venues': 'lib.paper.review._lang',
    # Deterministic text normalization.
    'smarten_quotes': 'lib.paper.review._textproc',
    'strip_slop_dashes': 'lib.paper.review._textproc',
    'scorecard_separator': 'lib.paper.review._textproc',
    'finalize_review_body': 'lib.paper.review._textproc',
    'finalize_rebuttal_body': 'lib.paper.review._textproc',
    'parse_rebuttal_decision': 'lib.paper.review._textproc',
    'rebuttal_decision_marker': 'lib.paper.review._textproc',
    # Prompt builders.
    'build_review_prompt': 'lib.paper.review._prompts',
    'build_review_tool_instruction': 'lib.paper.review._prompts',
    'build_rebuttal_prompt': 'lib.paper.review._prompts',
    'build_rebuttal_tool_instruction': 'lib.paper.review._prompts',
    'REBUTTAL_DECISION_MARKER': 'lib.paper.review._prompts',
}

_CHILD_MODULES = {'_lang', '_prompts', '_textproc'}


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None and name in _CHILD_MODULES:
        module_name = f'lib.paper.review.{name}'
    if module_name is None:
        raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
    module = import_module(module_name)
    value = module if name in _CHILD_MODULES else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__) | _CHILD_MODULES)
