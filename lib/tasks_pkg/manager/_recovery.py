"""Startup stale-task recovery + deferred boot re-dispatch.

``recover_stale_tasks_on_startup`` marks crash-interrupted turns, merges their
recovered content back into conversations, and (optionally) hands off the
BILLED re-dispatch to ``run_deferred_boot_dispatch`` — which is gated OFF by
default (``_boot_auto_dispatch_enabled``).
"""

import json
import time

from lib.database import DOMAIN_CHAT, get_thread_db, json_dumps_pg
from lib.log import get_logger

logger = get_logger(__name__)


def recover_stale_tasks_on_startup(prev_shutdown=None, dispatch=True):
    """Clean up stale tasks from a previous server crash at startup time.

    ``dispatch`` (default True) controls whether the BILLED re-dispatch work
    (autopilot-resume + killed-turn recovery) runs INLINE here. Pass
    ``dispatch=False`` to run ONLY the synchronous DB cleanup (mark stale →
    interrupted, clear activeTaskId, merge content) and RETURN the deferred-
    dispatch descriptor so the caller can run the billed dispatch LATER, after
    the server begins serving. This is load-bearing: when this function runs on
    the startup event loop (``asyncio.run(_startup())``), a carrier spawned by
    the inline dispatch is scheduled ON that loop, and ``asyncio.run`` will not
    tear down until the carrier's whole run finishes — the 297s-boot incident.
    With ``dispatch=False`` the returned descriptor is handed to
    ``run_deferred_boot_dispatch`` from inside the serving loop instead.

    When the server crashes mid-generation:
    - task_results has entries with status='running' (from checkpoints)
    - conversations have activeTaskId set in settings (never cleared)

    This function:
    1. Marks all stale task_results as 'interrupted'
    2. Clears activeTaskId from all conversation settings
    3. Syncs interrupted task content into conversation messages

    This ensures the frontend doesn't need to do Case B recovery for every
    stale conversation on every page load, which dramatically speeds up boot.

    Args:
        prev_shutdown: Optional classification dict from
            ``lib.shutdown_marker.classify_previous_shutdown`` (or
            ``report_and_arm``). When the previous exit was UNCLEAN (an
            untrappable OS SIGKILL/OOM), each recovered turn is tagged
            ``interruptedReason='killed'`` — the auto-recover signal. A clean
            manual shutdown tags ``'manual'``. Absent → left unset (legacy).
    """
    # A previous exit that did NOT run any graceful path (verdict 'unclean')
    # is an OS kill → recovered turns are 'killed' (auto-recover). A clean
    # exit (manual button / signal drain / restart) is 'manual'. None → unset.
    _interrupt_reason = None
    _restart_storm = False
    if isinstance(prev_shutdown, dict):
        _verdict = prev_shutdown.get('verdict')
        if _verdict == 'unclean':
            _interrupt_reason = 'killed'
        elif _verdict == 'clean':
            _interrupt_reason = 'manual'
        _restart_storm = bool(prev_shutdown.get('restart_storm'))
    # Convs whose recovered tail we tagged 'killed' — the candidate set for
    # actionable auto-recovery (re-dispatch) after the recovery commit.
    _killed_conv_ids: list = []
    try:
        db = get_thread_db(DOMAIN_CHAT)

        # ── Step 1: Mark stale running tasks as interrupted ──
        stale_rows = db.execute(
            "SELECT task_id, conv_id, content, thinking FROM task_results WHERE status='running'"
        ).fetchall()

        # conv_id → task_id of the interrupted task carrying the MOST recovered
        # text.  task_results.conv_id is BACKEND-AUTHORITATIVE (create_task stamps
        # it), unlike the frontend-synced settings.activeTaskId which is null/stale
        # after a mid-stream crash (the PUT that persists it may never have landed).
        # Keying the merge off THIS map is what lets a crash-interrupted turn be
        # recovered into conversations.messages even when activeTaskId was lost —
        # the root fix for "Continue starts a brand-new agent from scratch".
        interrupted_task_by_conv = {}
        if stale_rows:
            _best_recovered_len = {}
            for row in stale_rows:
                tid = row['task_id']
                cid = row['conv_id'] or ''
                clen = len(row['content'] or '')
                tlen = len(row['thinking'] or '')
                logger.info('[Startup] Marking stale task %s (conv=%s) as interrupted: '
                            'content=%dchars thinking=%dchars',
                            tid[:8], cid[:8], clen, tlen)
                if cid:
                    _tot = clen + tlen
                    if _tot >= _best_recovered_len.get(cid, -1):
                        _best_recovered_len[cid] = _tot
                        interrupted_task_by_conv[cid] = tid
            db.execute("UPDATE task_results SET status='interrupted' WHERE status='running'")
            db.commit()
            logger.info('[Startup] Marked %d stale running task(s) as interrupted '
                        '(%d owning conv(s) identified via task_results.conv_id)',
                        len(stale_rows), len(interrupted_task_by_conv))

        # ── Step 2+3: Merge recovered content into conversations + clear stale
        #    activeTaskId.  Drive off TWO sources, UNIONed by conv_id:
        #      (a) conversations still carrying settings.activeTaskId — clear the
        #          now-dead pointer (json_extract is index-backed on PG via
        #          idx_conv_active_task and native on SQLite).
        #      (b) conversations that OWN an interrupted task via
        #          task_results.conv_id — AUTHORITATIVE; recovers the turn even
        #          when activeTaskId was never persisted (the mid-stream-crash
        #          case that used to orphan the interrupted content entirely).
        conv_rows = db.execute(
            "SELECT id, settings, messages FROM conversations WHERE user_id=1 "
            "AND json_extract(settings, '$.activeTaskId') IS NOT NULL"
        ).fetchall()
        conv_by_id = {r['id']: r for r in conv_rows}

        _missing_ids = [c for c in interrupted_task_by_conv if c not in conv_by_id]
        if _missing_ids:
            _ph = ','.join('?' for _ in _missing_ids)
            for r in db.execute(
                "SELECT id, settings, messages FROM conversations WHERE user_id=1 "
                f"AND id IN ({_ph})", tuple(_missing_ids)
            ).fetchall():
                conv_by_id[r['id']] = r
            logger.info('[Startup] %d interrupted-owning conv(s) had NO activeTaskId '
                        '(recovered via task_results.conv_id): %s',
                        len(_missing_ids), [c[:8] for c in _missing_ids])

        cleared = 0
        recovered_conv_ids: list = []
        for cid, crow in conv_by_id.items():
            try:
                settings = json.loads(crow['settings'] or '{}')
            except (json.JSONDecodeError, TypeError) as _e_audit:
                logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                continue
            atid = settings.get('activeTaskId')
            # Authoritative merge source: the interrupted task OWNED by this conv
            # (task_results.conv_id) wins over the frontend-synced activeTaskId
            # pointer — the interrupted task is the one that actually holds the
            # recovered content/thinking/toolRounds.
            merge_task_id = interrupted_task_by_conv.get(cid) or atid
            if not merge_task_id and not atid:
                continue
            # Clear the dead pointer if present.
            if atid:
                settings['activeTaskId'] = None
            settings_json = json.dumps(settings, ensure_ascii=False)

            # ── Merge interrupted task data into the conversation messages
            #    (the checkpoint may carry partial content the UI never saw) ──
            task_row = None
            if merge_task_id:
                task_row = db.execute(
                    "SELECT content, thinking, tool_rounds, metadata FROM task_results WHERE task_id=?",
                    (merge_task_id,)
                ).fetchone()

            messages_json = None
            if task_row:
                task_content = task_row['content'] or ''
                task_thinking = task_row['thinking'] or ''
                if task_content or task_thinking:
                    try:
                        messages = json.loads(crow['messages'] or '[]')
                        if messages:
                            last_msg = messages[-1]
                            if last_msg.get('role') == 'assistant':
                                # Only update if task has more content
                                existing_content = len(last_msg.get('content') or '')
                                existing_thinking = len(last_msg.get('thinking') or '')
                                if len(task_content) > existing_content:
                                    last_msg['content'] = task_content
                                if len(task_thinking) > existing_thinking:
                                    last_msg['thinking'] = task_thinking
                                if not last_msg.get('finishReason'):
                                    last_msg['finishReason'] = 'interrupted'
                                # Tag WHY it was interrupted (killed vs manual)
                                # so the frontend/recovery can auto-recover an
                                # OS kill but leave a deliberate stop alone.
                                if _interrupt_reason and not last_msg.get('interruptedReason'):
                                    last_msg['interruptedReason'] = _interrupt_reason
                                # Merge toolRounds from task
                                if task_row['tool_rounds']:
                                    try:
                                        tr = json.loads(task_row['tool_rounds'])
                                        if tr and len(tr) > len(last_msg.get('toolRounds') or []):
                                            last_msg['toolRounds'] = tr
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                # Merge metadata
                                if task_row['metadata']:
                                    try:
                                        meta = json.loads(task_row['metadata'])
                                        if meta.get('model') and not last_msg.get('model'):
                                            last_msg['model'] = meta['model']
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                messages_json = json_dumps_pg(messages)
                            elif last_msg.get('role') == 'user':
                                # Task started but no assistant msg was appended yet
                                new_msg = {
                                    'role': 'assistant',
                                    'content': task_content,
                                    'thinking': task_thinking,
                                    'finishReason': 'interrupted',
                                    'timestamp': int(time.time() * 1000),
                                }
                                if _interrupt_reason:
                                    new_msg['interruptedReason'] = _interrupt_reason
                                if task_row['tool_rounds']:
                                    try:
                                        new_msg['toolRounds'] = json.loads(task_row['tool_rounds'])
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                if task_row['metadata']:
                                    try:
                                        meta = json.loads(task_row['metadata'])
                                        if meta.get('model'):
                                            new_msg['model'] = meta['model']
                                    except (json.JSONDecodeError, TypeError) as _e_audit:
                                        logger.debug('[manager] recover_stale_tasks_on_startup caught %s: %s', type(_e_audit).__name__, _e_audit)
                                        pass
                                messages.append(new_msg)
                                messages_json = json_dumps_pg(messages)
                    except (json.JSONDecodeError, TypeError) as exc:
                        logger.warning('[Startup] Failed to parse messages for conv=%s: %s',
                                       cid[:8], exc)

            # ── Backend-authoritative ghost reconcile (Phase-3 startup wiring) ──
            #    Sweep buried empty ghosts + collapse superseded error husks +
            #    classify the trailing ghost, persisted in THIS SAME recovery
            #    commit so the frontend loads an already-clean list (no PUT to
            #    lose → resurrect impossible). Runs on the MERGED messages when a
            #    merge happened, else on the stored list — a buried ghost must be
            #    swept even when the interrupted task added no content (the
            #    messages_json-still-None case). cache_prefix_count=0: post-restart
            #    the prompt-cache state is empty so the whole list is mutable
            #    (see EDITABLE_TAIL_COUNT single-source invariant). On a real
            #    change, stamp settings._reconciledAt so the frontend Case-D
            #    defers, and fire notify_history_rewrite so a later
            #    detect_cache_break NAMES the backend edit (never silences it —
            #    that is notify_compaction's job, the opposite signal).
            try:
                _base_msgs = (json.loads(messages_json) if messages_json
                              else json.loads(crow['messages'] or '[]'))
            except (json.JSONDecodeError, TypeError):
                _base_msgs = []
            if _base_msgs:
                try:
                    from lib.conversations.reconcile import reconcile_conversation_messages
                    _cleaned, _rec_changed = reconcile_conversation_messages(_base_msgs, 0)
                except Exception as _rec_e:
                    logger.warning('[Startup] ghost reconcile failed for conv=%s: %s',
                                   cid[:8], _rec_e, exc_info=True)
                    _cleaned, _rec_changed = _base_msgs, False
                if _rec_changed:
                    messages_json = json_dumps_pg(_cleaned)
                    try:
                        _rs = json.loads(settings_json or '{}')
                    except (json.JSONDecodeError, TypeError):
                        _rs = {}
                    _rs['_reconciledAt'] = int(time.time() * 1000)
                    settings_json = json.dumps(_rs, ensure_ascii=False)
                    try:
                        from lib.tasks_pkg.cache_tracking import notify_history_rewrite
                        notify_history_rewrite(cid)
                    except Exception as _hr_e:
                        logger.debug('[Startup] notify_history_rewrite failed conv=%s: %s',
                                     cid[:8], _hr_e)
                    logger.info('[Startup] Ghost-reconciled conv=%s on recovery '
                                '(%d→%d msgs, _reconciledAt stamped)',
                                cid[:8], len(_base_msgs), len(_cleaned))

            now_ms = int(time.time() * 1000)
            # Stamp the settled-turn facts the sidebar reads without messages
            # (raw only — classification stays frontend-side). Derive from the
            # FINAL merged tail (finishReason='interrupted' is stamped above),
            # then fold into settings_json so it rides the same atomic UPDATE —
            # NOT a separate SELECT→mutate→UPDATE (that would clobber).
            try:
                _final_msgs = json.loads(messages_json) if messages_json else json.loads(crow['messages'] or '[]')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('[Manager] final_msgs JSON parse failed, using fallback: %s', e)
                _final_msgs = []
            if _final_msgs:
                _lm = _final_msgs[-1]
                try:
                    _s = json.loads(settings_json or '{}')
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[Manager] settings JSON parse failed, using fallback: %s', e)
                    _s = {}
                _s['lastMsgRole'] = _lm.get('role')
                _s['lastMsgTimestamp'] = _lm.get('timestamp')
                _s['lastFinishReason'] = _lm.get('finishReason')
                _s['lastMsgError'] = bool(_lm.get('error'))
                _s['lastMsgHasOutput'] = bool(
                    (_lm.get('content') or '') or (_lm.get('thinking') or '')
                    or (_lm.get('toolRounds') or []) or _lm.get('_igResults'))
                settings_json = json.dumps(_s, ensure_ascii=False)
            if messages_json:
                from lib.conversations import build_search_text
                messages_parsed = json.loads(messages_json)
                search_text = build_search_text(messages_parsed)
                db.execute(
                    "UPDATE conversations SET settings=?, messages=?, updated_at=?, "
                    "msg_count=?, search_text=? WHERE id=? AND user_id=1",
                    (settings_json, messages_json, now_ms,
                     len(messages_parsed), search_text, cid)
                )
            else:
                db.execute(
                    "UPDATE conversations SET settings=?, updated_at=? WHERE id=? AND user_id=1",
                    (settings_json, now_ms, cid)
                )
            cleared += 1
            recovered_conv_ids.append(cid)
            # Track convs whose recovered tail is a 'killed' turn — the input
            # set for actionable re-dispatch below. Derive from the FINAL tail
            # (_final_msgs computed just above), not _interrupt_reason alone: a
            # tail that already carried its own finishReason keeps it, so only
            # a genuinely 'killed'-tagged tail qualifies.
            if _final_msgs:
                _tail = _final_msgs[-1]
                if (isinstance(_tail, dict) and _tail.get('role') == 'assistant'
                        and _tail.get('interruptedReason') == 'killed'):
                    _killed_conv_ids.append(cid)
            logger.info('[Startup] Recovered conv=%s from task=%s '
                        '(activeTaskId_cleared=%s messages_updated=%s)',
                        cid[:8],
                        merge_task_id[:8] if merge_task_id else 'none',
                        bool(atid), bool(messages_json))

        if cleared:
            db.commit()
            logger.info('[Startup] Recovered %d conversation(s) (merged interrupted '
                        'content + cleared any dead activeTaskId)', cleared)

        total = len(stale_rows) + cleared
        if total:
            logger.info('[Startup] ✅ Stale task recovery complete: %d task(s) interrupted, '
                        '%d conv(s) recovered', len(stale_rows), cleared)
            # Invalidate meta cache so first frontend request gets clean data
            try:
                from lib.conversations import invalidate_meta_cache
                invalidate_meta_cache()
            except Exception as e:
                logger.debug('[Startup] meta cache invalidation skipped: %s', e)
        else:
            logger.debug('[Startup] No stale tasks or activeTaskIds found — clean shutdown')

        # ── Billed re-dispatch (autopilot-resume + killed-turn recovery) ──
        #   These SPAWN carriers. When ``dispatch=False`` (the server startup
        #   path) they are DEFERRED to ``run_deferred_boot_dispatch``, invoked
        #   from the serving loop — so a carrier is NOT scheduled on the startup
        #   event loop (which ``asyncio.run`` would then wait to drain before it
        #   can return → the 297s boot). ``dispatch=True`` keeps the legacy
        #   inline behaviour (used by direct callers/tests).
        if dispatch:
            run_deferred_boot_dispatch(
                {'recovered_conv_ids': recovered_conv_ids,
                 'killed_conv_ids': _killed_conv_ids,
                 'restart_storm': _restart_storm})
        else:
            return {'recovered_conv_ids': recovered_conv_ids,
                    'killed_conv_ids': _killed_conv_ids,
                    'restart_storm': _restart_storm}

    except Exception as e:
        logger.error('[Startup] Stale task recovery failed (non-fatal): %s', e, exc_info=True)
    return None


def _boot_auto_dispatch_enabled() -> bool:
    """Whether crash recovery may AUTO-EXECUTE billed LLM turns at boot.

    Default OFF (the safe, owner-mandated behaviour): startup recovery only
    marks interrupted turns and surfaces them in the sidebar — it NEVER
    auto-fires an autopilot-resume or killed-turn re-dispatch. A killed/armed
    tail stays tagged and visible for the user to resume MANUALLY.

    Set ``TOFU_BOOT_AUTO_DISPATCH=1`` (or true/yes/on) to opt back into the
    legacy auto-recovery behaviour. Read live (not memoized) so tests can
    toggle it via the environment.
    """
    import os
    return (os.environ.get('TOFU_BOOT_AUTO_DISPATCH', '') or '').strip().lower() \
        in ('1', 'true', 'yes', 'on')


def run_deferred_boot_dispatch(recovery_result, *, should_continue=None,
                               stop_event=None):
    """Run the BILLED boot re-dispatch (autopilot-resume + killed recovery).

    Split out of ``recover_stale_tasks_on_startup`` so the server can run it
    from the SERVING event loop AFTER ``hypercorn_serve`` has started, never on
    the startup loop. ``recovery_result`` is the descriptor that function
    returns with ``dispatch=False``.

    ★ GATED OFF BY DEFAULT (:func:`_boot_auto_dispatch_enabled`). Crash recovery
    must NOT auto-execute any billed LLM turn — it only marks interrupted turns
    and surfaces them in the sidebar (done by the synchronous sweep in
    ``recover_stale_tasks_on_startup``). This whole billed dispatch — BOTH the
    autopilot-resume AND the killed-turn re-dispatch lanes — is skipped unless
    ``TOFU_BOOT_AUTO_DISPATCH`` is explicitly enabled. The gate lives HERE, at
    the single chokepoint both the serving-loop path and the legacy inline
    ``dispatch=True`` path funnel through, so neither can auto-fire.

    ``should_continue`` is an optional zero-arg predicate checked ONCE at entry;
    when it returns False (a shutdown was requested during boot) the whole
    dispatch is SKIPPED so a ``^C`` during startup never fires a fresh billed
    carrier we are about to tear down.

    ``stop_event`` is the server's ``_shutdown_requested`` ``threading.Event``,
    passed THROUGH to killed-recovery's long-lived drain daemon so it aborts
    mid-drain on shutdown — checking a predicate only at entry cannot stop a
    daemon that keeps running (and keeps spawning carriers + touching PG) after
    shutdown. Best-effort — never raises.
    """
    if not isinstance(recovery_result, dict):
        return
    # ★ Crash recovery is DISPLAY-ONLY by default: no billed LLM turn is
    #   auto-executed at boot. Interrupted/killed/armed tails are already
    #   marked + surfaced in the sidebar by the synchronous recovery sweep;
    #   this billed dispatch stays OFF unless explicitly opted in.
    if not _boot_auto_dispatch_enabled():
        _rec = recovery_result.get('recovered_conv_ids') or []
        _kil = recovery_result.get('killed_conv_ids') or []
        logger.info('[Startup] Boot auto-dispatch DISABLED (default) — crash '
                    'recovery is display-only: %d recovered / %d killed conv(s) '
                    'left for MANUAL resume (set TOFU_BOOT_AUTO_DISPATCH=1 to '
                    'auto-recover)', len(_rec), len(_kil))
        return
    if should_continue is not None:
        try:
            if not should_continue():
                logger.info('[Startup] Deferred boot dispatch SKIPPED — '
                            'shutdown requested during startup')
                return
        except Exception as e:
            logger.debug('[Startup] should_continue predicate raised: %s', e)

    recovered_conv_ids = recovery_result.get('recovered_conv_ids') or []
    _killed_conv_ids = recovery_result.get('killed_conv_ids') or []
    _restart_storm = bool(recovery_result.get('restart_storm'))

    # ── Resume any autopilot run that was armed when the server died ──
    #   Recovery above restored the interrupted reply, but the crash killed
    #   the end-of-turn VU hook mid-flight (no follow-up spawned, no baton).
    #   The DURABLE armed-marker is authoritative here — resume scans EVERY
    #   conv carrying a marker (not just recovered tasks), so an armed-but-
    #   idle conv (marker present, no in-flight task at crash) is resumed
    #   too. Hence this runs UNCONDITIONALLY (not gated on recovered ids);
    #   recovered_conv_ids is passed only for logging-symmetry union.
    try:
        from lib.tasks_pkg.autopilot import (
            resume_armed_autopilot_after_crash,
        )
        resumed = resume_armed_autopilot_after_crash(recovered_conv_ids)
        if resumed:
            logger.info('[Startup] Resumed %d armed autopilot run(s) '
                        'after crash: %s', len(resumed),
                        [c[:8] for c in resumed])
    except Exception as e:
        logger.warning('[Startup] autopilot resume-after-crash failed '
                       '(non-fatal): %s', e, exc_info=True)

    # ── Actionable auto-recovery of OS-KILLED turns ──
    #   The shutdown-marker tagged these tails 'killed' (untrappable OS
    #   SIGKILL/OOM, NOT a manual stop). Re-dispatch them so the interrupted
    #   work actually completes — a tag alone is inert. Capped per turn
    #   (recoverAttempts) and STOOD DOWN during a restart storm so recovery
    #   never becomes a thundering-herd re-fire that worsens the crash loop
    #   (the 3,286-restart incident). Only fires for the 'unclean' verdict;
    #   a clean/manual shutdown produces no 'killed' convs.
    try:
        from lib.tasks_pkg.killed_recovery import (
            list_killed_turn_convs,
            run_killed_recovery,
        )
        # AUTHORITATIVE candidate set = DURABLE SCAN of persisted killed
        # tails, UNIONed with the convs freshly recovered this boot. The
        # fresh set alone is a PROXY that misses a conv whose killed turn was
        # already persisted (e.g. a prior recovery carrier that FATALed and
        # kept its 'killed' tag) — that conv has no running task this boot,
        # so it would never be collected and would be stranded forever. The
        # scan closes that gap (the autopilot-resume lesson: key on the
        # durable marker, never a proxy). Order-preserving de-dup.
        _scan = list_killed_turn_convs()
        _seen: set = set()
        _candidates: list = []
        for _cid in (_killed_conv_ids + _scan):
            if _cid and _cid not in _seen:
                _seen.add(_cid)
                _candidates.append(_cid)
        if _candidates:
            _kr = run_killed_recovery(_candidates, storm=_restart_storm,
                                      stop_event=stop_event)
            logger.info('[Startup] Killed-turn recovery: candidates=%d '
                        '(fresh=%d scan=%d) redispatched=%d deferred=%d '
                        'exhausted=%d storm_held=%d skipped=%d',
                        len(_candidates), len(_killed_conv_ids), len(_scan),
                        _kr['redispatched'], _kr.get('deferred', 0),
                        _kr['exhausted'], _kr['storm_held'], _kr['skipped'])
    except Exception as e:
        logger.warning('[Startup] killed-turn recovery failed '
                       '(non-fatal): %s', e, exc_info=True)


