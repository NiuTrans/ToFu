"""Insight second-pass engine for Paper Reading Mode (facade package).

Runs AFTER the fidelity report is written. Where the report engine optimises for
completeness at ``temperature=0``, this pass optimises for the *uncovered* axis —
synthesis, taste, and TRANSFER — at a higher temperature, because insight is a
divergent act that temp=0 crushes.

Design contract (enforced, not aspirational — mirrors the recommend engine):

* The synthesis step is **agentic**: the model is given ``web_search`` /
  ``fetch_url`` (via the report engine's ``_execute_report_tool``) plus a
  current-date anchor and told to research what the subfield is stuck on TODAY
  before writing — a frozen-memory "future directions" list is exactly the
  regression the recommend engine already fixed.
* **The moat is transfer.** A "reader context" block (the reader's paper library
  + relevant stored memories) is injected so the pass can build concrete
  "this connects to «a paper you already read»" / "this transfers to «problem X»"
  bridges — the one thing a generic summariser cannot do.
* **Every paper it name-drops is GROUNDED** through the SAME
  ``search_arxiv`` / ``fetch_arxiv_title`` path the recommend engine uses. An
  ungrounded ref is stripped to ``null`` (the prose survives, the fake link does
  not) — an "insight" citing a hallucinated follow-up is worse than none.

Also here: :func:`score_report_rubric` — the measurement instrument shipped in
the same increment. It scores any report on four INSIGHT axes and returns strict
JSON so one-pass vs two-pass reports are a numeric diff, not a vibe.

This module is a pure re-export FACADE: it split out of a single
``insight_engine.py`` into cohesive sub-modules while preserving
``from lib.paper.insight_engine import X`` and ``from .insight_engine import X``
byte-for-byte. The sub-modules resolve the tests' monkeypatch seams
(``dispatch_stream`` / ``_execute_report_tool`` / ``search_arxiv`` /
``fetch_arxiv_title`` / ``_build_reader_context`` / ``score_report_rubric`` /
``_persist_insight`` / ``_self_identity``) THROUGH this package object at call
time, so a patch on ``ie.<name>`` bites exactly as in the flat module:

  * ``_config``      — tunable constants + gate/enabled/lang-key predicates.
  * ``_context``     — the reader-context (transfer moat) builder.
  * ``_grounding``   — arXiv grounding + self-reference guard.
  * ``_synthesize``  — JSON extraction, repair re-ask, agentic synthesis.
  * ``_render``      — grounded insight dict → Markdown section.
  * ``_rubric``      — the 4-axis measurement instrument.
  * ``_run``         — generate_insight / persist / run_report_insight.

Every failure path leaves a trace per CLAUDE.md §2.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Patchable dependency seams (re-exported so ``ie.<name>`` exists AND so a
#    test monkeypatch on the facade is what the sub-modules resolve at call
#    time). These mirror the original module-top imports. ──────────────────
from lib.llm_dispatch.api import dispatch_stream  # noqa: E402,F401
from lib.paper.arxiv import (  # noqa: E402,F401
    _extract_arxiv_id,
    fetch_arxiv_title,
    search_arxiv,
)
from lib.paper.insight_prompts import (  # noqa: E402,F401
    RUBRIC_AXES,
    insight_system_prompt,
    rubric_prompt,
)
from lib.paper.prompts import _REPORT_TOOLS, date_anchor_clause  # noqa: E402,F401
from lib.paper.recommend_engine import _norm_id, _title_grounded  # noqa: E402,F401
from lib.paper.tools import (  # noqa: E402,F401
    _execute_report_tool,
    display_query_for,
    parse_and_repair_tool_args,
)

# ── Config: constants + gate / enabled / lang-key predicates ──────────────
from lib.paper.insight_engine._config import (  # noqa: E402,F401
    INSIGHT_GATE_THRESHOLD,
    _CTX_LIBRARY_MAX,
    _CTX_MEMORY_MAX,
    _INSIGHT_LANG_PREFIX,
    _INSIGHT_TEMPERATURE,
    _REPAIR_MAX_TOKENS,
    _RUBRIC_MAX_TOKENS,
    _RUBRIC_TEMPERATURE,
    insight_enabled,
    insight_gate_fires,
    insight_lang_key,
)

# ── Reader context (the transfer moat) ────────────────────────────────────
from lib.paper.insight_engine._context import (  # noqa: E402,F401
    _build_reader_context,
    _context_query,
    _library_context,
    _memory_context,
)

# ── Grounding + self-reference guard ──────────────────────────────────────
from lib.paper.insight_engine._grounding import (  # noqa: E402,F401
    _ground_insight,
    _ground_ref,
    _is_self_reference,
    _self_identity,
)

# ── JSON extraction, repair re-ask, agentic synthesis ─────────────────────
from lib.paper.insight_engine._synthesize import (  # noqa: E402,F401
    _REPAIR_INSTRUCTION,
    _parse_llm_json,
    _repair_json_reask,
    _research_and_synthesize,
)

# ── Deterministic anchor resolution (model nominates, code decides) ─────
from lib.paper.insight_engine._anchors import (  # noqa: E402,F401
    _FUZZY_THRESHOLD,
    extract_report_headings,
    resolve_anchor,
    resolve_insight_anchors,
)

# ── Rendering ─────────────────────────────────────────────────────────────
from lib.paper.insight_engine._render import (  # noqa: E402,F401
    _HEADINGS,
    _ref_md,
    render_insight_markdown,
)

# ── Rubric critic — the measurement instrument ────────────────────────────
from lib.paper.insight_engine._rubric import (  # noqa: E402,F401
    _coerce_score,
    score_report_rubric,
)

# ── Orchestration entrypoints ─────────────────────────────────────────────
from lib.paper.insight_engine._run import (  # noqa: E402,F401
    _persist_insight,
    generate_insight,
    run_report_insight,
)

__all__ = [
    # ── public API ──
    'run_report_insight',
    'generate_insight',
    'score_report_rubric',
    'render_insight_markdown',
    'insight_lang_key',
    'insight_gate_fires',
    'insight_enabled',
    'INSIGHT_GATE_THRESHOLD',
    # ── constants / prompt helpers surfaced for callers & tests ──
    'RUBRIC_AXES',
    'insight_system_prompt',
    'rubric_prompt',
    # ── patchable dependency seams (tests monkeypatch these on ``ie``) ──
    'dispatch_stream',
    '_execute_report_tool',
    'search_arxiv',
    'fetch_arxiv_title',
    '_build_reader_context',
    '_persist_insight',
    '_self_identity',
    '_research_and_synthesize',
    '_repair_json_reask',
    '_parse_llm_json',
    '_REPAIR_INSTRUCTION',
    '_ground_insight',
    '_ground_ref',
    '_is_self_reference',
    '_coerce_score',
    '_ref_md',
    'extract_report_headings',
    'resolve_anchor',
    'resolve_insight_anchors',
]
