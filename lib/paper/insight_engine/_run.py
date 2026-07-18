"""Orchestration — the public insight entrypoints.

Holds :func:`generate_insight` (grounded synthesis + render), the persistence
helper :func:`_persist_insight`, and the gated report-path entry
:func:`run_report_insight`.

Every dependency a test monkeypatches — ``_build_reader_context``,
``_self_identity``, ``score_report_rubric``, ``_persist_insight`` (and, through
``generate_insight``, the ``dispatch_stream`` / arXiv seams inside
``_research_and_synthesize`` / ``_ground_insight``) — is resolved THROUGH the
package facade at call time (``import lib.paper.insight_engine as _pkg``) so a
patch on ``ie.<name>`` bites exactly as it did in the original flat module.
"""

import json
import time

from lib.llm_errors import AbortedError
from lib.log import get_logger

from ._config import INSIGHT_GATE_THRESHOLD, insight_gate_fires, insight_lang_key

logger = get_logger(__name__)


def generate_insight(paper_text, report_md, ui_lang='en', *, phash='',
                     model=None, project_path=None, abort=None, on_tool_event=None,
                     self_arxiv_id=None, self_title=None, allow_personal_context=True):
    """Produce a grounded insight section for a paper, given its report.

    Args:
        paper_text: full (or truncated) paper text — reference material.
        report_md: the already-written fidelity report — the primary material.
        ui_lang: 'en' | 'zh'.
        phash: paper hash (used to exclude the current paper from the library
            context and — by the caller — to persist).
        model: optional model override.
        project_path: optional project path for memory scoping.
        abort: optional zero-arg predicate (trips the loop's abort checks).
        on_tool_event: optional callback for research tool_start/tool_done.
        self_arxiv_id, self_title: identity of the paper under analysis, used to
            suppress self-referential connections (a foundational paper's own
            descendants tempt the model to bridge it back to itself). Resolved
            from phash/report when not given.
        allow_personal_context: when False (fail-closed default on every
            headless/BYO surface — see lib/agent_core/personal_scope), the
            operator-personal "reader context" (their paper library + memory
            store) is NOT injected, so one operator's reading history never
            leaks into an unrelated caller's paper analysis. The pass still
            runs; it just loses its personal transfer bridges.

    Returns:
        {
          'insight': <grounded insight dict> | None,
          'markdown': <rendered section str>,
          'grounded': int, 'dropped': int, 'selfref': int,
          'llmError': bool,
        }
    """
    import lib.paper.insight_engine as _pkg

    out = {'insight': None, 'markdown': '', 'grounded': 0, 'dropped': 0,
           'selfref': 0, 'llmError': False}
    if not (report_md or '').strip() and not (paper_text or '').strip():
        logger.warning('[Paper:Insight] Nothing to synthesize (empty report + paper)')
        return out

    if allow_personal_context:
        reader_context = _pkg._build_reader_context(phash, report_md, paper_text, ui_lang, project_path)
    else:
        # Headless / BYO surface: never splice the operator's library+memories
        # into an unrelated caller's analysis (personal_scope fail-closed).
        reader_context = ''
        logger.info('[Paper:Insight] hash=%s — personal reader-context SUPPRESSED '
                    '(headless / opt-out)', phash)

    try:
        insight = _pkg._research_and_synthesize(
            paper_text, report_md, reader_context, ui_lang,
            model=model, abort=abort, on_tool_event=on_tool_event)
    except AbortedError:
        logger.info('[Paper:Insight] Synthesis aborted for hash=%s', phash)
        return out
    except Exception as e:
        logger.error('[Paper:Insight] Synthesis failed for hash=%s: %s', phash, e, exc_info=True)
        out['llmError'] = True
        return out

    if not isinstance(insight, dict):
        logger.warning('[Paper:Insight] No usable insight JSON for hash=%s', phash)
        return out

    self_aid, self_ttl = _pkg._self_identity(phash, report_md, self_arxiv_id, self_title)
    grounded, dropped, selfref = _pkg._ground_insight(insight, self_aid=self_aid, self_title=self_ttl)
    out['insight'] = insight
    out['grounded'] = grounded
    out['dropped'] = dropped
    out['selfref'] = selfref
    out['markdown'] = _pkg.render_insight_markdown(insight, ui_lang)
    logger.info('[Paper:Insight] hash=%s — %d grounded / %d dropped / %d self-ref dropped, %d chars',
                phash, grounded, dropped, selfref, len(out['markdown']))
    return out


def _persist_insight(phash, ui_lang, markdown, model):
    """Persist an insight section under the ``insight:<ui_lang>`` key.

    Reuses the report engine's EXACT write-path (``upsert(db, PAPER_REPORTS,
    …)``) — no hand-rolled second writer — so the insight row obeys the same
    schema, upsert semantics, and PG/SQLite bridge as every other paper_reports
    row. The composite lang key keeps it a SEPARATE row from the plain report,
    so it never overwrites the fidelity report.
    """
    try:
        from lib.database import get_thread_db
        from lib.database._core_schema import PAPER_REPORTS, upsert
        db = get_thread_db()
        upsert(db, PAPER_REPORTS, {
            'paper_hash': phash,
            'lang': insight_lang_key(ui_lang),
            'report': markdown,
            'model': model or '',
            'meta': json.dumps({'kind': 'insight'}, ensure_ascii=False),
            'created_at': int(time.time()),
        }, retry=True)
        logger.info('[Paper:Insight] Persisted insight — hash=%s key=%s %d chars',
                    phash, insight_lang_key(ui_lang), len(markdown))
        return True
    except Exception as e:
        logger.warning('[Paper:Insight] Failed to persist insight hash=%s: %s', phash, e)
        return False


def run_report_insight(paper_text, report_md, ui_lang='en', *, phash='',
                       model=None, project_path=None, abort=None, on_tool_event=None,
                       self_arxiv_id=None, self_title=None, allow_personal_context=True,
                       persist=True):
    """Gated, persisted insight pass — the report-path entry point.

    Pipeline (the production shape validated by the gated A/B):
      1. Score the just-written report on the 4 insight axes (ONE no-tool rubric
         call — the gate input).
      2. HEADROOM GATE: only proceed when the report's own insight baseline is
         at/below ``INSIGHT_GATE_THRESHOLD`` (the pass helps low-baseline
         explainers, no-ops/hurts insight-saturated ones — proven at n=7 fired,
         CI lower 1.00). A ``None`` baseline fails OPEN.
      3. Generate the grounded insight (self-ref-suppressed, personal-context
         gated by ``allow_personal_context``).
      4. Persist under ``insight:<ui_lang>`` reusing the report write-path.

    Returns:
        {
          'fired': bool,           # did the gate let it run?
          'baseline': float|None,  # the report's own insight-rubric overall
          'insight': dict|None, 'markdown': str,
          'grounded': int, 'dropped': int, 'selfref': int,
          'persisted': bool, 'llmError': bool,
        }
    """
    import lib.paper.insight_engine as _pkg

    out = {'fired': False, 'baseline': None, 'insight': None, 'markdown': '',
           'grounded': 0, 'dropped': 0, 'selfref': 0, 'persisted': False,
           'llmError': False}

    baseline = None
    verdict = _pkg.score_report_rubric(report_md, model=model, abort=abort)
    if verdict:
        baseline = verdict['overall']
    out['baseline'] = baseline

    if not insight_gate_fires(baseline):
        logger.info('[Paper:Insight] Gate WITHHELD — hash=%s baseline=%.2f > %.1f '
                    '(report already insight-saturated)', phash, baseline or -1,
                    INSIGHT_GATE_THRESHOLD)
        return out

    out['fired'] = True
    logger.info('[Paper:Insight] Gate FIRED — hash=%s baseline=%s (<= %.1f)',
                phash, f'{baseline:.2f}' if baseline is not None else 'None',
                INSIGHT_GATE_THRESHOLD)

    gen = _pkg.generate_insight(
        paper_text, report_md, ui_lang, phash=phash, model=model,
        project_path=project_path, abort=abort, on_tool_event=on_tool_event,
        self_arxiv_id=self_arxiv_id, self_title=self_title,
        allow_personal_context=allow_personal_context)

    out.update({k: gen[k] for k in ('insight', 'markdown', 'grounded', 'dropped',
                                    'selfref', 'llmError')})

    if persist and gen.get('markdown') and gen.get('insight'):
        out['persisted'] = _pkg._persist_insight(phash, ui_lang, gen['markdown'], model)
    return out
