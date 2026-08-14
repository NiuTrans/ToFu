"""Autopilot baton-handoff cluster — post-cutover leaf module.

Extracted from lib/tasks_pkg/autopilot.py (pt_00459503 slice 4) AFTER
pt_8dc03017 step-3 cutover completed (commits 3e2ec0c3 / aa6f7ea6 /
6286913d): the withhold latch (_autopilot_deciding), the parent-done
baton payload, and _VUEventForwarder are all gone from the codebase.
The 6 baton-handoff helpers here operate on the CLEANED surface — VU
sub-task now creates under the real conv_id, so `_start_followup_task`
supersedes concurrent tasks normally.

Symbols:
  * _presync_parent_reply(task)
  * _has_pending_real_message(conv_id)
  * _successor_already_running(task, conv_id)
  * _append_vu_message_to_conv(conv_id, vu_msg_id, text, rounds, run_id, segments)
  * _maybe_auto_translate_vu(conv_id, vu_msg_id, content)
  * _start_followup_task(task, conv_id)

Called ONLY from lib.tasks_pkg.autopilot.maybe_run_autopilot; plus one
external CALL-time import: lib/tasks_pkg/endpoint/_translate.py imports
_maybe_auto_translate_vu via `from lib.tasks_pkg.autopilot import
_maybe_auto_translate_vu`, which resolves through the facade re-export.
"""

from __future__ import annotations

import json
import time

from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────
#  Follow-up scheduling — append a synthetic user msg + start a task
# ──────────────────────────────────────────────────────────────────

def _presync_parent_reply(task: dict) -> None:
    """Commit the parent task's FINAL assistant message to the conv DB.

    MUST run before this hook appends the VU turn / spawns the follow-up:
    once a follow-up registers as ``_conv_latest_task`` the freshness guard
    in ``manager._sync_result_to_conversation`` rejects the parent's final
    write, freezing the reply at its last streaming checkpoint (truncated
    content, ``finishReason=None``) and feeding that truncated copy to the
    follow-up.

    The orchestrator already calls this once before the hook when autopilot
    was enabled at task-creation time.  We repeat it here so the RUNTIME-ARM
    path (autopilot flipped on mid-stream via ``arm_autopilot``) is equally
    safe regardless of whether the arm landed before or after the
    orchestrator's gate — ``_sync_result_to_conversation`` only FILLS the
    trailing assistant slot (find-or-append), so a second call is an
    idempotent no-op when the orchestrator already synced.
    """
    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        return
    try:
        from lib.tasks_pkg.manager import (
            _sync_result_to_conversation,
            build_result_meta,
        )
        _sync_result_to_conversation(task, build_result_meta(task))
    except Exception as e:
        logger.warning('[Autopilot] parent pre-sync failed: %s — follow-up '
                       'may see a truncated parent reply', e, exc_info=True)


def _has_pending_real_message(conv_id: str) -> bool:
    """True iff a real HUMAN turn is queued — autopilot must yield to a person.

    Delegates to ``message_queue.has_pending_human_turn``, THE single
    consume-time predicate the dispatcher itself uses. It must stay a
    delegation: the previous implementation counted rows with
    ``get_queue_depth`` (filter: ``kind != KIND_AUTOPILOT``), a WEAKER filter
    than the dispatcher's, so the two readers could disagree about the same
    row. On 2026-07-28 they did — a brain kickoff whose epic finished while it
    sat queued read as "a human is waiting" here and as "discard me" there, so
    a completed VU turn was thrown away and nothing was dispatched in its
    place.

    Two behaviours are load-bearing and both are asserted in
    ``tests/test_autopilot_yield_not_destroy.py``:
      * only ``KIND_REAL`` preempts — machine work items (brain kickoffs, peer
        messages) wait for the run to end and are then picked up by the idle
        drain;
      * a row that would NOT really be dispatched never counts as a reason to
        stand down.
    """
    if not conv_id:
        return False
    try:
        from lib.message_queue import has_pending_human_turn
        return has_pending_human_turn(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] queue probe failed (non-fatal): %s', e)
        return False


def _successor_already_running(task: dict, conv_id: str) -> bool:
    """True if another task has already taken over for this conversation.

    ``persist_task_result`` runs ``_dispatch_queued_message`` before our
    hook fires, so a queued real-user message will already have spawned
    its own follow-up task.  Spawning a VU follow-up on top of that
    would (a) abort the queued task via ``abort_running_tasks_for_conv``
    and (b) clobber the user's actual question.  Detect this by looking
    at the latest-task registry.

    A pointer is not an owner. The index is a raw conv→task_id pointer that
    can legitimately name a DEAD task at recheck time — most notably the
    run's OWN VU carrier, which claims the index for the HB-1 handoff
    window and is discarded before this gate re-reads it (msb6ohqi
    2026-08-02: the gate read the corpse, concluded "superseded", and
    stranded a delivered VU turn with no follow-up). The conv-sync
    freshness guard has encoded the matching "own VU carrier = by-design
    handoff" exception since HB-1 landed (manager/_sync.py); this gate
    must apply the same judgement: only a task that is still LIVE in the
    registry is a real successor.
    """
    if not conv_id:
        return False
    try:
        from lib.tasks_pkg.manager import (
            _conv_latest_task,
            _conv_latest_task_lock,
            tasks,
            tasks_lock,
        )
        with _conv_latest_task_lock:
            latest = _conv_latest_task.get(conv_id)
        if not latest or latest == task.get('id'):
            return False
        with tasks_lock:
            succ = tasks.get(latest)
        if succ is None or succ.get('status') in ('done', 'error', 'aborted'):
            logger.info('[Autopilot] latest-task pointer for conv=%s names a '
                        'dead task %s (own VU carrier=%s, in-registry=%s) — '
                        'a pointer is not an owner; NOT superseded',
                        conv_id[:8], latest[:8],
                        (task.get('_vu_carrier_id') or '')[:8],
                        succ is not None)
            return False
        logger.info('[Autopilot] conv=%s is owned by live successor task=%s — '
                    'standing down is correct', conv_id[:8], latest[:8])
        return True
    except Exception as e:
        logger.debug('[Autopilot] latest-task probe failed (non-fatal): %s', e)
        return False


def _append_v2_autopilot_turns(task: dict, conv_id: str, vu_msg_id: str,
                               text: str, rounds: list | None = None,
                               run_id: str = '', segments: list | None = None
                               ) -> dict | None:
    """Create the VU turn and its pending Agent successor in one transaction."""
    try:
        from lib.tasks_pkg.manager._registry import task_user_id
        from lib.turn_lifecycle import (
            announce_related_turns,
            create_turn_pair,
            get_turn,
        )

        parent_turn = get_turn(
            conv_id, task.get('_turnId') or '',
            user_id=task_user_id(task) or 1)
        cfg = dict(task.get('config') or {})
        result = create_turn_pair(
            conv_id,
            command_id=(f'autopilot:{task.get("_attemptId") or task.get("id")}:'
                        f'{vu_msg_id}'),
            input_projection={
                'content': text, 'thinking': '', 'toolRounds': rounds or [],
                'segments': segments or [], 'timestamp': int(time.time() * 1000),
            },
            config=cfg,
            lane_id=parent_turn.get('laneId') or 'main',
            parent_turn_id=parent_turn['turnId'],
            kind='autopilot_reply', output_actor='assistant', run_id=run_id,
            user_id=task_user_id(task) or 1,
            input_actor='virtual_user', input_kind='autopilot_virtual_user',
            require_parent_is_lane_tail=True,
        )
        task['_v2AutopilotNext'] = result
        task['_v2NextTurnId'] = result['turn']['turnId']
        task['_v2NextAttemptId'] = result['attempt']['attemptId']
        announce_related_turns(
            task.get('_attemptId') or '',
            [result['submittedTurn']['turnId'], result['turn']['turnId']],
        )
        return {
            'role': 'user', 'content': text,
            'timestamp': result['submittedTurn']['createdAt'],
            '_turnId': result['submittedTurn']['turnId'],
            '_isVirtualUser': True, '_autopilotRunId': run_id,
            'toolRounds': rounds or [], 'segments': segments or [],
        }
    except Exception as exc:
        if getattr(exc, 'code', None):
            logger.info('[Autopilot] v2 continuation stood down conv=%s: %s',
                        conv_id[:8], exc.code)
            return None
        logger.error('[Autopilot] v2 continuation create failed conv=%s: %s',
                     conv_id[:8], exc, exc_info=True)
        return None


def _append_vu_message_to_conv(conv_id: str, vu_msg_id: str,
                                text: str,
                                rounds: list | None = None,
                                run_id: str = '',
                                segments: list | None = None) -> dict | None:
    """Append the VU's reply as a user message in the conversation DB.

    Called ONLY after the VU has successfully produced a reply (i.e.
    after ``run_virtual_user`` returned non-``None``).  This is a
    deliberate design choice:

      • We DO NOT pre-write an empty placeholder before the VU runs.
        Doing so used to leave orphan empty rows in the DB whenever
        the cleanup path was missed (server crash, abort race, etc.)
        — visible to the user as "an empty VU bubble at the bottom"
        even when autopilot never actually took over.

      • The frontend lazily creates the VU bubble in memory when it
        receives the first ``autopilot_vu_event`` carrying actual
        content (``delta`` with text or ``tool_start``).  No DB write
        happens until success — so a VU that bails out (``[VU:
        TASK_DONE]``, abort, real user msg) leaves NO trace on disk.

    ``_msgId`` is the caller-minted id that the frontend used to route
    streaming updates; persisting it here lets a page reload right
    AFTER autopilot completes find the same message id and reconcile.

    Returns the appended message on success, or ``None`` when the turn was NOT
    persisted — which the caller must treat as "stand down and preserve the
    reply via ``_preserve_unsent_vu_and_conclude``", never as a silent drop.
    ``None`` covers a missing row, an exhausted CAS budget, and the deliberate
    stand-down when a real human turn landed mid-flight.
    """
    try:
        from lib.database import (
            DOMAIN_CHAT,
            get_thread_db,
        )
    except Exception as e:
        logger.warning('[Autopilot] DB import failed: %s', e)
        return None

    try:
        db = get_thread_db(DOMAIN_CHAT)
        # Time basis for "did a human speak while we were producing this?".
        # Taken BEFORE the first read so any human row we observe with a later
        # timestamp genuinely arrived during this append attempt rather than
        # being pre-existing history.
        _started_ms = int(time.time() * 1000)
        vu_msg = {
            'role': 'user',
            'content': text,
            'timestamp': int(time.time() * 1000),
            '_msgId': vu_msg_id,
            '_isVirtualUser': True,
        }
        if run_id:
            vu_msg['_autopilotRunId'] = run_id
        if rounds:
            vu_msg['toolRounds'] = rounds
        # Segments (epic pt_cb8f98b0cb9b47fb): the thin typed-timeline list so
        # the VU turn renders the IDENTICAL agent inline per-tool timeline. On
        # reload, the save_conv preserve-merge re-attaches this by `_msgId` (the
        # `_isVirtualUser` row carries `_msgId`, set above) on every stripped
        # client PUT — so the timeline survives refresh, not just live+settle.
        if segments:
            vu_msg['segments'] = segments

        # Append under a rev-CAS, replaying onto a fresh read on a lost race.
        #
        # This function is BOTH victim and aggressor. It was the row erased by
        # the snapshot daemon's blind write-back, but its own UPDATE used to be
        # equally unconditional — so it would just as happily erase an assistant
        # sync or a translation commit that landed after its SELECT. Fixing only
        # the reader would have left the identical hole open at the hottest
        # concurrency point in the system.
        _MAX_CAS = 5
        for attempt in range(1, _MAX_CAS + 1):
            from lib.database.conversation_repository import load_conversation
            snapshot = load_conversation(db, conv_id)
            if snapshot is None:
                logger.warning('[Autopilot] conv=%s not found — cannot append VU msg',
                               conv_id[:8])
                return None
            messages = snapshot.messages
            cur_rev = int(snapshot.get('rev') or 0)

            # A REAL human turn landed while we were producing this reply.
            # Do NOT append behind it: the next turn feeds conversation history
            # back to the model, so a virtual-user row sitting after a human
            # question reads as words the human said — the machine-token leak
            # (pt_0ae59e94) in message form. Only KIND_REAL preempts autopilot,
            # and preemption means the run stands down. Returning None routes
            # the caller into _preserve_unsent_vu_and_conclude, so the text is
            # kept in the sidecar (expandable by the user) and the run emits its
            # terminal event — preserved and observable, never silently dropped.
            if any(isinstance(m, dict) and m.get('role') == 'user'
                   and not m.get('_isVirtualUser')
                   and m.get('timestamp', 0) > _started_ms
                   for m in messages):
                logger.info('[Autopilot] conv=%s a real human turn landed while the '
                            'VU was producing — standing down instead of appending '
                            'behind it (reply preserved in the sidecar)',
                            conv_id[:8])
                return None

            if any(isinstance(m, dict) and m.get('_msgId') == vu_msg_id
                   for m in messages):
                logger.info('[Autopilot] conv=%s VU msg %s already present — '
                            'treating append as done', conv_id[:8], vu_msg_id[:12])
                return vu_msg

            messages.append(vu_msg)
            now_ms = int(time.time() * 1000)
            try:
                from lib.conversations import build_search_text
                search_text = build_search_text(messages)
            except Exception as e:
                logger.debug('[Autopilot] build_search_text failed: %s', e)
                search_text = ''

            from lib.database.conversation_repository import replace_messages
            result = replace_messages(
                db, conv_id, messages, expected_rev=cur_rev,
                metadata={
                    'updated_at': now_ms,
                    'msg_count': len(messages),
                    'search_text': search_text,
                })
            if not result.applied:
                logger.debug('[Autopilot] conv=%s VU append lost the rev=%s race '
                             '(attempt %d/%d) — re-reading',
                             conv_id[:8], cur_rev, attempt, _MAX_CAS)
                continue

            logger.info('[Autopilot] conv=%s ✅ Appended VU msg %s (%d chars, %d rounds)',
                        conv_id[:8], vu_msg_id[:12], len(text), len(rounds or []))
            return vu_msg

        logger.warning('[Autopilot] conv=%s VU append gave up after %d CAS '
                       'attempts — reply preserved by the caller instead',
                       conv_id[:8], _MAX_CAS)
        return None
    except Exception as e:
        logger.error('[Autopilot] conv=%s append failed: %s',
                     conv_id[:8], e, exc_info=True)
        return None


def _maybe_auto_translate_vu(conv_id: str, vu_msg_id: str, content: str) -> None:
    """Server-side auto-translate safety net for an appended Autopilot VU turn.

    The virtual-user turn is persisted by ``_append_vu_message_to_conv`` on a
    code path SEPARATE from ``manager._sync_result_to_conversation`` (which
    owns the assistant/critic safety net), so without this call a VU turn is
    only ever translated if a viewer happens to fire a manual translate — the
    reported "autopilot conversation never triggers auto-translate" bug.

    A VU row is stored ``role='user'`` + ``_isVirtualUser=True`` and is
    DISPLAY-translated (``content`` = model-language original, the safety net
    writes the UI-language ``translatedContent`` outer bubble), so the
    role-agnostic ``_maybe_auto_translate_assistant`` is the correct engine.

    We resolve the row INDEX from the freshly-persisted messages by matching
    ``_msgId == vu_msg_id`` (authoritative — never a guessed positional), and
    deliberately pass NO ``task``: the parent task's ``_assistantMsgId`` and its
    incremental per-round accumulator belong to the assistant turn, not this VU
    content — handing them in would mis-anchor the translation and adopt the
    wrong accumulator. The whole-message thread is the right path here. The
    safety net's own gates (``resolve_auto_translate`` off, already-Chinese,
    existing ``translatedContent``, and the ``claim_inflight`` dedup keyed by
    ``_msgId``) make this idempotent against a concurrent frontend manual
    translate. Best-effort: never raises into the autopilot loop.
    """
    if not conv_id or not vu_msg_id or not content:
        return
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.database.conversation_repository import load_conversation
        db = get_thread_db(DOMAIN_CHAT)
        snapshot = load_conversation(db, conv_id)
        if snapshot is None:
            return
        messages = snapshot.messages
        vu_idx = next(
            (i for i, m in enumerate(messages)
             if isinstance(m, dict) and m.get('_msgId') == vu_msg_id),
            -1,
        )
        if vu_idx < 0:
            logger.debug('[Autopilot] conv=%s VU msg %s not found for '
                         'auto-translate — skipping', conv_id[:8], vu_msg_id[:12])
            return
        from lib.tasks_pkg.auto_translate import _maybe_auto_translate_assistant
        _maybe_auto_translate_assistant(conv_id, content, vu_idx, db=db)
    except Exception as e:
        logger.warning('[Autopilot] conv=%s VU auto-translate failed '
                       '(non-fatal): %s', conv_id[:8], e)


def _start_followup_task(task: dict, conv_id: str) -> str | None:
    """Build api_messages from the conversation and spawn a new task.

    Mirrors what ``_start_task_for_conv`` does, but inlined to avoid
    importing from ``routes`` (orchestrator must not pull route-layer
    code at module scope — circular).
    """
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.manager import abort_running_tasks_for_conv

    cfg = dict(task.get('config') or {})
    # Strip checkpoint / continue flags so the follow-up runs fresh.
    #
    # ★ assistantMsgId MUST be stripped: it is the CLIENT-minted stable id of
    #   the ORIGINAL turn's assistant bubble (shipped once in the send POST).
    #   If it survives the cfg copy, create_task stamps it as this follow-up's
    #   `_assistantMsgId`, and _new_assistant_slot then reuses it as the
    #   committed row's `_msgId` — so EVERY follow-up in the run commits with
    #   the SAME `_msgId`. The frontend keys/dedups DOM nodes by `_msgId`, so N
    #   colliding assistant rows collapse into ONE bubble and the Agent replies
    #   between VU turns become invisible (observed: 16 assistant rows sharing
    #   one id → transcript degenerates to a wall of VU/user turns). The
    #   follow-up has NO live client bubble carrying this id (the frontend mints
    #   a fresh one in _attachAutopilotFollowup), so dropping it lets
    #   _assign_message_ids mint a UNIQUE server UUID per follow-up.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
        'assistantMsgId', 'msgId',
    ):
        cfg.pop(stale_key, None)

    # ★ pt_a21cd6eb bug#2: the model is the CONVERSATION'S LIVE choice, not
    #   the run's launch-time pin. cfg is inherited from the task that
    #   STARTED the run, so an owner switching models mid-run (or the quota
    #   fallback persisting a working model into settings) never reached the
    #   follow-ups — measured 2026-08-01: conv switched to kimi-k3 at 16:58,
    #   the 18:20 follow-up still launched on the parent's claude-opus-5 and
    #   walked straight back into the same 429 wall. Re-resolve ONLY
    #   model/preset from the conv's current settings; every other key stays
    #   inherited (turn-scoped state).
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        _db = get_thread_db(DOMAIN_CHAT)
        _row = _db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)).fetchone()
        if _row:
            _live = json.loads(_row['settings'] or '{}')
            _lm = (_live.get('model') or '').strip()
            _lp = (_live.get('preset') or '').strip()
            if _lm and _lm != cfg.get('model'):
                logger.info('[Autopilot] follow-up model re-resolved from '
                            'conv settings: %s → %s (the run inherited the '
                            'launch-time pin; the live choice wins)',
                            cfg.get('model'), _lm)
                cfg['model'] = _lm
                if _lp:
                    cfg['preset'] = _lp
    except Exception as _me:
        logger.debug('[Autopilot] conv model re-resolve failed — inheriting '
                     'parent model: %s', _me)

    v2_result = task.get('_v2AutopilotNext') if task.get('_turnProtocolV2') else None
    if v2_result:
        from lib.turn_lifecycle import build_api_messages
        cfg.update({
            '_turnProtocolV2': True,
            '_turnId': v2_result['turn']['turnId'],
            '_attemptId': v2_result['attempt']['attemptId'],
            'excludeLast': True,
        })
        api_messages = build_api_messages(
            conv_id, v2_result['turn']['turnId'], cfg)
    else:
        from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db
        api_messages = build_api_messages_from_db(conv_id, cfg)
    if not api_messages:
        logger.warning('[Autopilot] conv=%s build_api_messages returned '
                       'empty — cannot start follow-up', conv_id[:8])
        return None

    # Belt-and-braces: any other still-running task for this conv is
    # superseded by this autopilot follow-up, same as a real user send.
    if not v2_result:
        abort_running_tasks_for_conv(conv_id)

    if v2_result:
        from lib.turn_lifecycle import claim_attempt_start, fail_start
        if not claim_attempt_start(v2_result['attempt']['attemptId']):
            return None
    try:
        # Keep the legacy call shape unchanged (besides preserving existing
        # tests and extensions, its default is deliberately supersede=True).
        # A v2 successor is an already-authorized attempt and must coexist
        # with its parent until the parent's terminal hook has returned.
        if v2_result:
            new_task = create_task(
                conv_id, api_messages, cfg, supersede=False)
        else:
            new_task = create_task(conv_id, api_messages, cfg)
    except Exception as e:
        logger.error('[Autopilot] Failed to create follow-up task: %s',
                     e, exc_info=True)
        if v2_result:
            from lib.error_envelope import make_envelope as _make_env
            fail_start(
                v2_result['attempt']['attemptId'],
                _make_env(
                    'internal',
                    detail='Autopilot failed to create follow-up task.',
                    model=cfg.get('model', ''), context='autopilot',
                    source='autopilot', raw=str(e),
                ),
            )
        return None
    new_task_id = new_task['id']
    new_task['_autopilotParent'] = task.get('id')

    logger.info('[Autopilot] Spawning follow-up task %s for conv=%s '
                '(parent=%s)', new_task_id[:8], conv_id[:8],
                task.get('id', '?')[:8])
    audit_log('autopilot_followup',
              parent_task_id=task.get('id', ''),
              new_task_id=new_task_id,
              conv_id=conv_id)

    try:
        if v2_result:
            from lib.turn_lifecycle import bind_task
            bind_task(v2_result['attempt']['attemptId'], new_task_id)
        from lib.tasks_pkg import spawn_task as _spawn_task
        _spawn_task(new_task)
    except Exception as e:
        logger.error('[Autopilot] Failed to start follow-up thread: %s',
                     e, exc_info=True)
        from lib.error_envelope import make_envelope as _make_env
        new_task['status'] = 'error'
        new_task['error'] = _make_env(
            'internal',
            detail='Autopilot failed to spawn follow-up thread.',
            model=cfg.get('model', ''),
            context='autopilot',
            source='autopilot',
            raw=str(e),
        )
        if v2_result:
            fail_start(v2_result['attempt']['attemptId'], new_task['error'])
        return None

    # Update conversation settings.activeTaskId so reload still finds the
    # live task.  Best-effort — failure here doesn't break the loop.
    # Serialized read-merge-write (settings_store) so this doesn't clobber a
    # concurrent tool-state / autopilot settings write on the same row.
    try:
        from lib.conversations import set_conversation_settings
        # notify=False: notify_conv_changed is emitted just below (no double
        # push); the gate still invalidates the sidebar cache.
        if not v2_result:
            set_conversation_settings(conv_id, {'activeTaskId': new_task_id},
                                      notify=False)
    except Exception as e:
        logger.debug('[Autopilot] activeTaskId update skipped: %s', e)

    try:
        from lib.conversations import notify_conv_changed
        from lib.tasks_pkg.manager._registry import task_user_id
        notify_conv_changed(conv_id, rev=None, user_id=task_user_id(task))
    except Exception as e:
        logger.debug('[Autopilot] conv-changed notify skipped: %s', e)

    return new_task_id
