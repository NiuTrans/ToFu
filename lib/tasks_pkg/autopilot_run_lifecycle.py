"""Autopilot run close-out lifecycle — extracted from ``autopilot.py`` ( slice 3).

**Extraction context** (board epic ```, slice 3):
carved out of ``lib/tasks_pkg/autopilot.py`` per
``docs/modules/task_engine.md``. Chose a SIBLING module rather
than a package conversion, matching slice 1 (``autopilot_state.py``) and
slice 2 (``autopilot_markers.py``).

**Why this slice closes the two-module cycle** (slice 2's residual debt):

  Slice 2 moved ``disarm_autopilot`` into ``autopilot_markers.py``, but
  its call to ``conclude_run`` had to stay lazy-imported at function
  scope because ``conclude_run`` remained in ``autopilot.py`` — and
  ``autopilot.py`` in turn re-exports the markers cluster from
  ``autopilot_markers``. That formed a two-module cycle that only
  worked because of the lazy-import posture.

  This slice moves ``conclude_run`` (and its three helpers
  ``_store_run_record``, ``_emit_run_concluded``,
  ``_emit_run_concluded_event``) into a brand-new LEAF module. The
  dependency chain becomes strictly downward::

      autopilot.py  ──(re-export)──▶ autopilot_run_lifecycle.py
                                                 │
                                                 ▼
                                       autopilot_state.py
      autopilot_markers.py  ──(top-level import)──▶ autopilot_run_lifecycle.py
                                                            │
                                                            ▼
                                                  autopilot_state.py

  ``autopilot_markers.py`` now imports ``conclude_run`` at MODULE TOP
  (no more lazy hack), because the leaf module has ZERO imports from
  ``autopilot.py``. The cycle is ELIMINATED, not merely guarded — a
  strict architectural upgrade over slice 2's lazy-import contract.

** sequencing constraint**: the sibling epic
``` (owner-parked, human-gated) plans to mutate
``_VUEventForwarder``, the ``_autopilot_deciding`` latch, and the VU
``convId=''`` opt-out. This module carries NONE of those symbols — the
close-out lifecycle is strictly disjoint from that mutation surface.

**What's in here**: the four functions that persist the terminal
"this autopilot run is over" fact + emit the associated pulse/SSE:

  * :func:`_store_run_record` — writes the single per-run record into
    ``settings.autopilotSummaries[run_id]`` under a per-conv lock, with
    reason-precedence + report-preservation + retention pruning.
  * :func:`_emit_run_concluded` — emits ONE project-brain
    ``run_concluded`` Activity event keyed on the run's project.
  * :func:`conclude_run` — the top-level close-out seam for the paths
    that end a run WITHOUT a clean ``[VU: TASK_DONE]`` (manual Stop,
    toggle-OFF disarm, superseding real user message).
  * :func:`_emit_run_concluded_event` — the report-free close-out that
    persists the record AND fires the pulse AND the SSE frame.

The facade module ``lib.tasks_pkg.autopilot`` re-exports every symbol
identity-preservingly so existing ``from lib.tasks_pkg.autopilot import
conclude_run`` call sites (autopilot_markers.disarm_autopilot; internal
autopilot.py callers) and tests that ``monkeypatch.setattr(ap,
'conclude_run', ...)`` keep working byte-identically.
"""

from __future__ import annotations

import time
import uuid

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger

from lib.tasks_pkg.autopilot_state import (
    _clear_run_id,
    _resolve_recent_run_id,
    _resolve_run_anchor_turn_id,
)

logger = get_logger(__name__)




def _store_run_record(conv_id: str, run_id: str, *,
                      user_id: int,
                      reason: str,
                      text: str = '',
                      unsent: bool = False,
                      translated: str = '') -> dict | None:
    """Persist the SINGLE authoritative per-run record in the SIDECAR.

    ONE record per run carries BOTH facts the frontend needs — that the run has
    ``status='concluded'`` (with its ``reason``) AND the optional close-out
    ``content`` (a manual stop has none). There is deliberately no second map
    and no ``status='running'`` record: only the TERMINAL fact is persisted, so
    the frontend's ``syncConversationToServer`` (which rebuilds the whole
    ``settings`` column from a whitelist) can never clobber a mid-run marker it
    doesn't yet hold.

    Stored under ``settings.autopilotSummaries[run_id]`` (kept for the existing
    read/write/IDB whitelist + reload round-trip), which surfaces to the
    frontend as ``conv.autopilotSummaries[run_id]`` without ever becoming a chat
    turn. The frontend gates the run fold on ``rec.status==='concluded'`` and
    renders ``content`` (when present) as the fold's read-only report panel.

    Read-modify-write is idempotent per run: re-concluding merges (a later
    ``task_done`` with a report supersedes an earlier bare ``stopped`` marker,
    and never downgrades a report back to empty). Returns the stored record
    (``{runId, status, reason, content?, translatedContent?, ts, _summaryId}``),
    or ``None`` on failure. The record has no ``role``/``_turnId`` — it is
    not a message.
    """
    try:
        from lib.agent_verdict import autopilot_summary_retention
        from lib.conversations import update_conversation_settings
    except Exception as e:
        logger.warning('[Autopilot] run record: import failed: %s', e)
        return None
    # Resolve the run's boundary turn identity once, server-side — the
    # backend authority for report placement. Read outside the settings mutation
    # (a separate column) so the frontend never has to re-derive placement by
    # scanning run-id stamps. '' when the run's turns aren't on disk yet.
    anchor_turn_id = _resolve_run_anchor_turn_id(
        conv_id, run_id, user_id=user_id)
    try:
        # Serialized read-merge-write (settings_store): autopilotSummaries
        # ACCRETES run records — a bare RMW would drop a concurrently-stored
        # sibling run. Merge the run under the per-conv lock on the freshest
        # blob so no run record is lost.
        out = {'record': None}

        def _mut(settings):
            summaries = settings.get('autopilotSummaries')
            if not isinstance(summaries, dict):
                summaries = {}
            prior = summaries.get(run_id) if isinstance(summaries.get(run_id), dict) else {}
            # Never downgrade a run that already carries a report to an empty
            # one: a later manual-stop conclude on a run that cleanly reported
            # keeps the report + upgrades the reason only if new one is task_done.
            content = text or (prior.get('content') or '')
            translated_final = translated or (prior.get('translatedContent') or '')
            # Reason precedence (a later conclude never downgrades a stronger
            # prior — manual stop / task_done can race): 'task_done'
            # (verified-complete) is strongest; everything else
            # (stopped / budget_exhausted / …) is weaker.
            _RANK = {'task_done': 3}
            _prior_reason = prior.get('reason') or ''
            reason_final = (_prior_reason
                            if _RANK.get(_prior_reason, 0) > _RANK.get(reason, 0)
                            else reason)
            record = {
                'runId': run_id,
                'status': 'concluded',
                'reason': reason_final,
                'ts': int(time.time() * 1000),
                '_summaryId': prior.get('_summaryId') or str(uuid.uuid4()),
            }
            # BACKEND-AUTHORITATIVE PLACEMENT — the stable turn id of the run's
            #   boundary turn. Resolved server-side (above) where the run's turns
            #   are known; the frontend docks the report at this id (a pure
            #   lookup) instead of inferring the boundary from run-id stamps.
            #   STICKY: a prior anchor ALWAYS wins over a fresh re-resolution.
            #   The anchor is resolved+persisted ONCE, synchronously, at the
            #   conclude point (the TASK_DONE sync pre-stamp below, or the
            #   manual-stop conclude_run) — while the run's boundary is known
            #   and stable. A later write (the async report-content fill ~63s
            #   later, which may run AFTER the user started a new round) must
            #   NEVER move it: recomputing then could drift the boundary past a
            #   newer turn and offset the report to the transcript tail. So once
            #   stamped, keep it; only compute fresh when there is no prior.
            anchor_final = (prior.get('anchorTurnId') or '') or anchor_turn_id
            if anchor_final:
                record['anchorTurnId'] = anchor_final
            # A run cut off by a safety cap (budget_exhausted / stuck /
            #   no_progress) is UNFINISHED — the objective is unverified. Flag
            #   it so the fold renders "stopped early — needs review" instead of
            #   a clean conclusion. A later clean task_done supersedes it (the
            #   reason_final no-downgrade rule above), so re-concluding clears it.
            from lib.agent_verdict import is_incomplete_stop
            if is_incomplete_stop(reason_final):
                record['incomplete'] = True
            if content:
                record['content'] = content
            # UNSENT — ``content`` here is a VU reply that was PRODUCED but
            #   never delivered into the conversation (the run yielded to a
            #   human / was superseded mid-flight). The frontend must label it
            #   as such: it is evidence of work done, NOT a turn that happened,
            #   and it deliberately does NOT exist in ``conv.messages`` (that
            #   list is the history sent upstream — injecting an undelivered VU
            #   reply there would make the model read it back as words the
            #   human said). Sticky like the anchor: once a record is marked
            #   unsent, a later bare re-conclude must not quietly clear the
            #   label while the text is still the unsent one.
            if unsent or (prior.get('unsent') and content
                          and content == (prior.get('content') or '')):
                record['unsent'] = True
            if translated_final:
                record['translatedContent'] = translated_final
            summaries[run_id] = record
            # RETENTION — the map accretes one record per run and re-serializes
            #   into every settings PUT + IDB write, so on a year-scale
            #   conversation an unbounded map makes each turn's write cost grow
            #   O(n).  Keep the N most-recent runs by ``ts``; the run currently
            #   being concluded (``run_id``) is ALWAYS retained (it's the
            #   freshest ts, but we also force-keep it so a clock skew can't
            #   evict the live fold's own record).
            retain = autopilot_summary_retention()
            if retain and len(summaries) > retain:
                def _rec_ts(item):
                    rid, rec = item
                    if rid == run_id:
                        return float('inf')  # never evict the current run
                    try:
                        return float((rec or {}).get('ts') or 0)
                    except (TypeError, ValueError) as e:
                        logger.debug('[Autopilot] float ts parse failed, using fallback: %s', e)
                        return 0.0
                kept = sorted(summaries.items(), key=_rec_ts, reverse=True)[:retain]
                evicted = len(summaries) - len(kept)
                summaries = dict(kept)
                logger.info('[Autopilot] conv=%s pruned autopilotSummaries: '
                            'evicted %d oldest run record(s), kept %d (cap=%d)',
                            conv_id[:8], evicted, len(summaries), retain)
            settings['autopilotSummaries'] = summaries
            out['record'] = record
            logger.info('[Autopilot] conv=%s ✅ Stored concluded run record in sidecar '
                        '(reason=%s, %d report chars, run=%s, NOT a message)',
                        conv_id[:8], reason_final, len(content), run_id)
            return None

        res = update_conversation_settings(
            conv_id, _mut, user_id=user_id)
        if res is None:
            logger.warning('[Autopilot] run record: conv=%s not found', conv_id[:8])
            return None
        return out['record']
    except Exception as e:
        logger.error('[Autopilot] conv=%s run record sidecar store failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None




def _emit_run_concluded(
    conv_id: str,
    run_id: str,
    text: str,
    config: dict | None,
    *,
    user_id: int,
) -> None:
    """Emit ONE project-brain 'run_concluded' Activity event for a finished run.

    Autopilot per-turn 'started'/'completed' events are SUPPRESSED at the task
    seams (config.autopilotRunId set), so a deep run surfaces in the feed as a
    single human-meaningful pulse here, at run close-out — keyed on the run's
    project (config.projectPath). Best-effort: never raises into the caller.
    """
    try:
        proj = ((config or {}).get('projectPath') or '').strip()
        if not proj or not conv_id:
            return
        from lib.conversations.project_feed import emit_project_event
        summary = (text or '').strip().splitlines()[0] if text else ''
        emit_project_event(
            proj, conv_id, 'run_concluded',
            summary or 'Autopilot run concluded',
            user_id=user_id,
            payload={'runId': run_id})
    except Exception as e:
        logger.debug('[Autopilot] run_concluded feed emit skipped: %s', e)




def conclude_run(
    conv_id: str,
    *,
    user_id: int,
    reason: str = 'stopped',
    run_id: str = '',
) -> dict | None:
    """Record the BACKEND-AUTHORITATIVE 'this autopilot run is over' fact.

    The single close-out seam for the paths that end a run WITHOUT a clean
    ``[VU: TASK_DONE]`` — a manual Stop, the toggle-OFF / queue-cancel disarm,
    or a superseding real user message. Historically these were "dumb": they
    cleared the marker but emitted NO run-level signal, so the frontend was
    forced to INFER run-end from stream/task absence (the inter-turn-gap
    heuristic that caused premature folds). This makes the terminal fact
    explicit and durable instead.

    Writes ONE concluded record (no report ``content`` — a manual stop has
    none) to the sidecar via :func:`_store_run_record`, then clears the run pin
    (``autopilotRunId`` + objective) so the next run is fresh — exactly the
    clean-close-out ordering. Idempotent: concluding an already-concluded run
    just refreshes the record (and never downgrades a ``task_done`` verdict).

    ``run_id`` may be passed explicitly; when omitted it is resolved from the
    most recent VU turn's ``_autopilotRunId`` so an already-disarmed run still
    folds. Returns the stored record, or ``None`` when there is no run to
    conclude (no run id resolvable → nothing was ever an autopilot run).
    """
    if not conv_id:
        return None
    if not run_id:
        run_id = _resolve_recent_run_id(conv_id, user_id=user_id)
    if not run_id:
        logger.debug('[Autopilot] conclude_run: conv=%s no run id — nothing to '
                     'conclude', conv_id[:8])
        return None
    record = _store_run_record(
        conv_id, run_id, user_id=user_id, reason=reason)
    _clear_run_id(conv_id, user_id=user_id)
    return record




def _emit_run_concluded_event(task: dict, conv_id: str, run_id: str,
                              reason: str = 'task_done') -> dict | None:
    """Persist the terminal fold record + emit the run-concluded pulse/SSE.

    The report-free close-out seam. The A-layer close-out REPORT (the LLM
    reporter turn + its sidecar ``content``) was removed; what remains is the
    B-layer fold machinery both close-out paths still need:

      1. write the BACKEND-AUTHORITATIVE concluded record via
         :func:`_store_run_record` (``status='concluded'``, ``reason``,
         ``anchorTurnId`` — NO report ``content``);
      2. emit the project-brain ``run_concluded`` Activity pulse;
      3. emit the ``autopilot_run_concluded`` SSE so a connected client folds
         the run's (VU→assistant)×N transcript immediately.

    ``reason`` is ``'task_done'`` on a clean ``[VU: TASK_DONE]`` and the guard
    reason (``budget_exhausted`` / ``stuck`` / …) on an abnormal cutoff — the
    latter stamps the record ``incomplete`` (via ``_store_run_record``) so the
    fold renders "stopped early — needs review". Returns the stored record, or
    ``None`` on persist failure. Best-effort on the emit steps.
    """
    from lib.tasks_pkg.manager import append_event
    from lib.tasks_pkg.manager import task_user_id

    tid = task['id'][:8]
    owner_user_id = int(task_user_id(task))
    record = _store_run_record(
        conv_id, run_id, user_id=owner_user_id, reason=reason)
    if record is None:
        return None

    _emit_run_concluded(
        conv_id,
        run_id,
        '',
        task.get('config'),
        user_id=owner_user_id,
    )

    try:
        append_event(task, build_event(
            EventType.AUTOPILOT_RUN_CONCLUDED,
            runId=run_id,
            record=record,
        ))
    except Exception as e:
        logger.debug('[Autopilot %s] run-concluded emit failed: %s', tid, e)
    return record


__all__ = [
    '_store_run_record',
    '_emit_run_concluded',
    'conclude_run',
    '_emit_run_concluded_event',
]
