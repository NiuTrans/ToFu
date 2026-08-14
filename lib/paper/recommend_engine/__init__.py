"""Describe-to-recommend engine for Paper Reading Mode (facade package).

The user often remembers *what a paper was about* but not its title (or even
mis-remembers a premise — "a diffusion LM won a NeurIPS award"). This engine
turns that fuzzy free-text description into a ranked list of **real, addable**
arXiv papers.

Design contract (enforced, not aspirational):

* The interpretation step is **agentic**: instead of guessing candidate titles
  from the model's frozen training memory (which cannot know about a conference
  happening *today* or papers posted last week), the model is given the
  project's own ``web_search`` / ``fetch_url`` tools and told to actually
  RESEARCH the current literature — search arXiv / the web, open the promising
  hits, verify any venue/award claim against a real source — BEFORE it proposes
  candidates. A "current date" anchor is injected so it never treats an
  in-progress year as the future. The final turn returns strict JSON.
* **No card is ever surfaced unless its arXiv ID resolves through the existing
  ``search_arxiv`` / ``fetch_arxiv_title`` path to a real paper.** A title the
  model produced but that cannot be grounded is dropped, logged at debug, and
  never rendered.
* The interpretive prose (``why`` / correction ``note``) is model text by
  design; the *papers* it points at are always grounded.

This module is a pure re-export FACADE: it split out of a single
``recommend_engine.py`` into cohesive sub-modules while preserving
``from lib.paper.recommend_engine import X`` and ``from .recommend_engine import
X`` byte-for-byte. The sub-modules resolve the tests' monkeypatch seams
(``dispatch_stream`` / ``_execute_report_tool`` / ``search_arxiv`` /
``fetch_arxiv_title`` / ``_ground_candidate`` / ``_RESEARCH_VERTICAL``) THROUGH
this package object at call time, so a patch on ``re_mod.<name>`` bites exactly
as in the flat module:

  * ``_ground``   — language guess + grounding helpers (the anti-hallucination
                    gate) + grounding / stopword constants.
  * ``_research`` — the system prompt, tunable research constants, JSON
                    extraction, and the agentic ``_research_and_interpret`` seam.
  * ``_events``   — ``iter_recommend_events`` / ``recommend_papers``, the public
                    streaming + blocking entrypoints.

CRITICAL — private cross-package consumers: ``lib/paper/insight_engine`` imports
``_norm_id`` / ``_title_grounded`` from this package facade, so they MUST stay
importable here.

Every failure path leaves a trace per CLAUDE.md §2.
"""

from lib.log import get_logger

logger = get_logger(__name__)

# ── Patchable dependency seams (re-exported so ``re_mod.<name>`` exists AND so
#    a test monkeypatch on the facade is what the sub-modules resolve at call
#    time). These mirror the original module-top imports. ──────────────────
from lib.llm_dispatch.api import dispatch_stream  # noqa: E402,F401
from lib.llm_errors import AbortedError  # noqa: E402,F401

from ..arxiv import (  # noqa: E402,F401
    _extract_arxiv_id,
    fetch_arxiv_title,
    search_arxiv,
)
from ..prompts import _REPORT_TOOLS, date_anchor_clause  # noqa: E402,F401
from ..tools import (  # noqa: E402,F401
    _execute_report_tool,
    display_query_for,
    parse_and_repair_tool_args,
)

# ── Grounding: language guess + anti-hallucination gate + constants ───────
from lib.paper.recommend_engine._ground import (  # noqa: E402,F401
    _GROUND_ATTEMPT_MULTIPLIER,
    _GROUND_SEARCH_DEPTH,
    _STOPWORDS,
    _card_from_result,
    _detect_lang,
    _ground_candidate,
    _ground_correction,
    _norm_id,
    _title_grounded,
    _title_tokens,
)

# ── Research: system prompt, research constants, JSON parse, agentic seam ──
from lib.paper.recommend_engine._research import (  # noqa: E402,F401
    _RECOMMEND_SYSTEM,
    _RESEARCH_VERTICAL,
    _parse_llm_json,
    _research_and_interpret,
)

# ── Public entrypoints: streaming generator + blocking wrapper ────────────
from lib.paper.recommend_engine._events import (  # noqa: E402,F401
    iter_recommend_events,
    recommend_papers,
)

__all__ = [
    # ── public API ──
    'iter_recommend_events',
    'recommend_papers',
    # ── constants surfaced for callers & tests ──
    '_RECOMMEND_SYSTEM',
    '_RESEARCH_VERTICAL',
    '_GROUND_ATTEMPT_MULTIPLIER',
    '_GROUND_SEARCH_DEPTH',
    '_STOPWORDS',
    # ── grounding helpers (private cross-package consumers depend on these) ──
    '_norm_id',
    '_title_grounded',
    '_title_tokens',
    '_detect_lang',
    '_card_from_result',
    '_ground_candidate',
    '_ground_correction',
    # ── interpretation helpers ──
    '_parse_llm_json',
    '_research_and_interpret',
    # ── patchable dependency seams (tests monkeypatch these on ``re_mod``) ──
    'dispatch_stream',
    'search_arxiv',
    'fetch_arxiv_title',
    '_extract_arxiv_id',
    '_execute_report_tool',
    'display_query_for',
    'parse_and_repair_tool_args',
    'date_anchor_clause',
    '_REPORT_TOOLS',
    'AbortedError',
]
