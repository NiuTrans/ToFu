"""Conversation synchronization — writing task results/partials back into the
``conversations.messages`` JSON, plus the post-terminal fan-out helpers
(proactive-status update, queued-message dispatch, project-summary refresh) and
the settle-time orphan-placeholder reconcile.

These functions carry the freshness-guard + CAS-retry discipline that keeps a
stale/superseded task from clobbering a newer task's answer. They read shared
state (``_conv_latest_task``) and the low-level persist helpers from sibling
modules; ``_sync_result_to_conversation`` / ``_assign_message_ids`` are
monkeypatched by tests, so they remain facade-reachable.
"""

import json
import time
from datetime import datetime

from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
from lib.error_envelope import to_json as _err_to_json
from lib.log import get_logger
from lib.tasks_pkg.auto_translate import _maybe_auto_translate_assistant

from lib.tasks_pkg.manager._state import (
    CHECKPOINT_MIN_DELTA_CHARS,
    _conv_latest_task,
    _conv_latest_task_lock,
    _latest_task_for_conv,
    tasks,
    tasks_lock,
)
from lib.tasks_pkg.manager._events import _assign_message_ids, _new_assistant_slot
# The SINGLE SOURCE OF TRUTH for "carrier task, not user-visible work" — shared
# with /api/chat/active, the restart guard and the sidebar. BOTH conv-sync paths
# below consult it so they can never drift apart (they once did: the terminal
# path matched only `_inline_messages` while the partial path matched nothing,
# which let the autopilot VU carrier write its stop-sentinel into a conversation
# as a real assistant message). Acyclic: _registry imports only _state/_persist.
from lib.tasks_pkg.manager._registry import is_carrier_task
from lib.tasks_pkg.manager._persist import (
    _merge_tool_rounds,
    _tool_rounds_have_dedicated_home,
    _upsert_task_row,
    terminal_state_log_summary,
)

logger = get_logger(__name__)


# ── Inbox-inject sidecar lanes (swarm / peer / user-steer / stall-nudge) ─────
# The four async-inject lanes each stash a DISPLAY-ONLY record on the task under
# its own underscore key. These are persisted VERBATIM onto the settled assistant
# message under the SAME key (an underscore field, exactly like
# ``_relatedConversations`` / ``_memoryPrefetch``). CRITICAL INVARIANT: they must
# NEVER be folded into ``toolRounds`` / ``segments`` — those are the wire-replay /
# prefix-cache source (``_reconstruct_tool_call_messages`` collapses the whole
# assistant turn to a lossy summary if any round lacks toolCallId/toolContent,
# which both breaks tool-turn continuation AND shifts the wire prefix). The wire
# builders (``_build_assistant_messages`` / ``assemble_segments``) read only
# ``role`` / ``content`` / ``toolRounds`` / ``segments`` — never underscore
# fields — so persisting these is provably wire-neutral. The frontend rebuilds
# the in-timeline inject chip from these at render time; the synthetic row is
# NEVER written back into the DB ``toolRounds``.
#
# ``_stallNudges`` is the intent-stall lane. It differs from the other three in
# WHO authored the injected message: swarm/peer/steer all carry text a human or
# another agent produced, while the nudge is text the LOOP ITSELF wrote. That is
# precisely why it needs the same display-only treatment — a system-authored
# ``role='user'`` message must be legible as a system action, never mistaken for
# something the user said.
INBOX_INJECT_SIDECAR_FIELDS = ('_inboxInjects', '_peerInjects',
                              '_userSteerInjects', '_stallNudges')


def _persist_inject_sidecars(task, last_msg):
    """Copy any accumulated inbox-inject sidecar lanes from the task onto the
    settled assistant message dict (in place).

    Returns True if any field was written (so a content-guard skip branch can
    still fall through to the DB write instead of dropping the sidecar).
    """
    wrote = False
    for _f in INBOX_INJECT_SIDECAR_FIELDS:
        _v = task.get(_f)
        if _v:
            last_msg[_f] = _v
            wrote = True
    return wrote


# ── Terminal-sync ownership boundary (RENDER_CONTRACT Phase 4 §2.2) ──────────
# The set of fields the TERMINAL sync authoritatively PRODUCES. On a CAS-miss
# regraft, ONLY these are grafted from our assembled assistant onto the fresh DB
# tail — every field NOT listed survives untouched, so a translation (or any
# FUTURE translate-owned field) committed by another writer in our read→write
# window is preserved. This is an OWNED WHITELIST, deliberately not a translate
# blacklist: a new translate field added later is safe-by-default (preserved)
# without editing this list. ``segments`` is intentionally ABSENT here — its
# structure is backend-owned but its per-segment ``translatedText`` is not, so
# it gets a nested merge (``_merge_segments_preserving_translations``) instead
# of a whole-value overwrite.
_TERMINAL_OWNED_FIELDS = (
    'content', 'thinking', 'error',
    'toolRounds',
    'finishReason', 'usage', 'preset', 'toolSummary',
    'model', 'provider_id', '_taskId',
    'fallbackModel', 'fallbackFrom', 'fallbackReason', 'fallbackKind',
    'apiRounds', 'modifiedFiles', 'modifiedFileList', 'cost',
    '_memoryPrefetch', '_preferencesApplied', '_relatedConversations',
    '_preferencesLearned', '_gitSha',
    '_inboxInjects', '_peerInjects', '_userSteerInjects', '_stallNudges',
)

# Terminal-path fields that the terminal sync writes but which are DELIBERATELY
# NOT grafted by a whole-value overwrite — they get a bespoke nested merge
# instead (structure backend-owned, but sub-fields translate-owned). This is
# the second half of the ownership boundary: a key written on the terminal path
# must be registered in EXACTLY ONE of _TERMINAL_OWNED_FIELDS (overwrite) or
# _TERMINAL_MERGE_EXCLUDED (nested merge). The drift-guard test
# (test_terminal_owned_fields_cover_all_writes) fails if a new terminal write
# lands in neither set — so a future backend field can't be silently dropped by
# a stale fresh-tail value on regraft.
_TERMINAL_MERGE_EXCLUDED = ('segments',)


def _merge_segments_preserving_translations(fresh_segs, backend_segs):
    """Take the backend segments (authoritative structure + order) but backfill
    each segment's ``translatedText`` from the matching fresh segment when the
    backend one lacks it.

    Match key is ``(llmRound, type, deliverable)`` — the same tuple the
    translate commit keys its stamp on. A pure projection: it never invents or
    drops a segment, and never overwrites a translatedText the backend already
    carries. Returns the backend list (mutated in place). Falls back to the
    backend list unchanged when either side is empty / not a list.
    """
    if not isinstance(backend_segs, list):
        return backend_segs
    if not (isinstance(fresh_segs, list) and fresh_segs):
        return backend_segs
    _fresh_tr = {}
    for s in fresh_segs:
        if isinstance(s, dict) and s.get('translatedText'):
            _fresh_tr[(s.get('llmRound'), s.get('type'), bool(s.get('deliverable')))] = \
                s['translatedText']
    if not _fresh_tr:
        return backend_segs
    for s in backend_segs:
        if not isinstance(s, dict) or s.get('translatedText'):
            continue
        _k = (s.get('llmRound'), s.get('type'), bool(s.get('deliverable')))
        if _k in _fresh_tr:
            s['translatedText'] = _fresh_tr[_k]
    return backend_segs


def _find_own_assistant_slot(messages, task_id):
    """Last assistant message OWNED by ``task_id`` (provenance scan), else None.

    Used when the tail no longer belongs to this task: the slot must be
    located by WHO produced it, never by position (pt_bf93496e98b9441e).
    """
    if not task_id:
        return None
    for _m in reversed(messages):
        if (isinstance(_m, dict) and _m.get('role') == 'assistant'
                and _m.get('_taskId') == task_id):
            return _m
    return None


def _merge_terminal_fields(fresh_tail, terminal_msg):
    """Graft the backend-OWNED terminal fields from ``terminal_msg`` onto the
    fresh DB tail IN PLACE (RENDER_CONTRACT Phase 4 §2.2).

    Replaces the historical whole-dict ``_fresh_messages[-1] = terminal_msg``
    regraft, which overwrote the fresh tail wholesale and thereby DROPPED every
    field the terminal path does not itself write — most importantly a
    ``translatedContent`` (and ``segments[].translatedText``) committed by the
    auto-translate writer in the terminal sync's read→write window. Here we copy
    only the owned whitelist; all other fields on ``fresh_tail`` survive, and
    ``segments`` is merged nested so backend structure wins while per-segment
    translations are preserved. Returns ``fresh_tail``.
    """
    for _f in _TERMINAL_OWNED_FIELDS:
        if _f in terminal_msg:
            fresh_tail[_f] = terminal_msg[_f]
    # 'error' is OWNED, so its ABSENCE in ``terminal_msg`` is itself the
    # verdict (the turn settled clean): a stale error on ``fresh_tail`` must
    # converge to the verdict rather than survive the graft
    # (pt_bf93496e98b9441e — a clean answer measured wearing another task's
    # reaper error).
    if 'error' not in terminal_msg:
        fresh_tail.pop('error', None)
    if 'segments' in _TERMINAL_MERGE_EXCLUDED and 'segments' in terminal_msg:
        fresh_tail['segments'] = _merge_segments_preserving_translations(
            fresh_tail.get('segments'), terminal_msg.get('segments'))
    return fresh_tail


def _maybe_refresh_project_summary(task):
    """Post-reply trigger for the lazy project-summary generator (Layer 2).

    Fires only for completed, non-aborted project conversations. The summary
    engine itself is the gate for *whether* to regenerate (msg_count growth),
    so this just decides whether the conversation is even a project candidate
    and kicks off a background, fire-and-forget refresh.
    """
    # PAUSED: the sidebar conversation-summary feature is unstable (render
    # location + timing issues), so we no longer REQUEST generation. The
    # engine (lib/conversations/project_summary) is left intact for a later
    # revival; this trigger and the get_conversation trigger are the only two
    # request sites, both currently disabled. Revisit later.
    return


def _update_proactive_execution_status(task):
    """Update the proactive scheduler task's execution status when its agentic task completes."""
    task_id = task.get('id', '')
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        # Find any proactive task whose last_execution_task_id matches this task
        row = db.execute(
            'SELECT id FROM scheduled_tasks WHERE last_execution_task_id=? AND task_type=?',
            [task_id, 'agent']
        ).fetchone()
        if not row:
            return  # Not a proactive execution

        sched_id = row['id']
        status = task.get('status', 'done')
        exec_status = 'ok' if status == 'done' and not task.get('error') else 'error'
        now = datetime.now().isoformat()

        db.execute(
            'UPDATE scheduled_tasks SET last_execution_status=?, updated_at=? WHERE id=?',
            [exec_status, now, sched_id]
        )
        db.commit()
        logger.info('[Proactive:%s] Execution %s completed with status=%s',
                    sched_id[:8], task_id[:8], exec_status)
    except Exception as e:
        from lib.database import log_db_finalize_error
        log_db_finalize_error(logger, 'warning', e,
                              f'[Proactive] Failed to update execution status for task {task_id[:8]}')


def _dispatch_queued_message(task):
    """Check for queued messages and dispatch the next one after task completion.

    Runs in a fire-and-forget manner — failures are logged but don't affect
    the calling task's persistence.

    When a task is aborted by the user, queued messages are still dispatched —
    the user explicitly stopped the current generation, so the next queued
    message should proceed.  Only on errors do we skip dispatch (user may
    want to fix something before the queued message runs).
    """
    conv_id = task.get('convId', '')
    if not conv_id:
        return

    # Autopilot's hook (``maybe_run_autopilot``) runs immediately before us
    # and may have already spawned a synthetic-user follow-up.  Spawning a
    # second successor here would race-abort it.  The queued real message
    # is left in the queue and will be picked up when the autopilot
    # follow-up itself completes.
    if task.get('_autopilot_spawned_followup'):
        logger.info('[Queue] Skipping dispatch — autopilot already spawned '
                    'follow-up task %s for conv=%s',
                    task['_autopilot_spawned_followup'][:8], conv_id[:8])
        return

    try:
        from lib.message_queue import dispatch_next_queued, get_queue_depth
        # Check if there are queued messages before dispatching
        depth = get_queue_depth(conv_id)
        if depth == 0:
            # Event channel: the conv is going idle with an EMPTY queue — let
            # the brain start an open epic routed to it NOW (no 30 s
            # heartbeat wait). A non-empty queue drains normally; the LAST
            # drained turn's own completion hook fires this nudge then.
            _nudge_brain_dispatch(task, conv_id)
            return

        if task.get('aborted'):
            logger.info('[Queue] Task was aborted for conv=%s — dispatching next queued message (depth=%d)',
                        conv_id[:8], depth)
        new_task_id = dispatch_next_queued(conv_id)
        if new_task_id:
            logger.info('[Queue] Auto-dispatched queued message → task %s for conv=%s',
                        new_task_id[:8], conv_id[:8])
    except Exception as e:
        from lib.database import log_db_finalize_error
        log_db_finalize_error(logger, 'warning', e,
                              f'[Queue] Auto-dispatch failed for conv={conv_id[:8]}')


def _nudge_brain_dispatch(task, conv_id):
    """Brain event-channel completion nudge: a task just completed leaving an
    EMPTY queue — the conversation is going idle. If the project board has an
    open epic routed to this conv, dispatch + drain it NOW instead of waiting
    for the 30 s heartbeat sweep.

    Best-effort: any failure leaves the epic to the sweep (the unchanged
    fallback). No-op for conversations without a projectPath.
    """
    try:
        project_path = ((task.get('config') or {}).get('projectPath')
                        or '').strip()
        if not project_path:
            return
        from lib.conversations.project_dispatch import on_conv_idle
        on_conv_idle(project_path, conv_id)
    except Exception as e:
        logger.debug('[Queue] brain completion-nudge skipped conv=%s: %s',
                     conv_id[:8], e)


def _reconcile_orphan_placeholder_on_settle(task):
    """Settle-time reconcile for a task that produced NOTHING to persist.

    When a task DROPS before its first token (stream died / worker crashed /
    aborted before any delta), the frontend has already minted an empty
    assistant placeholder ({role:'assistant', content:''}) as the stream
    target, and ``_sync_result_to_conversation`` skips its normal write (no
    content/thinking/error). Without this, that orphan placeholder is never
    swept at the source: the GET-path reconcile can't touch it while the task
    is live (a live stream target is byte-identical to a ghost), so it lingers
    as a blank "Agent" bubble until a future warm reopen heals it.

    This runs the SAME authoritative pure verdict as the GET/startup paths
    (``reconcile_conversation_messages`` → ``classify_ghost_tail`` returns
    'delete' for a bare empty trailing assistant) at TASK-END, keyed by taskId.

    THE GATE IS THE WHOLE BALLGAME (mirrors the GET-path live-task gate): only
    reconcile when THIS task is still the conv's latest. If a NEWER task
    superseded it, that newer task owns any live placeholder and we must not
    delete it. Because this fires at task-end, THIS task is by definition no
    longer producing tokens — so sweeping its own orphan tail is safe once it
    is confirmed un-superseded.

    Best-effort: never raises (a reconcile failure must not break finalization).
    Cache-neutral via the live prompt-cache prefix count.
    """
    conv_id = task.get('convId', '')
    if not conv_id or task.get('_inline_messages'):
        return
    # Keyed-by-taskId gate: a newer task owns any live placeholder now.
    latest = _latest_task_for_conv(conv_id)
    if latest and latest != task['id']:
        logger.debug('[SettleReconcile] conv=%s skip — superseded by newer task %s',
                     conv_id[:8], latest[:8])
        return
    db = None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, updated_at, settings, rev FROM conversations '
            'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
        if not row:
            return
        _row_updated_at = row['updated_at']
        _row_rev = row['rev']  # Phase 4 W-settle: CAS on rev (single-shot)
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError):
            logger.debug('[SettleReconcile] conv=%s messages JSON parse failed', conv_id[:8])
            return
        if not messages:
            return

        from lib.conversations.reconcile import reconcile_conversation_messages
        prefix_n = 0
        try:
            from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
            prefix_n = get_cache_prefix_count(conv_id) or 0
        except Exception as e:
            logger.debug('[SettleReconcile] conv=%s prefix count failed: %s', conv_id[:8], e)
        cleaned, changed = reconcile_conversation_messages(messages, prefix_n)
        if not changed:
            return

        # Clear the pinned activeTaskId (this task is done) in the same write.
        settings_json = None
        try:
            s = json.loads(row[2] or '{}') if row[2] else {}
            if s.get('activeTaskId'):
                s['activeTaskId'] = None
                settings_json = json.dumps(s, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            settings_json = None

        from lib.conversations import build_search_text
        search_text = build_search_text(cleaned)
        messages_json = json_dumps_pg(cleaned)
        now_ms = int(time.time() * 1000)
        # CAS guard: only write if no concurrent update landed since our SELECT.
        if settings_json is not None:
            cur = db.execute(
                '''UPDATE conversations
                   SET messages=?, msg_count=?, settings=?, search_text=?, updated_at=?
                   WHERE id=? AND user_id=1 AND rev=?''',
                (messages_json, len(cleaned), settings_json, search_text, now_ms,
                 conv_id, _row_rev))
        else:
            cur = db.execute(
                '''UPDATE conversations
                   SET messages=?, msg_count=?, search_text=?, updated_at=?
                   WHERE id=? AND user_id=1 AND rev=?''',
                (messages_json, len(cleaned), search_text, now_ms,
                 conv_id, _row_rev))
        db.commit()
        if (getattr(cur, 'rowcount', 0) or 0) <= 0:
            logger.debug('[SettleReconcile] conv=%s CAS miss — concurrent write won (safe)',
                         conv_id[:8])
            return
        try:
            from lib.conversations import update_conversation_fts
            update_conversation_fts(db, conv_id, search_text)
        except Exception as e:
            logger.debug('[SettleReconcile] conv=%s FTS update skipped: %s', conv_id[:8], e)
        # Phase 5 dual-write (flag-gated, inert when off): reconcile drops a
        # message mid-array (re-sequences) — full rebuild mirror.
        from lib.database.messages_rows import mirror_write_and_commit
        mirror_write_and_commit(db, conv_id, cleaned, now_ms=now_ms, full=True)
        logger.info('[SettleReconcile] conv=%s swept orphaned placeholder at task-end '
                    '(%d\u2192%d msgs, dropped-before-first-token)',
                    conv_id[:8], len(messages), len(cleaned))
        try:
            from lib.conversations import notify_conv_changed
            from lib.tasks_pkg.manager._registry import task_user_id
            _rev_row = db.execute('SELECT rev FROM conversations WHERE id=? AND user_id=1',
                                  (conv_id,)).fetchone()
            notify_conv_changed(conv_id, rev=(_rev_row[0] if _rev_row else None),
                                user_id=task_user_id(task))
        except Exception as e:
            logger.debug('[SettleReconcile] conv=%s notify skipped: %s', conv_id[:8], e)
    except Exception as e:
        logger.warning('[SettleReconcile] conv=%s reconcile failed (non-fatal): %s',
                       conv_id[:8], e, exc_info=True)


def _is_floor_retry_residue(task, msg_dict):
    """True when ``msg_dict``'s content+thinking BYTE-MATCH a FloorRetry first
    attempt this task DISCARDED (recorded at adoption — see _stream.py's
    ``_floor_retry_residue``).

    The ~5s streaming checkpoint mirrors ``task['content']``/``['thinking']``
    into the conversation row DURING an attempt; when a resend is adopted the
    row can still hold the discarded draft (longer than the final answer). The
    "existing > new → frontend genuinely won" guards must NOT treat that
    residue as a frontend win — a genuine frontend write can never byte-match
    a server-internal discarded attempt. Both fields must match the SAME
    residue entry exactly, so a row the frontend genuinely touched (edited /
    translated mid-flight) still qualifies as a frontend win.
    """
    residue = (task or {}).get('_floor_retry_residue') or []
    if not residue:
        return False
    content = (msg_dict or {}).get('content') or ''
    thinking = (msg_dict or {}).get('thinking') or ''
    for r in residue:
        if (content == (r.get('content') or '')
                and thinking == (r.get('thinking') or '')):
            return True
    return False


def _is_own_vu_carrier(latest_task_id, task) -> bool:
    """True when ``latest_task_id`` is the VU carrier spawned by ``task``'s OWN
    autopilot hook (HB-1, pt_8dc03017).

    The carrier deliberately claims the conv→latest index BEFORE the parent's
    done event so the client can attach to the successor
    transport-agnostically — a DESIGNED supersede, not an unexpected
    replacement. Keyed on the plain ``task['_vu_carrier_id']`` stamp (set at
    carrier creation in autopilot.run_virtual_user), NOT a registry lookup:
    the carrier is discarded from the in-memory registry before the parent's
    trailing persist runs, so a registry probe would miss it and mislabel the
    handoff as the WARNING branch (the every-autopilot-turn false alarm seen
    in the pt_8a491f9d forensics).
    """
    return bool(latest_task_id) and latest_task_id == (task.get('_vu_carrier_id') or '')


def _sync_result_to_conversation(task, meta):
    """Write the completed task result into the conversation's messages in the DB.

    Finds or creates the last assistant message and fills in content, thinking,
    toolRounds, finishReason, etc.  This makes the backend self-sufficient —
    even if no frontend client receives the 'done' SSE event, the conversation
    is updated.

    Runs in a separate try/except so failures don't affect task_results persistence.
    """
    conv_id = task.get('convId', '')
    task_id_short = task['id'][:8]
    pfx = f'[SyncConv {task_id_short}]'

    content = task.get('content') or ''
    thinking = task.get('thinking') or ''
    error = task.get('error')

    # Skip if there's truly nothing to write (e.g. aborted before any tokens).
    # This is the drop-before-first-token case: the frontend already minted an
    # empty assistant placeholder as the stream target, so before returning we
    # sweep that orphan at the SOURCE (settle-time reconcile keyed by taskId)
    # rather than leaving it for a future warm reopen to heal.
    #
    # ★ EXCEPTION: a turn that RAN TOOLS but was Stopped before the model
    #   emitted any closing prose/thinking (content=='' and thinking=='') is
    #   NOT an empty orphan — its completed tool rounds are real user-visible
    #   work. The guard must consult the SAME "is this a real tool round?"
    #   predicate reconcile uses (reconcile.has_real_round), so the two verdicts
    #   can never drift. Without this, the rounds were written only to the
    #   task_results aborted-floor and dropped from conversations.messages,
    #   vanishing on reload/restart (the reported "all my tools disappeared").
    from lib.conversations.reconcile import has_real_round
    _merged_for_guard = _merge_tool_rounds(task)
    _has_real_tool_round = has_real_round({'toolRounds': _merged_for_guard})
    if not content and not thinking and not error and not _has_real_tool_round:
        logger.debug('%s conv=%s Skipping conv sync — no content/thinking/error/toolRounds to write', pfx, conv_id)
        try:
            _reconcile_orphan_placeholder_on_settle(task)
        except Exception as e:
            logger.debug('%s conv=%s settle-time reconcile skipped: %s', pfx, conv_id, e)
        return

    # ── FRESHNESS GUARD: reject writes from stale/superseded tasks ──
    # When a user stops a task and regenerates, a new task becomes the
    # "latest" for this conversation. The old task may still be winding
    # down (abort is cooperative), and its _sync_result_to_conversation
    # would overwrite the new task's data. This guard prevents that.
    if conv_id:
        latest = _latest_task_for_conv(conv_id)
        if latest and latest != task['id']:
            _abort_reason = task.get('_abort_reason', '')
            _autopilot_child = task.get('_autopilot_spawned_followup')
            if task.get('aborted') or _abort_reason:
                # Expected path: the user started a newer task (e.g. Stop →
                # Edit → Regenerate) for this conv, so this task was aborted
                # and is now finishing its in-flight work. Cooperative abort
                # means it only stops at the next checkpoint, so it can reach
                # this point with stale content. Skipping the write is correct
                # and routine — not an error.
                logger.debug(
                    '%s conv=%s skipping conv sync: superseded by newer task %s '
                    '(this task aborted, reason=%s, %dchars stale content discarded)',
                    pfx, conv_id[:8], latest[:8], _abort_reason or 'superseded', len(content),
                )
            elif _autopilot_child:
                # Expected path: this task's OWN autopilot hook spawned a
                # follow-up task (the virtual-user turn) before persist ran,
                # so the follow-up is now 'latest' for the conv. This task was
                # not aborted — it finished normally and was superseded by its
                # own child. The follow-up rebuilt the conversation from the DB
                # (including this task's answer), so skipping the write here is
                # correct — the autopilot append path owns the DB write.
                logger.debug(
                    '%s conv=%s skipping conv sync: superseded by own autopilot '
                    'follow-up %s (%dchars; autopilot owns the DB write)',
                    pfx, conv_id[:8], _autopilot_child[:8], len(content),
                )
            elif _is_own_vu_carrier(latest, task):
                # Expected path (HB-1, pt_8dc03017): this task's OWN autopilot
                # hook created the VU carrier sub-task, which deliberately
                # claims the conv→latest index BEFORE this task's done event
                # so the client can attach to the successor
                # transport-agnostically. The parent was not aborted (it
                # finished normally); its reply reached the conv via the
                # pre-emit sync / committedMessage. NOT an unexpected
                # replacement — the WARNING below used to fire on EVERY
                # autopilot turn (pt_8a491f9d forensics, app.log:75363).
                logger.debug(
                    '%s conv=%s skipping conv sync: superseded by own VU '
                    'carrier %s (HB-1 by design, %dchars)',
                    pfx, conv_id[:8], latest[:8], len(content),
                )
            else:
                # Unexpected: a task that was never aborted is no longer the
                # latest for its conv. This shouldn't normally happen and may
                # point to a missing abort path — worth a look.
                logger.warning(
                    '%s conv=%s skipping conv sync: superseded by newer task %s, '
                    'but this task was never aborted (%dchars discarded). '
                    'Unexpected — a new task replaced this one without aborting it.',
                    pfx, conv_id[:8], latest[:8], len(content),
                )
            return

    # ── External-caller short-circuit ──
    # Tasks started via /api/chat/start with inline `messages` in the POST
    # body (SWE-bench harness, eval tools, external backends) have no
    # corresponding row in the `conversations` table — results are read by
    # the caller from `task_results` directly. Skip the write-back path so
    # we don't flood error.log with "Conversation not found" warnings.
    if is_carrier_task(task):
        logger.debug('%s conv=%s Carrier task — skipping conv sync by design', pfx, conv_id)
        return

    db = None
    try:
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()

        if not row:
            logger.warning('%s conv=%s Conversation not found in DB — cannot sync result back', pfx, conv_id)
            return

        # Capture the CAS token for optimistic locking (RENDER_CONTRACT Phase 4:
        # W1 → rev). ``rev`` is the server-issued monotonic message-version the
        # conversations_rev_bump_trg trigger advances on EVERY genuine messages
        # change, so two writers that read the same row at the same millisecond
        # no longer both pass the guard (the updated_at same-ms clobber). We
        # still stamp ``updated_at`` in the SET clause (freshness/ordering) but
        # it is NO LONGER the CAS token. ``row`` is a sqlite3.Row / psycopg
        # DictRow — named access works for both backends.
        _row_updated_at = row['updated_at']
        _row_rev = row['rev']
        try:
            messages = json.loads(row[0] or '[]')
        except (json.JSONDecodeError, TypeError):
            logger.error('%s conv=%s Failed to parse existing messages JSON', pfx, conv_id, exc_info=True)
            return

        if not messages:
            logger.warning('%s conv=%s Conversation has 0 messages — cannot sync result back', pfx, conv_id)
            return

        # ── CROSS-TALK DETECTION: verify message count consistency ──
        # Normal cause: the frontend saves completed previous turns to the DB
        # between task creation and task completion, so the DB naturally has more
        # messages than the snapshot sent to create_task().  We only flag as a
        # true anomaly when the extra messages contain consecutive same-role
        # entries (e.g. assistant-assistant or user-user), which cannot arise
        # from normal turn-taking and may indicate cross-talk or data corruption.
        expected_msg_count = task.get('_initial_msg_count')
        if expected_msg_count is not None and len(messages) > expected_msg_count + 2:
            extra_msgs = messages[expected_msg_count:]
            extra_summary = [(m.get('role'), len(m.get('content') or ''), m.get('model', 'N/A'))
                             for m in extra_msgs]
            # ── Skip dedup when endpoint-mode history is present ──
            # Endpoint mode legitimately produces consecutive-same-role messages
            # by design: planner+worker are both role=assistant, and critic+next
            # turn user are both role=user. These are NOT cross-talk anomalies.
            # Dedup here would destroy workers (shorter than planners) and, worse,
            # drop a short new user follow-up (e.g. "why did it stop?") in favor
            # of the preceding long critic review.  The message builder already
            # collapses historical endpoint sessions before sending to the LLM,
            # so we don't need to touch the persisted conversation.
            has_endpoint_history = any(
                (m.get('_isEndpointPlanner')
                 or m.get('_isEndpointReview')
                 or m.get('_epIteration') is not None
                 or m.get('_epIter') is not None
                 or m.get('_epPlannerIteration') is not None
                 or m.get('_epNextPhase'))
                for m in messages
            )
            # Check for consecutive same-role messages in the extras
            has_consecutive_same_role = any(
                extra_msgs[i].get('role') == extra_msgs[i + 1].get('role')
                for i in range(len(extra_msgs) - 1)
            )
            if has_consecutive_same_role and has_endpoint_history:
                logger.info(
                    '%s conv=%s Message count drift (DB=%d, task_start=%d, delta=%d) '
                    'with consecutive same-role — but ENDPOINT history detected. '
                    'Skipping dedup (planner+worker and critic+next-user are '
                    'expected same-role pairs). Extra msgs: %s',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )
            elif has_consecutive_same_role:
                logger.error(
                    '%s conv=%s ⛔ MESSAGE COUNT ANOMALY with consecutive same-role: '
                    'DB has %d messages but task started with %d — %d extra. '
                    'Extra msgs: %s — auto-deduplicating',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )
                # Auto-fix: remove consecutive duplicate-role messages
                # Keep the message with more content when two same-role msgs are adjacent.
                # ★ Guard: NEVER drop the last two messages (trailing user + assistant
                #   slot) — a short new user follow-up (e.g. "why?") must always win
                #   over any earlier same-role message it might be adjacent to.
                _tail_protect_idx = max(0, len(messages) - 2)
                deduped = [messages[0]]
                for idx, m in enumerate(messages[1:], start=1):
                    if (m.get('role') == deduped[-1].get('role')
                            and idx < _tail_protect_idx):
                        # Keep the one with more content
                        existing_len = len(deduped[-1].get('content') or '')
                        new_len = len(m.get('content') or '')
                        if new_len > existing_len:
                            deduped[-1] = m
                        logger.info('%s conv=%s Removed duplicate %s message (kept %d chars, dropped %d chars)',
                                   pfx, conv_id, m.get('role'), max(existing_len, new_len), min(existing_len, new_len))
                    else:
                        deduped.append(m)
                messages = deduped
                logger.info('%s conv=%s After dedup: %d messages (was %d)',
                           pfx, conv_id, len(messages), expected_msg_count + len(extra_msgs))
            else:
                logger.debug(
                    '%s conv=%s Message count drift (DB=%d, task_start=%d, delta=%d) — '
                    'normal frontend save of previous turns. Extra msgs: %s',
                    pfx, conv_id, len(messages), expected_msg_count,
                    len(extra_msgs), extra_summary
                )

        # Find this task's assistant message to fill in.
        # ── ID-FIRST location (never a blind positional guess) ──
        # Normally the assistant slot IS the tail (messages[-1]). But when a
        # NEXT turn was enqueued while this one streamed, the queued user
        # message may already sit as a trailing pending row (role='user') below
        # our assistant slot — so a blind `messages[-1]` would see 'user', fail
        # the role check, and append a SECOND assistant, orphaning the live one
        # (the two-writer truncation the queued-pending-row design must avoid).
        # Locate our own slot by its stable `_assistantMsgId` first; only fall
        # back to the tail when the id is absent (legacy / external callers) or
        # unmatched (slot not materialized yet — the tail branch then appends).
        _amid = task.get('_assistantMsgId')
        last_msg = None
        if _amid:
            from lib.tasks_pkg.manager._events import find_message_by_id
            _idx, _by_id = find_message_by_id(messages, _amid)
            if _by_id is not None and _by_id.get('role') == 'assistant':
                last_msg = _by_id
        if last_msg is None:
            # ★ PROVENANCE GUARD (pt_bf93496e98b9441e): a tail bubble carrying
            #   a DIFFERENT task's _taskId belongs to that task — filling it
            #   grafts THIS task's terminal state onto the other task's
            #   tombstone. Measured live: a reaped task's error bubble
            #   (appended one turn late) was adopted by the NEXT task's
            #   successful sync, which inherited the stale error onto its
            #   clean 'stop' answer. A client-minted placeholder carries NO
            #   _taskId, so the legacy fill path is unaffected.
            _tail = messages[-1]
            if (_tail.get('role') == 'assistant'
                    and not (_tail.get('_taskId')
                             and _tail.get('_taskId') != task['id'])):
                last_msg = _tail

        if last_msg is None:
            # ── Guard: an aborted/superseded task must NOT append a new
            #   assistant slot ──
            # When the user clicks Stop → Regenerate, the regen handler
            # truncates the conversation down to the user turn (tail is now
            # role='user') and starts a fresh task. The old aborted task can
            # reach this point AFTER that truncation but BEFORE the new task
            # registers as `_conv_latest_task` (so the freshness guard above
            # didn't catch it). Blindly appending the stale assistant content
            # here resurrects the just-truncated turn → the "U1 A1 U1 A2"
            # doubled-context bug. Aborted tasks may only FILL an existing
            # trailing assistant slot, never create one.
            _abort_reason = task.get('_abort_reason', '')
            if _abort_reason == 'stuck_no_progress':
                # ── EXCEPTION: a reaper-wedged task OWNS its trailing user
                #   turn ONLY while that turn is still the tail. If the conv
                #   has moved on (a newer turn's prompt landed after the reap
                #   but before this sync), appending here lands the error
                #   bubble on the WRONG turn — and the next task's sync then
                #   adopts the foreign bubble (pt_bf93496e98b9441e, the
                #   ce514dce → ae7bbe38 error copy). Prefer this task's OWN
                #   settled slot (an earlier sync of the same reap may already
                #   have written it); append ONLY when the trailing user
                #   message is still this task's own prompt; otherwise DROP —
                #   the task_results terminal floor still resolves polls, and
                #   no bubble beats a bubble on the wrong turn. The narrow
                #   'stuck_no_progress' scope is asserted by
                #   test_NC_reaped_task_guard_still_blocks_regenerate_truncation.
                last_msg = _find_own_assistant_slot(messages, task['id'])
                if last_msg is None:
                    _imc = task.get('_initial_msg_count')
                    if (messages[-1].get('role') == 'user'
                            and (not isinstance(_imc, int)
                                 or len(messages) == _imc)):
                        logger.info('%s conv=%s reaped wedged task — appending '
                                    'assistant error bubble for the unanswered '
                                    'trailing turn', pfx, conv_id)
                        last_msg = _new_assistant_slot(task)
                        messages.append(last_msg)
                    else:
                        logger.warning('%s conv=%s reaped wedged task\'s trailing '
                                       'turn is no longer the tail (msgs=%d, '
                                       'initial=%s) — NOT appending the error '
                                       'bubble onto a newer turn; the '
                                       'task_results terminal floor still '
                                       'resolves polls',
                                       pfx, conv_id, len(messages), _imc)
                        return
            elif task.get('aborted') or _abort_reason:
                logger.info('%s conv=%s Last message is role=%s and this task is '
                            'aborted (reason=%s) — dropping stale write instead of '
                            'appending a new assistant (prevents truncated-turn '
                            'resurrection)',
                            pfx, conv_id, messages[-1].get('role'),
                            _abort_reason or 'aborted')
                return
            else:
                # No trailing assistant message — append one. Build the slot via
                # _new_assistant_slot so it ADOPTS the client-shipped
                # _assistantMsgId as its _msgId (divergent-id duplicate-bubble
                # root fix — see RENDER_CONTRACT §2.3 identity alignment).
                logger.info('%s conv=%s Last message is role=%s, appending new assistant message',
                           pfx, conv_id, messages[-1].get('role'))
                last_msg = _new_assistant_slot(task)
                messages.append(last_msg)

        # ── Guard: don't overwrite with LESS content ──
        # The frontend may have already synced a fuller version via PUT
        existing_content_len = len(last_msg.get('content') or '')
        existing_thinking_len = len(last_msg.get('thinking') or '')
        new_content_len = len(content)
        new_thinking_len = len(thinking)

        # ★ Merge checkpoint toolRounds for continue flow
        tool_rounds = _merge_tool_rounds(task)

        # ★ FloorRetry-residue exemption: decide BEFORE the guard. When the
        #   "longer existing content" byte-matches a first attempt THIS task
        #   discarded (recorded at adoption in _stream.py) and mirrored here
        #   by the streaming checkpoint before convergence, it is NOT a
        #   frontend win — the guard must not engage, so the normal path
        #   overwrites the residue with the authoritative final answer (the
        #   mrxij7q34xm070 root bug: the discarded 4344-char R3 draft
        #   out-ranked the 3751-char R7 answer, and the residue then rode
        #   _committedMsg verbatim to the client).
        _fr_residue_exempt = (existing_content_len > new_content_len
                              and existing_thinking_len > new_thinking_len
                              and _is_floor_retry_residue(task, last_msg))
        if _fr_residue_exempt:
            logger.info('%s conv=%s Content guard: existing=%d+%d > new=%d+%d but '
                        'existing BYTE-MATCHES this task\'s discarded FloorRetry '
                        'attempt — overwriting with the authoritative final answer',
                        pfx, conv_id, existing_content_len, existing_thinking_len,
                        new_content_len, new_thinking_len)
        if existing_content_len > new_content_len and existing_thinking_len > new_thinking_len \
                and not _fr_residue_exempt:
            # ★ FIX: Even when frontend has more content (synced before us),
            #   still update toolRounds + metadata — the backend has richer
            #   tool data (toolContent, assistantContent) that the frontend
            #   may have missed if the SSE stream broke mid-delivery.
            #   Without this, page refresh → Continue loses toolContent
            #   because the frontend's stale sync overwrote our checkpoint.
            _tr_updated = False
            if tool_rounds:
                _existing_tr = last_msg.get('toolRounds') or []
                # Only replace if we have more rounds or the existing rounds
                # are missing toolContent (frontend sync race condition)
                _existing_has_tc = all(r.get('toolContent') for r in _existing_tr if r.get('status') == 'done')
                _new_has_tc = any(r.get('toolContent') for r in tool_rounds if r.get('status') == 'done')
                if len(tool_rounds) > len(_existing_tr) or (not _existing_has_tc and _new_has_tc):
                    last_msg['toolRounds'] = tool_rounds
                    _tr_updated = True
            # Always update finishReason/metadata (frontend may not have received done event)
            if meta.get('finishReason') and not last_msg.get('finishReason'):
                last_msg['finishReason'] = meta['finishReason']
            if meta.get('usage') and not last_msg.get('usage'):
                last_msg['usage'] = meta['usage']
            if meta.get('model') and not last_msg.get('model'):
                last_msg['model'] = meta['model']
            if meta.get('provider_id') and not last_msg.get('provider_id'):
                last_msg['provider_id'] = meta['provider_id']
            # ★ The terminal error envelope is VERDICT metadata, not content —
            #   the guard protects the fuller body, never the verdict. Without
            #   this a turn that streamed partial prose and then FAILED settles
            #   its own row with finishReason='error' but NO envelope: the
            #   typed explanation (429 / quota / timeout) then lives only on
            #   whatever row the frontend happened to stamp — measured live as
            #   the duplicate-twin bubble (conv mscns5i0fcofgl, task 48ee5c90).
            #   Mirrors the normal path's set/pop exactly.
            if error:
                last_msg['error'] = error
            else:
                last_msg.pop('error', None)
            # ★ _taskId MUST be copied on this path too. It is pure PROVENANCE
            #   (which task produced this turn), never content, so writing it
            #   can never clobber the fuller content this branch is protecting.
            #   Omitting it was a real data defect: this branch stamps
            #   finishReason but not _taskId, so a turn that took it settled
            #   PERMANENTLY without a task id — measured at 24 of 42 anchor-less
            #   turns, "finishReason present + _taskId absent" being this
            #   branch's exact fingerprint. The consequence is user-visible: the
            #   per-tool-row debug entry resolves through msg._taskId, so every
            #   tool row of such a turn silently loses its entry.
            _taskid_wrote = False
            if meta.get('taskId') and not last_msg.get('_taskId'):
                last_msg['_taskId'] = meta['taskId']
                _taskid_wrote = True
            # ★ Inbox-inject sidecars: persist EVEN on the content-guard path.
            #   The frontend PUT'd fuller content before we settled, but it can
            #   never carry these (they are backend-observed at inject time), so
            #   dropping them here = "disappears on refresh". Writing them makes
            #   this branch fall through to the DB write below instead of the
            #   bare skip `return`.
            _sidecar_wrote = _persist_inject_sidecars(task, last_msg)
            if _tr_updated or meta.get('finishReason') or _sidecar_wrote or _taskid_wrote:
                logger.info('%s conv=%s Content guard: existing=%d+%d > new=%d+%d, '
                           'but still updating toolRounds=%s metadata=%s sidecar=%s taskId=%s',
                           pfx, conv_id, existing_content_len, existing_thinking_len,
                           new_content_len, new_thinking_len,
                           _tr_updated, bool(meta.get('finishReason')), _sidecar_wrote,
                           _taskid_wrote)
            else:
                logger.info('%s conv=%s Server already has MORE content (existing=%d+%d > new=%d+%d) — '
                           'frontend likely already synced. Skipping.',
                           pfx, conv_id, existing_content_len, existing_thinking_len,
                           new_content_len, new_thinking_len)
                return

        else:
            # Normal path: backend has equal or more content — update everything
            if content:
                last_msg['content'] = content
            if thinking:
                last_msg['thinking'] = thinking
            if error:
                last_msg['error'] = error
            else:
                # ★ A task settling with NO error must not leave a stale error
                #   on its own message (pt_bf93496e98b9441e): the slot can
                #   carry a transient mid-stream envelope, or — measured —
                #   ANOTHER task's verdict that rode in via a shared bubble.
                #   Terminal state is backend-authoritative: absence of an
                #   error IS the verdict.
                last_msg.pop('error', None)

        # Copy metadata fields that the frontend would normally set.
        # Terminal metadata is backend-authoritative — once the task reaches
        # this code path the backend has the truth, and any earlier value
        # the frontend sync may have written (e.g. 'interrupted' before the
        # final 'stop' arrived) is superseded.
        if tool_rounds:
            last_msg['toolRounds'] = tool_rounds
        # ★ segments (epic pt_cb8f98b0cb9b47fb, step 2): persist the THIN
        #   timeline onto the message dict too (round-trips through the
        #   conversations.messages JSON column). Co-persisted with toolRounds
        #   above, so rehydrate_segments can rebuild _round on read. Dark:
        #   nothing reads msg['segments'] yet.
        #
        # ★ pt_687b87ac root fix: RE-ASSEMBLE the timeline here, at the exact
        #   point it is consumed — never persist whatever mid-stream
        #   checkpoint assembly happens to sit on ``task['segments']``. The
        #   pre-emit sync (orchestrator/_finalize.py) otherwise stamps
        #   ``_committedMsg`` with a timeline whose terminal text segment
        #   holds only the first streamed word, while persist_task_result's
        #   later re-assembly completes the DB tail — the done frame has
        #   already left with the stale prefix. ``assemble_segments`` is a
        #   pure projection of (toolRounds, content, thinking); its only
        #   writers are the checkpoint/persist assemblies, so refreshing can
        #   never clobber hand-authored state. Best-effort: on assembly
        #   failure fall back to the task's existing list (today's behaviour).
        try:
            from lib.tasks_pkg.segments import assemble_segments
            _fresh_segs = assemble_segments(task, merged=_merge_tool_rounds(task))
            if _fresh_segs:
                task['segments'] = _fresh_segs
        except Exception as _sa_e:
            logger.warning('%s conv=%s terminal segment re-assembly failed '
                           '(non-fatal, using existing timeline): %s',
                           pfx, conv_id, _sa_e, exc_info=True)
        try:
            _segs = task.get('segments')
            if _segs:
                from lib.tasks_pkg.segments import segments_to_json
                last_msg['segments'] = segments_to_json(_segs)
        except Exception as _sm_e:
            logger.warning('%s conv=%s segments write onto message failed (non-fatal): %s',
                           pfx, conv_id, _sm_e)
        if meta.get('finishReason'):
            last_msg['finishReason'] = meta['finishReason']
        if meta.get('usage'):
            last_msg['usage'] = meta['usage']
        if meta.get('preset'):
            last_msg['preset'] = meta['preset']
        if meta.get('toolSummary'):
            last_msg['toolSummary'] = meta['toolSummary']
        if meta.get('model'):
            last_msg['model'] = meta['model']
        if meta.get('provider_id'):
            last_msg['provider_id'] = meta['provider_id']
        if meta.get('taskId'):
            last_msg['_taskId'] = meta['taskId']
        if meta.get('fallbackModel'):
            last_msg['fallbackModel'] = meta['fallbackModel']
            last_msg['fallbackFrom'] = meta.get('fallbackFrom', '')
            if meta.get('fallbackReason'):
                last_msg['fallbackReason'] = meta['fallbackReason']
            if meta.get('fallbackKind'):
                last_msg['fallbackKind'] = meta['fallbackKind']
        if meta.get('apiRounds'):
            last_msg['apiRounds'] = meta['apiRounds']
        if meta.get('modifiedFiles'):
            last_msg['modifiedFiles'] = meta['modifiedFiles']
        if meta.get('modifiedFileList'):
            last_msg['modifiedFileList'] = meta['modifiedFileList']

        # ── Cost snapshot (persisted at sync time, not lazily fetched) ──
        # Cost depends only on usage + model + provider + the pricing table
        # at the time of the call. By stamping the result onto the message
        # AND each apiRounds entry now, every render path reads the value
        # directly from msg.cost / rd.cost — zero per-render network calls.
        # See `compute_cost` in lib/cost.py.
        # The cost is "as of message time" — pricing changes do NOT
        # retroactively rewrite history. This matches what was actually
        # charged and is what billing/auditing wants anyway.
        try:
            from lib.cost import compute_cost as _compute_cost
            _msg_model = (last_msg.get('model') or meta.get('model') or '')
            _msg_provider = (last_msg.get('provider_id')
                              or meta.get('provider_id') or None)
            if last_msg.get('usage') and not last_msg.get('cost'):
                _c = _compute_cost(last_msg['usage'],
                                    model_id=_msg_model,
                                    provider_id=_msg_provider)
                if _c:
                    last_msg['cost'] = _c
            _rounds = last_msg.get('apiRounds') or []
            for _rd in _rounds:
                if not isinstance(_rd, dict):
                    continue
                if _rd.get('cost'):
                    continue
                _ru = _rd.get('usage') or {}
                if not _ru:
                    continue
                _rc = _compute_cost(
                    _ru,
                    model_id=_rd.get('model') or _msg_model,
                    provider_id=(_rd.get('provider_id')
                                  or _rd.get('providerId')
                                  or _msg_provider))
                if _rc:
                    _rd['cost'] = _rc
        except Exception as _ce:
            logger.warning('%s conv=%s Cost stamp failed (non-fatal): %s',
                           pfx, conv_id[:8] if conv_id else '?', _ce)

        # Backfill stable per-message IDs.  Newly created messages get a
        # UUID; existing messages keep theirs.  Index-free addressing is
        # what makes routes/translate.py and PATCH /messages/by-id/<mid>
        # robust against concurrent inserts.
        _assign_message_ids(messages)

        # memory prefetch: persist indicator payload for reload visibility
        if task.get('_memoryPrefetch'):
            last_msg['_memoryPrefetch'] = task['_memoryPrefetch']

        # preferences-applied chip: persist so the chip survives reload
        if task.get('_preferencesApplied'):
            last_msg['_preferencesApplied'] = task['_preferencesApplied']

        # related-conversations chip: persist so the chip survives reload
        if task.get('_relatedConversations'):
            last_msg['_relatedConversations'] = task['_relatedConversations']

        # inbox-inject sidecars (swarm / peer / user-steer): persist so the
        # in-timeline inject chips survive reload. Underscore fields only — the
        # wire builders never read them (see _persist_inject_sidecars). Called
        # here for the normal path; the content-guard branch above already wrote
        # them (idempotent) so it could fall through to this DB write.
        _persist_inject_sidecars(task, last_msg)

        # preferences-learned: persist the "Noted: you prefer X" moment(s)
        if task.get('_preferencesLearned'):
            last_msg['_preferencesLearned'] = task['_preferencesLearned']

        # git-shim: persist the round commit sha for redo/diff references.
        if task.get('gitSha'):
            last_msg['_gitSha'] = task['gitSha']

        # ── Same-task twin FOLD at settle time (one task = one row) ──
        # A frontend reconnect placeholder pushed back mid-task (same _taskId,
        # the typed error envelope — the conv mscns5i0fcofgl two-bubble bug)
        # must converge into this task's OWN row in the SAME write, not on
        # the next GET heal. Runs BEFORE the settings block below so the
        # sidebar tail facts come from the CONVERGED tail, and before the
        # CAS loop's fragment mark (a folded keeper's fresh finishReason
        # must not be mislabelled 'aborted'). Cache-prefix-honouring like
        # the GET/PUT seams (same-task rows are tail rows by construction,
        # so the guard never bites here in practice). Idempotent — the
        # per-attempt call inside the CAS loop covers graft-introduced
        # twins after a CAS miss.
        if len(messages) >= 2:
            from lib.conversations.reconcile import fold_duplicate_task_twins
            _fold_prefix_n = 0
            try:
                from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
                _fold_prefix_n = get_cache_prefix_count(conv_id)
            except Exception as _fp_e:
                logger.debug('%s conv=%s get_cache_prefix_count failed: %s',
                             pfx, conv_id[:8], _fp_e)
            messages, _twins_folded = fold_duplicate_task_twins(messages, _fold_prefix_n)
            if _twins_folded:
                # The fold replaces a filled keeper with a dict COPY —
                # re-resolve our slot so every later consumer (settings
                # tail facts, the CAS-miss graft, task['_committedMsg'])
                # sees the row that is actually committed.
                if last_msg.get('_msgId'):
                    from lib.tasks_pkg.manager._events import find_message_by_id as _fmid_fold
                    _fi, _fm = _fmid_fold(messages, last_msg['_msgId'])
                    if _fm is not None:
                        last_msg = _fm
                logger.info('%s conv=%s Folded %d same-task twin assistant row(s) '
                            'at terminal sync (one task = one row; terminal '
                            'verdict moved onto the surviving row)',
                            pfx, conv_id, _twins_folded)

        # Serialize and write back — json_dumps_pg strips null bytes from
        # raw data AND removes \u0000 escapes from the JSON text.
        messages_json = json_dumps_pg(messages)
        now_ms = int(time.time() * 1000)

        # ── Also clear activeTaskId from settings so subsequent reloads
        #    don't re-trigger Case B recovery for an already-synced task ──
        settings_row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()
        settings_json = None
        if settings_row:
            try:
                s = json.loads(settings_row[0] or '{}')
                changed = False
                if s.get('activeTaskId'):
                    s['activeTaskId'] = None
                    changed = True
                # ★ Update lastMsgRole/lastMsgTimestamp so metadata shells
                # reflect the new last message (assistant, not user) for Case E
                if messages:
                    lm = messages[-1]
                    # Raw settled-turn facts for the sidebar's stripped-messages
                    # path (classification stays frontend-side). Recompute every
                    # sync so a re-run overwrites a prior interrupted verdict.
                    _lfr = lm.get('finishReason')
                    _lerr = bool(lm.get('error'))
                    _lout = bool((lm.get('content') or '') or (lm.get('thinking') or '')
                                 or (lm.get('toolRounds') or []) or lm.get('_igResults'))
                    if (s.get('lastMsgRole') != lm.get('role')
                            or s.get('lastMsgTimestamp') != lm.get('timestamp')
                            or s.get('lastFinishReason') != _lfr
                            or s.get('lastMsgError') != _lerr
                            or s.get('lastMsgHasOutput') != _lout):
                        s['lastMsgRole'] = lm.get('role')
                        s['lastMsgTimestamp'] = lm.get('timestamp')
                        s['lastFinishReason'] = _lfr
                        s['lastMsgError'] = _lerr
                        s['lastMsgHasOutput'] = _lout
                        changed = True
                if changed:
                    settings_json = json.dumps(s, ensure_ascii=False)
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning('[Task] Failed to parse/clear activeTaskId from settings for conv=%s: %s', conv_id, e, exc_info=True)

        # ── Optimistic lock: only update if no concurrent write occurred ──
        # Use updated_at as CAS guard to prevent overwriting a fresher
        # frontend sync.  If the row was updated since our SELECT, our
        # read-modify-write would clobber the frontend's data.
        # ── Also update search_text for fast conversation search ──
        from lib.conversations import build_search_text
        # ── Bounded CAS-retry on the terminal write ──────────────────────────
        # The terminal sync is the ONLY writer of the final answer when the SSE
        # `done` frame never reaches the browser (poor network → finishStream
        # never fires). A SINGLE-SHOT CAS that missed used to DROP the final
        # content, assuming "frontend synced first (safe)". A flaky network
        # violates that: a concurrent partial-checkpoint / meta-cache write can
        # bump ``updated_at`` while the frontend did NOT win, silently losing
        # the answer (it survives only in task_results). On a miss we re-read
        # the fresh row: if it already holds >= our content the frontend
        # genuinely won (skip — never shrink); otherwise we graft our assembled
        # assistant onto the fresh tail and re-CAS (up to 3×). Mirrors the
        # bounded loop _sync_partial_to_conversation already has.
        #     Raw db.execute()+commit (not db_execute_with_retry) because the
        #     retry helper masks rowcount, and we need it to detect CAS-miss.
        MAX_TERMINAL_CAS = 3
        _cas_succeeded = False
        search_text = build_search_text(messages)
        for _cas_attempt in range(MAX_TERMINAL_CAS):
            # Same-task twin fold, per attempt (idempotent), BEFORE the
            # fragment mark — mirrors reconcile_conversation_messages' pass
            # order: a folded keeper's freshly-gained finishReason must not
            # be mislabelled 'aborted' by the mark. A CAS-miss graft REPLACES
            # `messages` with the fresh row, which can carry the twin again —
            # the pre-loop call above already folded the common case, so this
            # usually no-ops.
            if len(messages) >= 2:
                from lib.conversations.reconcile import fold_duplicate_task_twins
                try:
                    from lib.tasks_pkg.cache_tracking import get_cache_prefix_count
                    _fold_prefix_n = get_cache_prefix_count(conv_id)
                except Exception as _fp_e2:
                    logger.debug('%s conv=%s get_cache_prefix_count failed (CAS loop): %s',
                                 pfx, conv_id[:8], _fp_e2)
                    _fold_prefix_n = 0
                messages, _twins_folded = fold_duplicate_task_twins(messages, _fold_prefix_n)
                if _twins_folded:
                    if last_msg.get('_msgId'):
                        from lib.tasks_pkg.manager._events import find_message_by_id as _fmid_fold2
                        _fi2, _fm2 = _fmid_fold2(messages, last_msg['_msgId'])
                        if _fm2 is not None:
                            last_msg = _fm2
                    logger.info('%s conv=%s Folded %d same-task twin row(s) on CAS '
                                'attempt %d', pfx, conv_id, _twins_folded,
                                _cas_attempt + 1)
            # Stop→Regenerate self-heal: a superseded content-bearing fragment
            # (no terminal reason) adjacent to this settle's answer must be
            # stamped finishReason='aborted' here — not only on the next GET —
            # or the aborted partial renders in completed chrome. Runs per
            # attempt because a CAS-miss graft REPLACES `messages` with the
            # fresh row, which needs the same mark. Mark-only (no delete /
            # reindex), so it is cache-prefix-neutral.
            if len(messages) >= 2:
                from lib.conversations.reconcile import (
                    mark_superseded_incomplete_fragments,
                )
                messages, _frags_marked = mark_superseded_incomplete_fragments(messages)
                if _frags_marked:
                    logger.info('%s conv=%s Marked %d superseded incomplete fragment(s) '
                                'finishReason=aborted at terminal sync',
                                pfx, conv_id, _frags_marked)
            messages_json = json_dumps_pg(messages)
            search_text = build_search_text(messages)
            now_ms = int(time.time() * 1000)
            # CAS on rev (NOT updated_at): the trigger bumps rev, so a concurrent
            # translate/checkpoint write between our SELECT and this UPDATE
            # advances rev and we MISS here (re-read below) instead of silently
            # clobbering it. ``rev`` NEVER appears in SET — the trigger owns it.
            if settings_json:
                cur = db.execute(
                    '''UPDATE conversations
                       SET messages=?, updated_at=?, msg_count=?, settings=?, search_text=?
                       WHERE id=? AND user_id=1 AND rev=?''',
                    (messages_json, now_ms, len(messages), settings_json, search_text, conv_id, _row_rev)
                )
            else:
                cur = db.execute(
                    '''UPDATE conversations
                       SET messages=?, updated_at=?, msg_count=?, search_text=?
                       WHERE id=? AND user_id=1 AND rev=?''',
                    (messages_json, now_ms, len(messages), search_text, conv_id, _row_rev)
                )
            db.commit()
            _cas_succeeded = (getattr(cur, 'rowcount', 0) or 0) > 0
            if _cas_succeeded:
                break
            # CAS miss — re-read the fresh row to decide retry vs frontend-won.
            _fresh = db.execute(
                'SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)).fetchone()
            if not _fresh:
                break
            _fresh_updated_at = _fresh['updated_at']
            _fresh_rev = _fresh['rev']
            try:
                _fresh_messages = json.loads(_fresh[0] or '[]')
            except (json.JSONDecodeError, TypeError):
                logger.warning('%s conv=%s CAS-retry re-read parse failed — abandoning retry',
                               pfx, conv_id)
                break
            if not _fresh_messages:
                break
            _fresh_tail = _fresh_messages[-1]
            # ★ PROVENANCE (pt_bf93496e98b9441e): a fresh tail carrying a
            #   DIFFERENT task's _taskId is that task's bubble — it is
            #   neither a "frontend won" for THIS task nor a legal graft
            #   target (grafting would graft this task's verdict onto the
            #   other task's tombstone).
            _fresh_tail_foreign = bool(_fresh_tail.get('_taskId')) \
                and _fresh_tail.get('_taskId') != task['id']
            if (_fresh_tail.get('role') == 'assistant' and not _fresh_tail_foreign
                    and len(_fresh_tail.get('content') or '') >= new_content_len
                    and len(_fresh_tail.get('thinking') or '') >= new_thinking_len
                    and not _is_floor_retry_residue(task, _fresh_tail)):
                # Genuine frontend win landed between our read and our write:
                # a fuller answer is already persisted. Do NOT shrink it — the
                # historical "safe skip", now proven rather than assumed.
                # A byte-match against this task's discarded FloorRetry attempt
                # is NOT a frontend win (the residue exemption): fall through
                # to the graft + retry so the final answer overwrites it.
                logger.info('%s conv=%s terminal CAS miss %d/%d — fresh row holds '
                            '>= our content (frontend genuinely won); not shrinking',
                            pfx, conv_id, _cas_attempt + 1, MAX_TERMINAL_CAS)
                break
            # Flaky-network case: updated_at moved but content did NOT win —
            # graft our assembled assistant onto OUR slot in the fresh row and
            # retry. MERGE (not whole-dict replace): copy only the backend-OWNED
            # terminal fields so a translation (translatedContent /
            # segments[].translatedText) committed onto the fresh tail in our
            # read→write window survives (RENDER_CONTRACT Phase 4 §2.2).
            _graft = None
            if _fresh_tail.get('role') == 'assistant' and not _fresh_tail_foreign:
                _graft = _fresh_tail
            elif _amid:
                from lib.tasks_pkg.manager._events import find_message_by_id as _fmid
                _gi, _gm = _fmid(_fresh_messages, _amid)
                if _gm is not None and _gm.get('role') == 'assistant':
                    _graft = _gm
            if _graft is None:
                _graft = _find_own_assistant_slot(_fresh_messages, task['id'])
            if _graft is not None:
                _merge_terminal_fields(_graft, last_msg)
            elif task.get('_abort_reason') == 'stuck_no_progress':
                # A reaped task whose own slot vanished between retries must
                # NOT append its error bubble onto a moved-on tail (the same
                # wrong-turn defect the first pass guards).
                logger.warning('%s conv=%s reaped task found no own slot in the '
                               'fresh row — not grafting/appending onto a '
                               'foreign tail', pfx, conv_id)
                break
            else:
                _fresh_messages.append(last_msg)
            messages = _fresh_messages
            _row_updated_at = _fresh_updated_at
            _row_rev = _fresh_rev
            logger.info('%s conv=%s terminal CAS miss %d/%d — re-read fresh row '
                        'and re-applying the final answer',
                        pfx, conv_id, _cas_attempt + 1, MAX_TERMINAL_CAS)
        # FTS index is only updated when CAS succeeds.  Updating FTS for a
        # write we lost would leave search hits pointing at content we
        # never persisted — search results would surface dead data.
        if _cas_succeeded:
            from lib.conversations import update_conversation_fts
            update_conversation_fts(db, conv_id, search_text)
            # Phase 5 dual-write (flag-gated, inert when off): terminal
            # append/graft onto the tail — incremental tail mirror.
            from lib.database.messages_rows import mirror_write_and_commit
            mirror_write_and_commit(db, conv_id, messages,
                                    now_ms=int(time.time() * 1000))
            # ── Phase-1 parity stamp (the never-landed write) ──
            # Freeze the EXACT terminal assistant dict we just committed to
            # conversations.messages so orchestrator.py can ship it verbatim as
            # the done event's `committedMessage`. The frontend then projects it
            # VERBATIM (single source of truth for the settled bubble) instead
            # of reconstructing from its transient stream buffer. Shallow copy
            # freezes the row AS WRITTEN. Stamped ONLY on CAS success: skip /
            # CAS-miss / crash paths intentionally leave it UNSET → no
            # committedMessage rides the event → the client keeps its transient
            # buffer (the offline fallback, still guarded by keep-longer).
            task['_committedMsg'] = dict(last_msg)
        if not _cas_succeeded:
            logger.info('%s conv=%s Optimistic lock missed — row was updated concurrently '
                       '(expected_updated_at=%s). '
                       'Frontend likely synced first; backend sync skipped (safe).',
                       pfx, conv_id, _row_updated_at)
        else:
            logger.info('%s conv=%s ✅ Synced result to conversation — content=%dchars thinking=%dchars '
                        'msgs=%d (was: content=%d thinking=%d)',
                        pfx, conv_id, new_content_len, new_thinking_len, len(messages),
                        existing_content_len, existing_thinking_len)

        # ── Invalidate meta cache AND push the change to clients so a sibling
        #    device reconciles the completed answer without a manual refresh.
        #    Carry the post-write rev (bumped by the messages-change trigger)
        #    so the client rev-gate refetches this conv's body exactly once. ──
        try:
            from lib.conversations import notify_conv_changed
            from lib.tasks_pkg.manager._registry import task_user_id
            _mgr_rev_row = db.execute(
                'SELECT rev FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)).fetchone()
            _mgr_rev = _mgr_rev_row[0] if _mgr_rev_row else None
            notify_conv_changed(conv_id, rev=_mgr_rev, user_id=task_user_id(task))
        except Exception as e:
            logger.debug('[Manager] conv-changed notify skipped: %s', e)

        # ── Auto-translate: server-side safety net for translation ──
        # Ensures translation happens even if the frontend is offline / switched away.
        # Independent of the row CAS above: _maybe_auto_translate_assistant
        # re-reads fresh DB state, dedups against existing translatedContent
        # and running translate tasks, and commits via a targeted by-id write
        # — it never does the full-row write the CAS guards.  Gating it on
        # _cas_succeeded meant that when a live frontend won the conversation
        # row-write race (the active-view case), nobody translated until the
        # user switched conversations or clicked translate.  Fire regardless.
        if content and not error:
            try:
                _maybe_auto_translate_assistant(conv_id, content, len(messages) - 1, db, task=task)
            except Exception as te:
                logger.warning('%s conv=%s Auto-translate trigger failed (non-fatal): %s',
                               pfx, conv_id, te)
        else:
            # No content / errored task: the safety net is skipped, so if the
            # tool loop spun up an incremental accumulator (per-round segments
            # translated in the background) it would otherwise dangle until its
            # 300s idle-timeout and log a misleading "finalize never called"
            # warning. Tear it down explicitly. No-op when no accumulator
            # exists (autoTranslate off — the common case).
            try:
                from lib.translate import cancel_incremental
                if cancel_incremental(task):
                    logger.info('%s conv=%s cancelled incremental accumulator '
                                '(task ended with no content / error=%s)',
                                pfx, conv_id, bool(error))
            except Exception as ce:
                logger.debug('%s conv=%s cancel_incremental failed: %s', pfx, conv_id, ce)

    except Exception as e:
        from lib.database import log_db_finalize_error
        log_db_finalize_error(logger, 'error', e,
                              f'{pfx} conv={conv_id} ❌ Failed to sync result to conversation')


def checkpoint_task_partial(task, force=False):
    """Persist the current in-flight task state to DB so it survives a server crash.

    Called after each tool-execution round in the orchestrator loop.
    Writes to both task_results (for poll recovery) and the conversation
    (for direct page-reload recovery).

    Uses status='running' so the frontend can distinguish a partial checkpoint
    from a final result (status='done'|'error').

    ``force`` bypasses the "nothing meaningful yet" early return below. That
    guard assumes prose is the only thing worth persisting, which is false for
    a TOOL-ONLY round: a turn whose first act is a long ``run_command`` has
    empty content AND empty thinking, so the guard would drop the very write
    that makes the running round (and its deadline) recoverable after a
    conversation switch. Callers pass force=True only when the round itself
    carries state worth durably storing.
    """
    content_len = len(task.get('content') or '')
    thinking_len = len(task.get('thinking') or '')
    task_id_short = task['id'][:8]
    conv_id = task.get('convId', '')

    # Don't bother checkpointing if there's nothing meaningful yet.
    # `force` (or a still-in-flight tool round) makes a tool-only turn
    # checkpointable — see the docstring and `_has_inflight_round`.
    if content_len == 0 and thinking_len == 0 and not force and not _has_inflight_round(task):
        return

    # ★ Merge checkpoint toolRounds for continue flow
    _merged_tr = _merge_tool_rounds(task)

    # ★ Segment-timeline SoT (epic pt_cb8f98b0cb9b47fb): assemble on the CRASH
    #   path too, so a turn cut off mid-prose leaves a persisted resumable tail.
    #   persist_task_result assembles at clean finalization; without the same
    #   block here a server crash / transport drop mid-answer would have NO
    #   segment record — only the legacy channels — and the Continue prefill
    #   path (resume_prefill_from_segments) would find nothing to resume. The
    #   resumable flag is NOT stamped here (status='running', no finishReason
    #   yet); recover_stale_tasks_on_startup stamps finishReason='interrupted'
    #   on the message, and the continue read passes it as the finish_reason
    #   override. Best-effort: a segment failure must NEVER break checkpointing.
    try:
        from lib.tasks_pkg.segments import assemble_segments
        task['segments'] = assemble_segments(task, merged=_merged_tr)
    except Exception as _seg_e:
        logger.warning('[Checkpoint %s] segment assembly failed (non-fatal): %s',
                       task_id_short, _seg_e, exc_info=True)

    try:
        # See _tool_rounds_have_dedicated_home: skip the duplicate blob for
        # tasks whose toolRounds are checkpointed into conversations.messages
        # by _sync_partial_to_conversation on the same cadence.
        tr_json = None if _tool_rounds_have_dedicated_home(task) else json.dumps(_merged_tr, ensure_ascii=False)
        meta = {}
        if task.get('model'): meta['model'] = task['model']
        if task.get('preset'): meta['preset'] = task['preset']
        if task.get('thinkingDepth'): meta['thinkingDepth'] = task['thinkingDepth']
        meta_json = json.dumps(meta, ensure_ascii=False) if meta else None
        # ★ Thin segments for the checkpoint row (same discipline as
        #   persist_task_result step 2). Rehydrated on read via the co-persisted
        #   tool_rounds. Best-effort — never break the checkpoint write.
        _cp_segments_json = None
        try:
            _cp_segs = task.get('segments')
            if _cp_segs:
                from lib.tasks_pkg.segments import segments_to_json
                _cp_segments_json = json.dumps(segments_to_json(_cp_segs), ensure_ascii=False)
        except Exception as _sj_e:
            logger.warning('[Checkpoint %s] segments serialize failed (non-fatal): %s',
                           task_id_short, _sj_e, exc_info=True)
        # Error envelope is JSON-serialised at the wire — see persist_task_result.
        _cp_error_json = _err_to_json(task['error']) if task.get('error') is not None else None
        _upsert_task_row(task, conv_id, content=task.get('content') or '',
                         thinking=task.get('thinking') or '', status='running',
                         error_json=_cp_error_json, tr_json=tr_json, meta_json=meta_json,
                         segments_json=_cp_segments_json)
        logger.debug('[Checkpoint %s] conv=%s Saved partial: content=%dchars thinking=%dchars',
                     task_id_short, conv_id, content_len, thinking_len)
    except Exception as e:
        from lib.database import log_db_finalize_error
        log_db_finalize_error(logger, 'warning', e,
                              f'[Checkpoint {task_id_short}] conv={conv_id} Failed to checkpoint')
        # ★ P0 observability: when the checkpoint row can't be written (classic
        #   cause: connection pool exhausted), the in-flight terminal metadata
        #   would otherwise be invisible in the logs. If this task already has
        #   a finish verdict computed in memory, surface it so a failed-to-
        #   persist turn is diagnosable from error.log alone.
        if task.get('finishReason'):
            logger.warning('[Checkpoint %s] conv=%s ⚠️ CHECKPOINT NOT PERSISTED — %s',
                           task_id_short, conv_id,
                           terminal_state_log_summary(task, persisted=False))

    # Also sync partial content into the conversation's messages in DB
    # For endpoint mode, skip — endpoint.py handles multi-turn sync
    if not task.get('endpoint_mode'):
        _sync_partial_to_conversation(task)

    # ── CROSS-TALK DETECTION: log when multiple tasks are being checkpointed concurrently ──
    with tasks_lock:
        running_tasks = [(tid[:8], t.get('convId', '')[:8])
                         for tid, t in tasks.items()
                         if t.get('status') == 'running' and tid != task['id']]
    if running_tasks:
        logger.debug(
            '[Checkpoint %s] conv=%s ⚠️ %d other running task(s): %s — '
            'concurrent streams increase cross-talk risk on frontend',
            task_id_short, conv_id, len(running_tasks),
            running_tasks
        )


def _has_inflight_round(task) -> bool:
    """True when the task holds at least one tool round that is STILL RUNNING.

    The complement of ``lib.conversations.reconcile.has_real_round``, which
    answers "is there a SETTLED round?". Both are needed and they must stay
    distinct: a checkpoint has to preserve a round that has NOT settled yet
    (that is the whole point of a checkpoint), while the ghost sweep must keep
    treating an unsettled bodyless bubble as clutter.

    Live statuses are the ones the executor assigns before a verdict exists:
    the announce state, the execution state, and the three WAITING states
    (approval / human guidance / stdin) — a command blocked on a human gate is
    emphatically still in flight, and is exactly the case that runs longest.
    """
    rounds = (task.get('_checkpointToolRounds') or []) + (task.get('toolRounds') or [])
    for r in rounds:
        if isinstance(r, dict) and r.get('status') in (
                'searching', 'executing', 'pending_approval',
                'awaiting_human', 'awaiting_stdin'):
            return True
    return False


def _sync_partial_to_conversation(task):
    """Write partial streaming state into the conversation's last assistant message.

    Comprehensive checkpoint: writes content, thinking, toolRounds, and
    structural metadata (model, modifiedFileList, _memoryPrefetch,
    gitSha) so a page reload mid-stream reconstructs the same UI the user
    saw before the disconnect — without depending on the in-memory task
    object, the activeTaskId stash, or poll fallback.

    Terminal-only fields (finishReason, usage, toolSummary, cost) are withheld
    while the turn is mid-stream — INCLUDING the ~110-line window in
    ``orchestrator/_finalize.py`` where ``task['finishReason']`` is already
    stamped but ``task['status']`` is still ``'running'`` (the span holding the
    blocking ``_generate_tool_summary`` call). Persisting a verdict there marks
    a still-generating turn as settled, and the frontend answers that with a
    duplicate assistant bubble.

    They ARE carried once finalize is REALLY underway — the task reports a
    terminal status (``lib/chat/terminal_gate.is_terminal_status``, the same
    rule the poll + SSE snapshot transports use) or the
    ``_finalize_started_at`` latch is set — so a checkpoint that outlives a
    failed terminal persist still leaves a populated finish-bar instead of the
    empty "model name only" bar. When the verdict is carried, ``_taskId`` is
    written with it: a terminal field without its identity anchor is a row that
    cannot be recognised as its own completed turn. See the P1a block.
    """
    conv_id = task.get('convId', '')
    content = task.get('content') or ''
    thinking = task.get('thinking') or ''
    # ── Empty-turn skip ──
    # Prose is NOT the only thing worth checkpointing. A turn whose first act is
    # a long-running tool (a `run_command` build/test) has empty content AND
    # empty thinking for its whole duration, yet its round carries live state —
    # status, tStart, the execution deadline — that a mid-command reload or
    # conversation switch must be able to project. For a conv-backed task this
    # message row is the ONLY durable home for that round (task_results leaves
    # tool_rounds NULL by design — see _tool_rounds_have_dedicated_home), so
    # returning here dropped it on the floor entirely.
    #
    # NOTE the predicate is `has_inflight_round`, NOT the project-wide
    # `has_real_round`: the latter means "SETTLED round" (status=='done' /
    # results present) and returns False for the still-running round we are
    # trying to preserve — using it here would be a vacuous guard that changes
    # nothing. `has_real_round` must KEEP that strict meaning: the ghost sweep
    # and tail classifier rely on it to decide a bodyless bubble is clutter.
    if not content and not thinking and not _has_inflight_round(task):
        return

    # ── CARRIER GUARD (must mirror the terminal sync exactly) ──
    # A carrier runs no user-visible turn, so it must never materialise a row
    # in conversations.messages. The freshness guard below cannot substitute
    # for this: the autopilot VU sub-task records ITSELF as the conversation's
    # latest task (pt_8dc03017 HB-1), so it passes freshness by construction.
    # Without this, its first streaming delta was appended as a headless
    # assistant message that could never be settled — the terminal sync
    # (correctly) rejects carriers, so the row stayed frozen at that delta with
    # a finish bar that could never complete.
    if is_carrier_task(task):
        logger.debug('[Checkpoint] conv=%s Carrier task %s — skipping partial '
                     'conv sync by design', conv_id[:8], task['id'][:8])
        return

    # ── FRESHNESS GUARD: reject checkpoint writes from stale tasks ──
    if conv_id:
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        if latest and latest != task['id']:
            logger.debug('[Checkpoint] conv=%s Stale task %s — skipping partial sync (latest=%s)',
                         conv_id[:8], task['id'][:8], latest[:8])
            return

    # ★ Merge checkpoint toolRounds for continue flow
    tool_rounds = _merge_tool_rounds(task)

    # Bounded CAS retry — under contention with the frontend or other writers
    # we re-read and try again rather than silently dropping the checkpoint.
    MAX_CAS = 3
    last_err = None
    for attempt in range(MAX_CAS):
        try:
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT messages, updated_at, rev FROM conversations WHERE id=? AND user_id=1',
                (conv_id,)
            ).fetchone()
            if not row:
                return

            try:
                messages = json.loads(row[0] or '[]')
            except (json.JSONDecodeError, TypeError) as exc:
                logger.warning('[Manager] Unparseable conversation messages for conv=%s: %s', conv_id, exc)
                return

            if not messages:
                return

            cur_rev = row[2]  # Phase 4 W-partial: CAS on rev (loop re-reads each attempt)

            # ── ID-FIRST location (mirrors the terminal sync) ──
            # A queued next-turn user message may sit as a trailing pending row
            # below this task's assistant slot; locate our slot by its stable
            # `_assistantMsgId` so a blind tail read doesn't spawn a second
            # assistant. Fall back to the tail when the id is absent/unmatched.
            _amid = task.get('_assistantMsgId')
            last_msg = None
            if _amid:
                from lib.tasks_pkg.manager._events import find_message_by_id
                _idx, _by_id = find_message_by_id(messages, _amid)
                if _by_id is not None and _by_id.get('role') == 'assistant':
                    last_msg = _by_id
            if last_msg is None:
                last_msg = messages[-1]
            if last_msg.get('role') != 'assistant':
                # Do NOT materialize a brand-new trailing assistant row from a
                # thinking-only first checkpoint.  A turn that streams a stray
                # reasoning fragment and then dies (server crash) would otherwise
                # leave an empty husk ({content:'', thinking:'I'}) with no
                # finishReason — which renders as a blank bubble with no finish
                # tag and slips past the frontend ghost-cleanup (the stray
                # `thinking` defeats its `!thinking` guard).  Wait until there's
                # real CONTENT before appending the row; thinking accumulated by
                # then is written alongside it, so nothing is dropped.  Updating
                # an EXISTING assistant row (frontend placeholder) is unaffected.
                if not content:
                    logger.debug('[Checkpoint] conv=%s deferring trailing assistant '
                                 'row — thinking-only first checkpoint (no content yet)',
                                 conv_id[:8])
                    return
                last_msg = _new_assistant_slot(task)
                messages.append(last_msg)

            existing_content_len = len(last_msg.get('content') or '')
            existing_thinking_len = len(last_msg.get('thinking') or '')

            # Track whether anything actually changed; we skip the UPDATE if
            # content didn't grow AND no new structural data is available.
            mutated = False

            # ── Coalesce sub-threshold content/thinking growth ──
            # The O(conv-size) messages-JSON rewrite is the expensive half of a
            # partial checkpoint. Grown text is always applied to last_msg
            # IN-MEMORY (free), but a tiny content/thinking delta on its own is
            # NOT worth the whole-JSON write: we withhold the WRITE (not the
            # data) until the delta crosses CHECKPOINT_MIN_DELTA_CHARS. Because
            # the delta is measured against the (unwritten) DB row, growth is
            # cumulative across skips and flushes as soon as it crosses the
            # threshold. If any OTHER change (tool_rounds / metadata) triggers
            # the write anyway, the already-applied fresh text rides along for
            # free. The cheap task_results blob is written EVERY checkpoint and
            # the terminal sync always writes the full final content, so nothing
            # is lost — only the mid-stream messages mirror lags by < threshold.
            _content_grew = bool(content and len(content) > existing_content_len)
            _thinking_grew = bool(thinking and len(thinking) > existing_thinking_len)
            # ★ Convergence, not just growth (FloorRetry-residue root fix): the
            #   authoritative task text can legitimately SHRINK mid-task — a
            #   FloorRetry adoption discards the first attempt AFTER its deltas
            #   were already mirrored here by earlier checkpoints, and the
            #   per-round reset restarts prose per round. A grew-only guard
            #   pinned the longest-ever attempt in the row forever (later
            #   rounds — including the final answer — never exceeded it), which
            #   then poisoned the terminal content guard into "frontend
            #   genuinely won" (live conv mrxij7q34xm070: the 4344-char
            #   discarded R3 draft out-ranked the 3751-char R7 answer).
            #   Write whenever the value differs (non-empty only — an empty
            #   post-reset accumulator must never wipe the mirror); a SHRINK is
            #   semantically load-bearing, so it bypasses delta coalescing.
            _content_changed = bool(content and content != (last_msg.get('content') or ''))
            _thinking_changed = bool(thinking and thinking != (last_msg.get('thinking') or ''))
            _content_shrank = _content_changed and not _content_grew
            _thinking_shrank = _thinking_changed and not _thinking_grew
            _pending_delta = ((len(content) - existing_content_len if _content_grew else 0)
                              + (len(thinking) - existing_thinking_len if _thinking_grew else 0))
            _terminal = bool(task.get('finishReason')
                             or task.get('status') in ('done', 'error', 'aborted'))
            # The text delta alone justifies a write only when it is big enough
            # (or coalescing is disabled, or the task is terminal, or it is a
            # convergence shrink as above).
            _text_write_worthy = (_pending_delta > 0 and (
                CHECKPOINT_MIN_DELTA_CHARS == 0
                or _terminal
                or _pending_delta >= CHECKPOINT_MIN_DELTA_CHARS
            )) or _content_shrank or _thinking_shrank

            if _content_changed:
                last_msg['content'] = content
            if _thinking_changed:
                last_msg['thinking'] = thinking
            if _text_write_worthy:
                mutated = True

            if tool_rounds:
                _existing_tr = last_msg.get('toolRounds') or []
                # Replace if we have more rounds OR existing rounds lack toolContent
                # (frontend race may have synced an earlier tool-result without it).
                _existing_done_have_tc = all(
                    r.get('toolContent') for r in _existing_tr if r.get('status') == 'done'
                )
                _new_done_have_tc = any(
                    r.get('toolContent') for r in tool_rounds if r.get('status') == 'done'
                )
                if (len(tool_rounds) > len(_existing_tr)
                        or (not _existing_done_have_tc and _new_done_have_tc)):
                    last_msg['toolRounds'] = tool_rounds
                    mutated = True

            # Structural metadata that is meaningful BEFORE final completion.
            # Backend is authoritative for these; only fill if frontend hasn't.
            for src_key, dst_key in (
                ('model', 'model'),
                ('provider_id', 'provider_id'),
                ('preset', 'preset'),
                ('modifiedFiles', 'modifiedFiles'),
                ('modifiedFileList', 'modifiedFileList'),
                ('apiRounds', 'apiRounds'),
                ('_memoryPrefetch', '_memoryPrefetch'),
                ('_preferencesApplied', '_preferencesApplied'),
                ('_relatedConversations', '_relatedConversations'),
                ('_preferencesLearned', '_preferencesLearned'),
                # inbox-inject sidecars — persist mid-stream too so a reload
                # BEFORE the terminal sync still shows the inject chips.
                ('_inboxInjects', '_inboxInjects'),
                ('_peerInjects', '_peerInjects'),
                ('_userSteerInjects', '_userSteerInjects'),
            ):
                v = task.get(src_key)
                if v and not last_msg.get(dst_key):
                    last_msg[dst_key] = v
                    mutated = True
            git_sha = task.get('gitSha')
            if git_sha and not last_msg.get('_gitSha'):
                last_msg['_gitSha'] = git_sha
                mutated = True

            # ── P1a: carry the terminal finish verdict when finalize is REALLY underway ──
            # This sync normally withholds finishReason/usage/toolSummary
            # because they aren't final until the turn completes. But once the
            # orchestrator is genuinely finalizing, a checkpoint that fires
            # before — or INSTEAD of — the terminal persist (e.g. the terminal
            # persist's task_results write threw under pool exhaustion) is the
            # only durable trace of the verdict. Carrying it means a
            # crash-recovered partial renders a populated finish-bar
            # (finishReason + usage + cost) instead of the empty
            # "model name only" bar.
            #
            # ★ THE TRIGGER IS NOT `task.get('finishReason')` (2026-07-31).
            #   orchestrator/_finalize.py stamps task['finishReason'] at L843
            #   but flips task['status']='done' only at L954 — a 110-line
            #   window holding the BLOCKING _generate_tool_summary LLM call.
            #   The 5s checkpoint timer lands inside it routinely, so a
            #   presence-only trigger PERSISTED a terminal verdict onto a turn
            #   that was still generating. The frontend reads finishReason as
            #   "this turn settled": assistantTailIsPriorTurn then classifies
            #   the live bubble as a prior turn and connectToTask mints a
            #   SECOND assistant bubble — and because this row is in the DB, a
            #   reload reproduces it instead of clearing it.
            #
            #   `_finalize_started_at` is stamped at L953 — AFTER that window
            #   and one line before the terminal flip — so it admits exactly
            #   the "finalize is really underway" case this block was written
            #   for, and excludes the window that mints the contradiction.
            #   The terminal-status test comes from lib/chat/terminal_gate.py
            #   (the same rule the poll + SSE snapshot transports consume)
            #   rather than a third hand-written copy of the timing assumption.
            from lib.chat.terminal_gate import is_terminal_status as _is_terminal
            _verdict_is_final = bool(
                _is_terminal(task.get('status'))
                or task.get('_finalize_started_at'))
            if task.get('finishReason') and _verdict_is_final:
                if last_msg.get('finishReason') != task['finishReason']:
                    last_msg['finishReason'] = task['finishReason']
                    mutated = True
                # ★ ATOMIC IDENTITY ANCHOR. A terminal verdict without
                #   `_taskId` is a row that cannot be recognised as its OWN
                #   completed turn: the frontend reducer's identity arm needs
                #   _taskId to fire, so `{finishReason, no _taskId}` reads as
                #   somebody else's finished turn and mints a duplicate bubble.
                #   Historically ONLY the terminal sync
                #   (_sync_result_to_conversation) stamped it, so every row
                #   this block wrote was anchor-less. The two must land
                #   together or not at all.
                if task.get('id') and not last_msg.get('_taskId'):
                    last_msg['_taskId'] = task['id']
                    mutated = True
                if last_msg.get('finishReason') != task['finishReason']:
                    last_msg['finishReason'] = task['finishReason']
                    mutated = True
                _tu = task.get('usage')
                if _tu and not last_msg.get('usage'):
                    last_msg['usage'] = _tu
                    mutated = True
                _tts = task.get('toolSummary')
                if _tts and not last_msg.get('toolSummary'):
                    last_msg['toolSummary'] = _tts
                    mutated = True
                # Stamp the cost snapshot so the finish-bar's cost tag survives
                # even when the terminal persist never ran. Mirrors the terminal
                # sync's compute (usage + model + provider + pricing-at-now).
                if last_msg.get('usage') and not last_msg.get('cost'):
                    try:
                        from lib.cost import compute_cost as _compute_cost
                        _c = _compute_cost(
                            last_msg['usage'],
                            model_id=(last_msg.get('model') or task.get('model') or ''),
                            provider_id=(last_msg.get('provider_id')
                                         or task.get('provider_id') or None))
                        if _c:
                            last_msg['cost'] = _c
                            mutated = True
                    except Exception as _pce:
                        logger.debug('[Checkpoint] conv=%s partial cost stamp failed: %s',
                                     conv_id[:8] if conv_id else '?', _pce)

            # ★ Segment-timeline SoT (epic pt_cb8f98b0cb9b47fb): mirror the THIN
            #   segments onto the message dict so a page-reload / Continue after
            #   a mid-prose crash can read the resumable tail from the same
            #   conversations.messages row it reads everything else from.
            #   Segments are a PROJECTION of content/thinking/toolRounds, so we
            #   refresh last_msg in-memory every checkpoint — but the refresh
            #   RIDES ALONG on a write already warranted by a real change; it
            #   must NOT independently force the O(conv-size) write, or it would
            #   defeat the delta coalescing above (segments change on every
            #   token, exactly as content does). This is safe because
            #   task_results.segments IS written every checkpoint (the
            #   authoritative crash-recovery source that
            #   _rehydrate_segments_from_task_results reads) and the terminal
            #   persist writes final segments to messages — so the messages
            #   segment mirror lags by the same < threshold window as content
            #   and converges at completion. Best-effort — serialize failure
            #   just skips the mirror.
            _seg_val = task.get('segments')
            if _seg_val:
                try:
                    from lib.tasks_pkg.segments import segments_to_json
                    last_msg['segments'] = segments_to_json(_seg_val)
                except Exception as _msj_e:
                    logger.debug('[Checkpoint] conv=%s segments->msg serialize failed: %s',
                                 conv_id[:8], _msj_e)

            # Backfill stable IDs onto every message — pure write-side hook.
            if _assign_message_ids(messages):
                mutated = True

            if not mutated:
                return

            messages_json = json_dumps_pg(messages)
            now_ms = int(time.time() * 1000)
            # Partial checkpoints write ONLY reload-critical columns
            # (messages/updated_at/msg_count). search_text/FTS is a
            # whole-conversation derived index rebuilt on EVERY 5s checkpoint
            # for O(conv-size) waste — and the terminal _sync_result_to_conversation
            # always rebuilds it from the settled messages anyway, so a
            # mid-stream value is superseded on completion (and indexing a
            # not-yet-final tail points search hits at unsettled content).
            # Indexing is owned SOLELY by the terminal sync.
            cur = db.execute(
                'UPDATE conversations SET messages=?, updated_at=?, msg_count=? '
                'WHERE id=? AND user_id=1 AND rev=?',
                (messages_json, now_ms, len(messages), conv_id, cur_rev)
            )
            db.commit()
            rowcount = getattr(cur, 'rowcount', None)
            if rowcount == 0:
                # CAS miss — retry with a fresh read.
                logger.debug('[Checkpoint] conv=%s CAS miss attempt %d/%d — re-reading',
                             conv_id[:8], attempt + 1, MAX_CAS)
                time.sleep(0.02 * (attempt + 1))
                continue
            # Phase 5 dual-write (flag-gated, inert when off): checkpoint
            # mutates/appends the tail — incremental tail mirror.
            from lib.database.messages_rows import mirror_write_and_commit
            mirror_write_and_commit(db, conv_id, messages, now_ms=now_ms)
            logger.debug('[Checkpoint] conv=%s Synced partial: content=%d→%d thinking=%d→%d tools=%d',
                         conv_id, existing_content_len, len(content),
                         existing_thinking_len, len(thinking), len(tool_rounds or []))
            return
        except Exception as e:
            last_err = e
            logger.debug('[Checkpoint] conv=%s partial sync attempt %d/%d failed: %s',
                         conv_id, attempt + 1, MAX_CAS, e)
            time.sleep(0.05 * (attempt + 1))
    if last_err is not None:
        logger.debug('[Checkpoint] conv=%s gave up after %d attempts: %s',
                     conv_id, MAX_CAS, last_err)


