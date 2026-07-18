"""Post-report agentic second-pass hooks for paper report generation.

Two best-effort, gated, non-destructive passes that run AFTER the fidelity
report is DONE + persisted:

* :func:`_maybe_run_insight`   — the gated insight synthesis/transfer pass
  (delegates to ``lib.paper.insight_engine.run_report_insight``).
* :func:`_maybe_run_termfill`  — the gated definition-backfill pass
  (delegates to ``lib.paper.terminology_backfill.run_report_termfill``).

Both are lazily-importing (``.insight_engine`` / ``.terminology_backfill`` /
``.review`` are resolved at call time to keep the facade import cheap and avoid
import cycles) and emit events through ``_append_report_event`` on the same
task event log the report uses.

Split out of the flat ``report_engine.py`` while preserving
``from lib.paper.report_engine import _maybe_run_insight`` / ``_maybe_run_termfill``
(and the ``.report_engine`` relative form) byte-for-byte via the package facade.
"""

from lib.log import get_logger

from ..report_runtime import _append_report_event

logger = get_logger(__name__)


def _maybe_run_insight(task, phash, ui_lang, report_md, *, truncated_paper, model):
    """Run the gated insight second-pass after a report completes (best-effort).

    Skips entirely unless ``TOFU_PAPER_INSIGHT`` is on and this is a plain
    report (never Review Mode). Emits an ``insight_start`` event so the reader
    can show a "synthesizing insight…" affordance, then either an ``insight``
    event carrying the rendered section (gate fired + produced) or an
    ``insight_skipped`` event (gate withheld / nothing produced). Persistence is
    handled inside ``run_report_insight`` (``insight:<ui>`` key).

    ``allow_personal_context`` is resolved via ``lib/agent_core/personal_scope``
    from the task's cfg — the interactive report route leaves it unset → the
    resolver defaults True (owner keeps the transfer moat); every headless
    cfg-builder stamps ``paperInsightPersonalContext=False`` so a BYO caller's
    analysis never gets the operator's library/memories.
    """
    from ..insight_engine import insight_enabled, run_report_insight
    from ..review import is_review_family

    if not insight_enabled():
        return
    if is_review_family(task.get('lang') or ''):
        return
    if not (report_md or '').strip():
        return

    from lib.agent_core.personal_scope import resolve_paper_insight_personal_context
    allow_personal = resolve_paper_insight_personal_context(task.get('config'))

    abort_event = task.get('abort_event')

    def _abort():
        return bool(abort_event and abort_event.is_set())

    def _on_tool_event(ev):
        # Forward the insight research tool_start/tool_done into the SAME event
        # log the report uses, tagged so the frontend routes them to the insight
        # affordance rather than the report's tool panel.
        ev = dict(ev)
        ev['insight'] = True
        _append_report_event(task, ev)

    _append_report_event(task, {'type': 'insight_start', 'paperHash': phash})
    logger.info('[Paper:Insight] Starting gated second-pass — hash=%s ui_lang=%s '
                'personal_ctx=%s', phash, ui_lang, allow_personal)

    result = run_report_insight(
        truncated_paper, report_md, ui_lang, phash=phash, model=model,
        abort=_abort, on_tool_event=_on_tool_event,
        allow_personal_context=allow_personal)

    if result.get('markdown') and result.get('insight'):
        task['insight_text'] = result['markdown']
        _append_report_event(task, {
            'type': 'insight', 'paperHash': phash,
            'insight': result['markdown'],
            'lang': ui_lang,
            'baseline': result.get('baseline'),
            'grounded': result.get('grounded', 0),
            'selfref': result.get('selfref', 0),
        })
        logger.info('[Paper:Insight] Emitted insight — hash=%s fired=%s baseline=%s '
                    '%d chars', phash, result['fired'], result.get('baseline'),
                    len(result['markdown']))
    else:
        _append_report_event(task, {
            'type': 'insight_skipped', 'paperHash': phash,
            'fired': result.get('fired', False),
            'baseline': result.get('baseline'),
            'llmError': result.get('llmError', False),
        })
        logger.info('[Paper:Insight] No insight surfaced — hash=%s fired=%s baseline=%s',
                    phash, result.get('fired'), result.get('baseline'))


def _maybe_run_termfill(task, phash, ui_lang, report_md, report_meta, *, model):
    """Run the gated definition-backfill second pass (best-effort, additive).

    Skips unless ``TOFU_PAPER_TERMFILL`` is on, this is a plain report (never
    Review Mode), and the terminology audit actually flagged a gap
    (``report_meta['terminologyAudit']`` present). Generates a gap-closing
    glossary addendum (pure body context, re-audit gated), persists it under the
    SEPARATE ``termfill:<ui>`` key, and emits a ``termfill`` event carrying the
    addendum so the live reader sees the added definitions and the frontend can
    downgrade the warning card. The primary persisted report body is untouched
    (byte-identical whether this runs or not) — mirrors the insight pass.
    """
    from lib.agent_core.personal_scope import resolve_paper_termfill_enabled

    from ..review import is_review_family
    from ..terminology_backfill import run_report_termfill, termfill_globally_disabled

    # Fleet-wide kill switch first, then the per-request gate (interactive ON,
    # headless opt-in — resolved from task['config'] via the personal-scope
    # registry, the same seam the insight pass uses).
    if termfill_globally_disabled():
        return
    if not resolve_paper_termfill_enabled(task.get('config')):
        return
    if is_review_family(task.get('lang') or ''):
        return
    audit = (report_meta or {}).get('terminologyAudit')
    if not audit:
        return
    if not (report_md or '').strip():
        return

    logger.info('[Paper:TermFill] Starting gated backfill — hash=%s ui_lang=%s '
                'gaps=%s', phash, ui_lang, audit.get('counts'))
    result = run_report_termfill(report_md, ui_lang, phash=phash, model=model,
                                 audit=audit)
    if result.get('markdown') and result.get('closed'):
        task['termfill_text'] = result['markdown']
        _append_report_event(task, {
            'type': 'termfill', 'paperHash': phash,
            'addendum': result['markdown'], 'lang': ui_lang,
        })
        logger.info('[Paper:TermFill] Emitted addendum — hash=%s %d chars',
                    phash, len(result['markdown']))
    else:
        _append_report_event(task, {'type': 'termfill_skipped', 'paperHash': phash})
        logger.info('[Paper:TermFill] No gap-closing addendum surfaced — hash=%s', phash)
