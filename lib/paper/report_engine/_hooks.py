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


def _merge_second_pass(task, phash, name, payload, *, model=None):
    """Fold a second pass's billed usage into report_meta['secondPasses'].

    Design §3.3 (cost visibility): every post-report pass (insight /
    termfill / checkpoints) bills REAL tokens the finish tag must account
    for. This helper:

      1. updates ``task['report_meta']['secondPasses'][name]`` in place
         (usage + pass-specific verdict fields + computed cost);
      2. recomputes the meta's TOTAL cost (body + Σ passes) into
         ``totalCostCny`` / ``totalCostUsd``;
      3. re-persists ONLY the meta column of the already-written
         paper_reports row (the body was persisted before the hooks ran —
         meta predates the passes, so it must be re-written);
      4. emits a ``report_meta`` event so a live reader's finish tag hot-
         updates (the ``done`` event's meta predates the passes).

    Best-effort: any failure logs and returns — the passes themselves are
    already done and their content events already emitted.
    """
    meta = task.get('report_meta')
    if not isinstance(meta, dict):
        logger.debug('[Paper:SecondPass] no report_meta on task — skip %s merge', name)
        return
    entry = {k: v for k, v in (payload or {}).items() if v is not None}
    usage = entry.get('usage')
    if isinstance(usage, dict):
        try:
            from lib.cost import compute_cost
            cost = compute_cost(usage, model_id=model or meta.get('model') or '',
                                provider_id=meta.get('providerId') or None)
            if cost:
                entry['costCny'] = cost.get('costCny')
                entry['costUsd'] = cost.get('costUsd')
        except Exception as e:
            logger.warning('[Paper:SecondPass] %s cost computation failed: %s', name, e)
    passes = meta.setdefault('secondPasses', {})
    passes[name] = entry

    # Total = body usage + Σ pass usages (token fields), re-costed once.
    try:
        keys = ('prompt_tokens', 'completion_tokens', 'cache_read_tokens',
                'cache_write_tokens', 'reasoning_tokens')
        total = {k: int(meta.get(_camel(k), 0) or 0) for k in keys}
        for p in passes.values():
            u = (p or {}).get('usage') or {}
            for k in keys:
                total[k] += int(u.get(k, 0) or 0)
        from lib.cost import compute_cost
        tcost = compute_cost(total, model_id=model or meta.get('model') or '',
                             provider_id=meta.get('providerId') or None)
        if tcost:
            meta['totalCostCny'] = tcost.get('costCny')
            meta['totalCostUsd'] = tcost.get('costUsd')
        meta['totalUsage'] = total
    except Exception as e:
        logger.warning('[Paper:SecondPass] total-cost computation failed: %s', e)

    # Meta-only re-persist (body untouched). The row was written before the
    # hooks ran; UPDATE the single column rather than re-upserting the body.
    try:
        import json as _json
        from lib.database import get_thread_db
        db = get_thread_db()
        db.execute(
            "UPDATE paper_reports SET meta = ? WHERE paper_hash = ? AND lang = ?",
            (_json.dumps(meta, ensure_ascii=False), phash, task.get('lang') or ''))
        logger.info('[Paper:SecondPass] meta re-persisted — hash=%s pass=%s',
                    phash, name)
    except Exception as e:
        logger.warning('[Paper:SecondPass] meta re-persist failed hash=%s pass=%s: %s',
                       phash, name, e)

    _append_report_event(task, {'type': 'report_meta', 'paperHash': phash,
                                'meta': meta})


def _camel(snake_key):
    """prompt_tokens → promptTokens (the report meta's camelCase usage keys)."""
    parts = snake_key.split('_')
    return parts[0] + ''.join(p.title() for p in parts[1:])


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

    # Four-level enable chain (design §3.4): per-request cfg (headless stamp
    # / opt-in) > server_config user toggle > env fleet seed > default ON.
    if not insight_enabled(task.get('config')):
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

    # Cost visibility (design §3.3): fold this pass's billed usage into the
    # report meta's secondPasses breakdown, re-persist the row's meta, and
    # notify live readers via a report_meta event — whether or not the gate
    # fired (a withheld gate still paid for the rubric scoring).
    _merge_second_pass(task, phash, 'insight', {
        'fired': result.get('fired', False),
        'baseline': result.get('baseline'),
        'usage': result.get('usage'),
    }, model=model)

    if result.get('markdown') and result.get('insight'):
        task['insight_text'] = result['markdown']
        _append_report_event(task, {
            'type': 'insight', 'paperHash': phash,
            'insight': result['markdown'],
            # v2 structured payload (grounded items with resolved anchor_idx)
            # — the reader distributes anchored cards from this; the markdown
            # stays for legacy/plain rendering.
            'items': result.get('insight'),
            'lang': ui_lang,
            'baseline': result.get('baseline'),
            'grounded': result.get('grounded', 0),
            'selfref': result.get('selfref', 0),
            'anchors': result.get('anchors'),
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


def _maybe_run_checkpoints(task, phash, ui_lang, report_md, *, model):
    """Run the gated checkpoint second-pass after a report completes (best-effort).

    Skips unless the four-level chain allows it (design §3.4 — per-request
    cfg stamp > server_config > env > interactive default ON) and this is a
    plain report (never Review Mode). Emits a ``checkpoints`` event with the
    anchored items (flip cards distributed client-side), folds the call's
    usage into meta.secondPasses, persists under ``checkpoints:<ui>``.
    Fully wrapped — a failure here must NEVER taint the emitted report.
    """
    from ..checkpoint_engine import checkpoints_enabled, run_report_checkpoints
    from ..review import is_review_family

    if not checkpoints_enabled(task.get('config')):
        return
    if is_review_family(task.get('lang') or ''):
        return
    if not (report_md or '').strip():
        return

    abort_event = task.get('abort_event')

    def _abort():
        return bool(abort_event and abort_event.is_set())

    logger.info('[Paper:Checkpoints] Starting gated pass — hash=%s ui_lang=%s',
                phash, ui_lang)
    result = run_report_checkpoints(report_md, ui_lang, phash=phash, model=model,
                                    abort=_abort)
    _merge_second_pass(task, phash, 'checkpoints', {
        'cards': len(result.get('items') or []),
        'usage': result.get('usage'),
    }, model=model)
    if result.get('items'):
        task['checkpoints'] = result['items']
        _append_report_event(task, {
            'type': 'checkpoints', 'paperHash': phash,
            'items': result['items'], 'lang': ui_lang,
        })
        logger.info('[Paper:Checkpoints] Emitted — hash=%s %d cards',
                    phash, len(result['items']))
    else:
        _append_report_event(task, {'type': 'checkpoints_skipped',
                                    'paperHash': phash})
        logger.info('[Paper:Checkpoints] No cards surfaced — hash=%s', phash)


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
    _merge_second_pass(task, phash, 'termfill', {
        'closed': result.get('closed', False),
        'usage': result.get('usage'),
    }, model=model)
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
