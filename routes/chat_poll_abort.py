"""routes/chat_poll_abort.py — poll / abort / flow-trace handlers.

Extracted from routes/chat.py (pt_04686ac6 slice 10 — the sub-package
decomposition half of the epic). Matches the sibling-module pattern
established by routes/chat_queue.py, routes/chat_human_io.py,
routes/chat_tool_state.py: the module ATTACHES routes to the same
api_v1_chat_bp via @route decorators, and routes/__init__.py side-effect
imports it so the decorators fire at app-init time.

Handlers moved (byte-parity):

  * chat_abort_conv — POST /api/v1/chat/abort-conv/<conv_id>
  * chat_abort     — POST /api/v1/chat/abort/<task_id>
  * chat_poll      — GET  /api/v1/chat/poll/<task_id>
  * chat_flow_trace— GET  /api/v1/chat/flow-trace/<task_id>

The four handlers share a common concern: reading task state (from memory
or DB) and either mutating it (abort) or returning it (poll / flow-trace).
Zero coupling to the send/stream/continue paths, which is why they extract
cleanly as a single cluster.
"""

from __future__ import annotations

import json
import time
import zlib

from flask import request

from lib.api_response import api_not_found, api_ok
from lib.database import DOMAIN_CHAT, get_db
from lib.log import audit_log, get_logger
from lib.tasks_pkg import tasks, tasks_lock
from routes.api_v1.auth import current_auth, require_scope
from routes.api_v1.chat import api_v1_chat_bp
from routes.chat_helpers import (
    _loads_yielding,
    _log_poll_task_id_mismatch,
    _running_checkpoint_verdict,
)
from routes.chat_state import _mark_conv_aborted
from lib.chat.persistence import extract_db_meta as _extract_db_meta

logger = get_logger(__name__)

# ── Terminal-field gate (2026-07-31, conv ms8c0645hwl327) ────────────────
# `finishReason` (and the terminal metadata that travels with it) is a
# TERMINAL signal: the frontend reads it as "this turn is over". But the
# orchestrator stamps ``task['finishReason']`` ~111 lines BEFORE it flips
# ``task['status']='done'`` (lib/tasks_pkg/orchestrator/_finalize.py), and that
# window contains the blocking ``_generate_tool_summary`` LLM call — so it is
# seconds wide, not microseconds. Polls landing inside it used to receive the
# self-contradictory pair ``{status:'running', finishReason:'stop'}``.
#
# The frontend acts on that: `_pollFallback` copies `finishReason` onto the live
# message, and `assistantTailIsPriorTurn` then classifies the task's OWN live
# bubble as a PRIOR turn — so `connectToTask` pushes a fresh placeholder, the
# deltas move to it, the first bubble freezes mid-sentence and BOTH render.
# One conv.messages entry, two agent bubbles.
#
# Fix the SOURCE: a non-terminal snapshot never advertises terminal fields.
# Everything else on the wire (content / thinking / toolRounds / phase /
# progress chips) is explicitly still shipped while running — this withholds
# only the fields that MEAN "finished".
#
# ★ The RULE ITSELF lives in lib/chat/terminal_gate.py — ONE implementation
#   shared with the SSE `state` snapshot (lib/chat_dispatch.py). It was briefly
#   defined locally here, which would have been the fourth hand-maintained copy
#   of a metadata field policy; `extract_task_meta`'s docstring records what
#   that asymmetry has already cost this project.
from lib.chat.terminal_gate import (  # noqa: E402
    TERMINAL_ONLY_KEYS as _TERMINAL_ONLY_KEYS,
    is_terminal_status as _is_terminal_status,
)


@api_v1_chat_bp.route('/api/v1/chat/abort-conv/<conv_id>', methods=['POST'], endpoint='ui_chat_abort_conv')
@require_scope('chat')
def chat_abort_conv(conv_id):
    """Abort all running tasks for a conversation by conv ID.

    Used when the frontend aborts during translation and never received a
    taskId — the server may have already started a task that needs to be
    killed.  This is the convId-based counterpart of ``/api/chat/abort/<task_id>``.

    Also records a per-conv abort marker so any /api/chat/send still
    blocked inside auto-translate can detect the abort and bail out
    before persisting / enqueueing / dispatching the message.
    """
    from lib.tasks_pkg import abort_running_tasks_for_conv
    _mark_conv_aborted(conv_id)
    aborted = abort_running_tasks_for_conv(conv_id)
    # ★ ③-3 (pt_a21cd6eb): also tombstone running DB rows the registry has
    #   LOST — the in-registry sweep above structurally cannot reach them
    #   (measured 2026-08-01: abort-conv returned 0 while a live task spun).
    from lib.tasks_pkg.manager import (
        plant_abort_tombstones_for_conv as _plant_conv,
    )
    tombstoned = _plant_conv(conv_id, source='api_chat_abort_conv')
    if aborted or tombstoned:
        logger.info('[Chat] Abort-by-conv conv=%s — aborted %d task(s), '
                    'tombstoned %d registry-lost',
                    conv_id[:8], aborted, tombstoned)
    else:
        logger.debug('[Chat] Abort-by-conv conv=%s — no running tasks found', conv_id[:8])
    return api_ok({'aborted': aborted, 'tombstoned': tombstoned})
@api_v1_chat_bp.route('/api/v1/chat/abort/<task_id>', methods=['POST'], endpoint='ui_chat_abort')
@require_scope('chat')
def chat_abort(task_id):
    """Abort a running task by id.

    Sets ``task['aborted']`` (the orchestrator polls this between rounds),
    SIGTERMs any spawned ``run_command`` subprocess, and signals the external
    backend if one is in use. Idempotent — a duplicate abort logs at WARNING
    and returns ok.

    This is the single, authoritative abort handler — it carries the real
    subprocess / external-backend kill logic. The previous duplicate stub in
    ``routes/api_v1/chat.py`` (which only flipped ``aborted``) was removed.
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        # ★ ③-3 (pt_a21cd6eb): a registry miss no longer means “no such
        #   task” — the 2026-08-01 evaporation family proved a LIVE task can
        #   be missing here (abort 404'd while the worker kept cycling).
        #   When a running DB row exists, plant an abort tombstone: the
        #   worker's abort_check consumes it at its next retry poll.
        from lib.tasks_pkg.manager import plant_abort_tombstone as _plant
        if _plant(task_id, source='api_chat_abort'):
            return api_ok(taskId=task_id, status='abort_signaled',
                          note='task absent from registry; abort tombstone '
                               'planted — the live worker consumes it at its '
                               'next abort poll')
        return api_not_found('Not found')
    was_already_aborted = task.get('aborted', False)
    task['aborted'] = True
    task['_abort_timestamp'] = time.time()
    audit_log('api_chat_abort',
              key_id=(current_auth().key_id if current_auth() else ''),
              task_id=task_id)
    # Log comprehensive abort context
    _status = task.get('status', '?')
    _elapsed = time.time() - task.get('created_at', time.time())
    _content_len = len(task.get('content') or '')
    _thinking_len = len(task.get('thinking') or '')
    _model = task.get('model', '?')
    _conv_id = task.get('convId', '?')
    if was_already_aborted:
        logger.warning('[Chat] Task %s abort DUPLICATE — already aborted. conv=%s status=%s',
                       task_id, _conv_id, _status)
    else:
        logger.info('[Chat] Task %s ABORT RECEIVED — conv=%s model=%s status=%s '
                    'elapsed=%.1fs content=%dchars thinking=%dchars',
                    task_id, _conv_id, _model, _status, _elapsed, _content_len, _thinking_len)
    # ── Kill any running subprocess (run_command) ──
    _sub_pid = task.get('_subprocess_pid')
    if _sub_pid:
        try:
            import os as _os
            import signal as _signal
            _pgid = task.get('_subprocess_pgid')
            if _pgid:
                _os.killpg(_pgid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess process group pgid=%d',
                            task_id[:8], _pgid)
            else:
                _os.kill(_sub_pid, _signal.SIGTERM)
                logger.info('[Chat] Task %s — sent SIGTERM to subprocess pid=%d',
                            task_id[:8], _sub_pid)
        except (OSError, ProcessLookupError) as e:
            logger.debug('[Chat] Task %s — subprocess kill skipped: %s', task_id[:8], e)

    # ── User-Stop busy-projection broadcast ──
    # The busy projection (snapshot_running_by_conv → conv_has_work_in_flight)
    # already EXCLUDES an aborted task by design ("aborted always wins: the
    # instant the user presses Stop the conversation must read idle"), but a
    # frame only leaves the server when someone EMITS it — and this seam
    # never did. Without it the originating tab's authoritative busy Set
    # still holds this tid after finishStream cleared the local handles
    # (activeStreams + conv.activeTaskId): convIsBusy keeps the composer in
    # Stop shape while Priority-3 of the stop cascade has no handle left, so
    # every further click is a silent no-op until the task fully unwinds and
    # the TERMINAL frame lands (up to a whole tool call later) — the "Stop
    # takes several clicks" report. This is the third emit site of the SAME
    # broadcast: the supersede sweep (manager/_registry.py P3) and
    # notify_terminal_busy_state already carry the other two.
    # Unconditional: a duplicate abort re-asserts the idle projection for a
    # client that missed the first frame. Fail-open: a notify/import error
    # must never break the abort path (notify_conv_changed is fail-open too).
    if _conv_id and _conv_id != '?':
        try:
            from lib.conversations.meta_cache import notify_conv_changed
            from lib.tasks_pkg.manager._registry import task_user_id
            notify_conv_changed(_conv_id, rev=None, user_id=task_user_id(task))
        except Exception as _ne:
            logger.warning('[Chat] Task %s abort busy-notify failed: %s',
                           task_id[:8], _ne)

    return api_ok()


@api_v1_chat_bp.route('/api/v1/chat/interrupt-command/<task_id>', methods=['POST'],
                      endpoint='ui_chat_interrupt_command')
@require_scope('chat')
def chat_interrupt_command(task_id):
    """Interrupt the task's CURRENTLY-RUNNING run_command — WITHOUT aborting
    the task (owner directive 2026-08-01, pt_232244fb).

    Sets ``task['_cmd_interrupt']``; the run_command read loop (which polls
    every ~0.2s) consumes it, kills the process tree, and returns the
    PARTIAL output plus the interruption marker as an ordinary tool result —
    so the model sees what the command produced before being stopped and the
    turn continues. This is the per-command counterpart of
    ``/api/v1/chat/abort/<task_id>`` (which stops the WHOLE turn).

    Response shapes (all 200 except a missing task):
      * ``{'interrupted': True, 'pid': N}``                  — flag planted
      * ``{'interrupted': False, 'reason': 'task_not_running'}``
      * ``{'interrupted': False, 'reason': 'no_active_command'}`` — the task
        is not inside a run_command right now (nothing to interrupt)
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return api_not_found('Not found')
    if task.get('status') != 'running' or task.get('aborted'):
        return api_ok({'interrupted': False, 'reason': 'task_not_running'})
    pid = task.get('_subprocess_pid')
    if not pid:
        return api_ok({'interrupted': False, 'reason': 'no_active_command'})
    task['_cmd_interrupt'] = {'source': 'user', 'ts': time.time(), 'note': '',
                              'pid': pid}
    audit_log('api_chat_interrupt_command',
              key_id=(current_auth().key_id if current_auth() else ''),
              task_id=task_id)
    logger.info('[Chat] Task %s — user interrupt requested for run_command pid=%s',
                task_id[:8], pid)
    return api_ok({'interrupted': True, 'pid': pid})


# ── Incremental-poll section trimming (2026-08-06, epic pt_688f9783) ────────
# The degraded-mode poll loop (sse_poll_fallback.js) re-requests this endpoint
# every ~0.3-2s for the REST OF THE TURN, and the FULL snapshot — content +
# thinking + toolRounds with every tool result verbatim — rode every response:
# measured 968KB per poll on a 60-round task, ~2MB/2s through the tunnel when
# a few convs degrade at once (the 2026-08-06 four-streams-one-second flap).
#
# Opt-in echo-fingerprint protocol (non-incr callers are byte-identical):
# an incr=1 response carries `fp` — per-section fingerprints; the poll loop
# echoes them back verbatim as `ifp`; a section whose fingerprint still
# matches is OMITTED with a `<section>Same` marker. Self-healing by
# construction: any reset/truncation/rewrite changes the fingerprint, so the
# next response carries the full section again — no server-side versioning,
# no bookkeeping, one-cycle worst case. crc32 catches the same-length
# mutation (an in-flight round's elapsed tick) that a length check cannot.
def _incr_poll_fps(task: dict) -> dict:
    """Per-section fingerprints of the CURRENT shippable poll sections."""
    def _crc(s: str) -> int:
        return zlib.crc32(s.encode('utf-8')) if s else 0

    def _last_crc(rows) -> int:
        if not rows:
            return 0
        try:
            return zlib.crc32(
                json.dumps(rows[-1], ensure_ascii=False, default=str).encode('utf-8'))
        except Exception as e:
            # Unserialisable tail → never matches an echo → always full.
            logger.debug('[Chat] incr poll fp crc failed: %s', e)
            return -1

    _rounds = task.get('toolRounds') or []
    _ep_turns = task.get('_endpoint_turns') or []
    return {
        'c': _crc(task.get('content') or ''),
        't': _crc(task.get('thinking') or ''),
        'r': [len(_rounds), _last_crc(_rounds)],
        'e': [len(_ep_turns), _last_crc(_ep_turns)],
    }


def _maybe_trim_incr_poll(r: dict, task: dict) -> dict:
    """Trim an already-built full poll payload for `?incr=1` callers.

    Post-filter shape: the caller builds `r` exactly as before (the field-list
    contract is untouched), then fat sections are dropped when the echoed
    fingerprint matches. `fp` rides EVERY incr response so the client can
    start (or re-start) echoing after any gap.
    """
    if request.args.get('incr') != '1':
        return r
    _ifp = None
    _raw_ifp = request.args.get('ifp') or ''
    if _raw_ifp:
        try:
            _ifp = json.loads(_raw_ifp)
        except (ValueError, TypeError) as _ifp_err:
            logger.debug('[Chat] incr poll — bad ifp ignored: %s', _ifp_err)
            _ifp = None
    _fp = _incr_poll_fps(task)
    r['fp'] = _fp

    def _same(tag) -> bool:
        return bool(_ifp) and _ifp.get(tag) == _fp.get(tag)

    if _same('c') and 'content' in r:
        del r['content']
        r['contentSame'] = True
    if _same('t') and 'thinking' in r:
        del r['thinking']
        r['thinkingSame'] = True
    if _same('r') and 'toolRounds' in r:
        del r['toolRounds']
        r['roundsSame'] = True
    if _same('e') and 'endpointTurns' in r:
        del r['endpointTurns']
        r['endpointTurnsSame'] = True
    return r


def _live_unregistered_gate(task_id) -> bool:
    """③-2 (pt_a21cd6eb): positive proof-of-life for a registry-missing task.

    The stale-checkpoint flip assumes “not in THIS replica's memory ⇒
    crashed”, but the 2026-08-01 evaporation family proved a task can be
    ALIVE while unregistered — and each flip then corrupts a live turn's row
    to 'interrupted' (measured 2× in one afternoon). A still-growing event
    log (latest row < 120s old) is positive proof of life. Probe failure
    fails SAFE to the legacy verdict (gate False), preserving Epic C
    crash-recovery semantics when the oracle is unavailable.
    """
    try:
        from lib.tasks_pkg.event_log import latest_event_ts as _let
        _mx = _let(task_id)
        return bool(_mx) and (int(time.time() * 1000) - _mx) < 120_000
    except Exception as _fe:
        logger.debug('[Chat] Poll %s — freshness probe failed: %s',
                     task_id[:8], _fe)
        return False


@api_v1_chat_bp.route('/api/v1/chat/poll/<task_id>', methods=['GET'], endpoint='ui_chat_poll')
@require_scope('chat')
def chat_poll(task_id):
    """Poll a task by id; returns the live in-memory task or its DB checkpoint.

    Memory hit returns the full live task dict; DB hit deserialises the
    persisted ``task_results`` row. Tasks whose DB row says ``status='running'``
    but which are absent from memory are reported as ``status='interrupted'``
    (server crashed mid-task) so the frontend stops polling and recovers the
    partial content.
    """
    with tasks_lock:
        task = tasks.get(task_id)

    if task:
        content_len = len(task.get('content') or '')
        thinking_len = len(task.get('thinking') or '')
        finish_reason = task.get('finishReason') or '?'
        model = task.get('model') or '?'
        logger.debug('[Chat] Poll %s from memory — status=%s content=%dchars thinking=%dchars '
                     'finishReason=%s model=%s error=%s',
                     task_id[:8], task['status'], content_len, thinking_len,
                     finish_reason, model, bool(task.get('error')))
        if task['status'] == 'done' and content_len == 0 and thinking_len == 0 and not task.get('error'):
            logger.warning('[Chat] Poll %s ⚠️ RETURNING EMPTY RESULT — task is done but has no content or thinking! '
                          'finishReason=%s model=%s',
                          task_id[:8], finish_reason, model)
        # pt_8dc03017 cutover: the `_autopilot_deciding` withhold is gone —
        # a done task is terminal; the VU runs as an independent task.
        _reported_status = task['status']
        r = {
            'id': task['id'], 'status': _reported_status,
            'content': task['content'], 'thinking': task['thinking'],
        }
        # ★ Server-authoritative task start (ms). Lets the frontend seed its
        #   elapsed timer from the REAL start on a reconnect/refresh instead of
        #   restarting from 0 — see health_stream_timer.js::_seedStreamTimerStart.
        _created = task.get('created_at')
        if _created:
            r['createdAt'] = int(_created * 1000)
        # Field list MUST mirror chat_poll's DB-path loop and
        # _extract_task_meta. See _extract_task_meta docstring.
        # ★ Terminal fields are withheld until the REPORTED status is terminal
        #   (see _TERMINAL_ONLY_KEYS above) so a running task can never ship a
        #   `finishReason` — the contradiction that minted duplicate bubbles.
        #   Keyed on `_reported_status` (the value actually shipped) so the
        #   status and the terminal fields cannot disagree.
        _terminal_ok = _is_terminal_status(_reported_status)
        for key in ('error', 'toolRounds', 'finishReason', 'usage', 'preset',
                     'toolSummary', 'phase', 'modifiedFiles', 'modifiedFileList',
                     'model', 'provider_id', 'thinkingDepth', 'apiRounds',
                     'compactionUsage'):
            if key in _TERMINAL_ONLY_KEYS and not _terminal_ok:
                continue
            if task.get(key):
                r[key] = task[key]
        if task.get('id'):
            r['taskId'] = task['id']
        if task.get('_fallback_model'):
            r['fallbackModel'] = task['_fallback_model']
        if task.get('_fallback_from'):
            r['fallbackFrom'] = task['_fallback_from']
        if task.get('_fallback_reason'):
            r['fallbackReason'] = task['_fallback_reason']
        if task.get('_fallback_kind'):
            r['fallbackKind'] = task['_fallback_kind']
        # ★ Memory prefetch indicator (persists through poll fallback + reload)
        if task.get('_memoryPrefetch'):
            r['memoryPrefetch'] = task['_memoryPrefetch']
        # ★ Preferences-applied chip (persists through poll fallback + reload)
        if task.get('_preferencesApplied'):
            r['preferencesApplied'] = task['_preferencesApplied']
        # ★ Related-conversations chip (persists through poll fallback + reload)
        if task.get('_relatedConversations'):
            r['relatedConversations'] = task['_relatedConversations']
        # ★ Preferences-learned moment(s) (persist through poll + reload)
        if task.get('_preferencesLearned'):
            r['preferencesLearned'] = task['_preferencesLearned']
        # ★ Inbox-inject sidecars (swarm/peer/user-steer) — persist through poll
        #   fallback + reload so the in-timeline inject chips repaint.
        if task.get('_inboxInjects'):
            r['inboxInjects'] = task['_inboxInjects']
        if task.get('_peerInjects'):
            r['peerInjects'] = task['_peerInjects']
        if task.get('_userSteerInjects'):
            r['userSteerInjects'] = task['_userSteerInjects']
        # pt_8dc03017 cutover: the autopilot baton no longer rides the done /
        # poll. The successor is discovered via the conv→latest-task supersede
        # index (the follow-up registers under the real convId), so there is no
        # `autopilotNextTaskId`/`autopilotVuMessage` to mirror here.
        # ★ Include endpoint turns for endpoint mode tasks so _pollFallback
        #   can reconstruct the full multi-turn conversation.  Also surface
        #   the same authoritative terminal signals the SSE state snapshot
        #   carries (endpointPhase / endpointStopReason / endpointIteration)
        #   so BOTH transports hand the frontend the identical baton — the
        #   poll path can then suppress ghost-worker creation after Critic
        #   STOP exactly like the SSE state handler does.
        if task.get('endpoint_mode'):
            r['endpointMode'] = True
            if task.get('_endpoint_turns'):
                r['endpointTurns'] = task['_endpoint_turns']
            r['endpointPhase'] = task.get('_endpoint_phase', 'planning')
            r['endpointIteration'] = task.get('_endpoint_iteration', 0)
            if task.get('_endpoint_stop_reason'):
                r['endpointStopReason'] = task['_endpoint_stop_reason']
        return api_ok(_maybe_trim_incr_poll(r, task))

    logger.debug('[Chat] Poll %s — not in memory, checking DB', task_id[:8])
    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT task_id,conv_id,content,thinking,error,status,tool_rounds,metadata FROM task_results WHERE task_id=?',
        (task_id,)
    ).fetchone()
    if row:
        _db_content_len = len(row['content'] or '')
        _db_thinking_len = len(row['thinking'] or '')
        _db_meta = _extract_db_meta(row)
        _db_finish = _db_meta.get('finishReason', '?')
        _db_model = _db_meta.get('model', '?')
        # ★ If the DB has status='running' but the task is NOT in memory,
        #   the server crashed/restarted mid-task. Mark it as 'interrupted'
        #   so the frontend stops polling and recovers the partial content.
        effective_status = row['status']
        _reconnect_hint = False
        if effective_status == 'running':
            # ★ Epic C (§4.1 / §6.4): a running checkpoint absent from THIS
            #   replica's memory means either (a) single-process crash, or
            #   (b) multi-replica — the task is alive on ANOTHER replica and
            #   the poll landed here via a stale/misrouted request. We do NOT
            #   run a cross-replica liveness probe (ratified §6.4). Instead:
            #   under the sharded (redis) backend, report status='running' +
            #   reconnect=True so the client re-routes to the owning replica via
            #   taskId affinity (NOT the false 'interrupted' that would strand a
            #   live task). Under the single-process (inproc) backend, absent
            #   genuinely means crashed → keep today's 'interrupted'
            #   crash-recovery behaviour byte-identical.
            _sharded = False
            try:
                from lib.env_compat import getenv_compat as _ge
                _sharded = (_ge('TOFU_RUNTIME_STATE_BACKEND') or 'inproc').strip().lower() == 'redis'
            except Exception as _e_be:
                logger.debug('[Chat] backend probe failed: %s', _e_be)
            # ★ ③-2 (pt_a21cd6eb): liveness gate BEFORE the absent=crashed
            #   flip. The flip assumes “not in THIS replica's memory ⇒ crashed”,
            #   but the 2026-08-01 evaporation family proved a task can be
            #   ALIVE while unregistered — and each flip then corrupts a live
            #   turn's row to 'interrupted' (measured 2× in one afternoon).
            #   A still-growing event log is positive proof of life: report
            #   running+reconnect (same shape as the sharded verdict) instead.
            _fresh_gate = _live_unregistered_gate(task_id)
            if _fresh_gate and not _sharded:
                logger.warning('[Chat] Poll %s — absent from memory but event '
                               'log is FRESH (<120s old) — live-but-unregistered; '
                               'reporting running+reconnect, NOT flipping to '
                               'interrupted', task_id[:8])
                effective_status = 'running'
                _reconnect_hint = True
            else:
                _verdict_status, _reconnect_hint = _running_checkpoint_verdict(_sharded)
                effective_status = _verdict_status
                if _reconnect_hint:
                    logger.info('[Chat] Poll %s — running checkpoint absent locally under sharded backend; '
                                'reporting running+reconnect (task likely on another replica — affinity re-route). '
                                '%dchars content, %dchars thinking.',
                                task_id[:8], _db_content_len, _db_thinking_len)
                    # Do NOT flip the DB to interrupted — the task is (probably)
                    # alive on its owning replica; flipping would corrupt its state.
                else:
                    logger.warning('[Chat] Poll %s — found stale checkpoint (status=running) in DB but task is NOT in memory. '
                                   'Server likely crashed mid-task. Returning status=interrupted with %dchars content, %dchars thinking.',
                                   task_id[:8], _db_content_len, _db_thinking_len)
                    # effective_status already 'interrupted' from the verdict.
                    # ★ Update DB so future polls don't re-trigger this warning
                    try:
                        db.execute("UPDATE task_results SET status='interrupted' WHERE task_id=?", (task_id,))
                        db.commit()
                    except Exception as e:
                        logger.warning('[Chat] Failed to update stale task %s to interrupted: %s', task_id[:8], e)
                    # ★ P0 observability: surface the activeTaskId ↔ msg _taskId
                    #   desync behind an empty finish-bar (no persisted metadata).
                    _log_poll_task_id_mismatch(db, row['conv_id'], task_id, _db_meta)
        else:
            logger.debug('[Chat] Poll %s from DB — status=%s content=%dchars thinking=%dchars '
                         'finishReason=%s model=%s error=%s',
                         task_id[:8], row['status'], _db_content_len, _db_thinking_len,
                         _db_finish, _db_model, bool(row['error']))
        # ★ Close the 5s cold-replay window on the POLL fallback too (the
        #   sse_poll_fallback.js path): the task is not in memory, so row
        #   content/thinking is the (up to 5s stale) task_results checkpoint.
        #   Fold the lossless per-delta task_events log — identical to the two
        #   SSE cold emits above — so a poll-fallback reconnect mid-stream sees
        #   the full buffer, not a short checkpoint. Falls back to the row pair
        #   on an empty/failed log.
        from lib.tasks_pkg.event_fold import fold_cold_state_text
        _poll_c, _poll_t = fold_cold_state_text(
            task_id, row['content'] or '', row['thinking'] or '')
        r = {
            'id': row['task_id'], 'status': effective_status,
            'content': _poll_c, 'thinking': _poll_t,
        }
        if _reconnect_hint:
            # Tell the client to re-open the stream (it will land on the
            # owning replica via taskId affinity) rather than treat this as a
            # terminal interrupted state.
            r['reconnect'] = True

        if row['error']:
            from lib.error_envelope import from_json as _err_from_json
            r['error'] = _err_from_json(row['error'])
        if row['tool_rounds']:
            try:
                r['toolRounds'] = _loads_yielding(row['tool_rounds'])
            except (json.JSONDecodeError, ValueError, TypeError) as e:
                logger.warning('[Chat] Failed to parse tool_rounds in poll for task %s: %s', task_id, e, exc_info=True)
        else:
            from lib.tasks_pkg import load_tool_rounds_from_conversation
            _tr = load_tool_rounds_from_conversation(row['conv_id'])
            if _tr:
                r['toolRounds'] = _tr
        # Field list MUST mirror chat_poll's in-memory loop and
        # _extract_task_meta. provider_id was previously dropped here
        # even though persist_task_result writes it into meta_json,
        # silently round-tripping through the DB.
        # ★ Same terminal-field gate as the in-memory branch: under the sharded
        #   reconnect verdict `effective_status` stays 'running' while the
        #   persisted meta may already carry a finishReason, so gating only the
        #   other branch would leave the identical contradiction reachable here.
        _db_terminal_ok = _is_terminal_status(effective_status)
        for key in ('finishReason', 'usage', 'preset', 'toolSummary',
                     'model', 'provider_id', 'thinkingDepth', 'apiRounds',
                     'modifiedFiles', 'modifiedFileList'):
            if key in _TERMINAL_ONLY_KEYS and not _db_terminal_ok:
                continue
            if _db_meta.get(key):
                r[key] = _db_meta[key]
        # ★ Endpoint mode: the in-memory task that held _endpoint_turns has
        #   been evicted (past TTL) or lost to a server restart, so reconstruct
        #   the multi-turn structure from the durable conversation messages
        #   (the authoritative store — endpoint.py syncs every turn there).
        #   Without this, a poll-fallback that outlives the in-memory task
        #   returns no endpointMode → the frontend overwrites the multi-turn
        #   endpoint render with the last single-turn content blob, a
        #   display-state desync that only a manual refresh repaired.  Covers
        #   BOTH done and interrupted statuses (the interrupted server-crash
        #   path is exactly when this reconstruction matters most).
        if _db_meta.get('endpointMode'):
            r['endpointMode'] = True
            from lib.tasks_pkg import load_endpoint_turns_from_conversation
            _ep_turns = load_endpoint_turns_from_conversation(row['conv_id'])
            if _ep_turns:
                r['endpointTurns'] = _ep_turns
            if _db_meta.get('endpointStopReason'):
                r['endpointStopReason'] = _db_meta['endpointStopReason']
        if _db_meta.get('fallbackModel'):
            r['fallbackModel'] = _db_meta['fallbackModel']
            r['fallbackFrom'] = _db_meta.get('fallbackFrom', '')
            if _db_meta.get('fallbackReason'):
                r['fallbackReason'] = _db_meta['fallbackReason']
            if _db_meta.get('fallbackKind'):
                r['fallbackKind'] = _db_meta['fallbackKind']
        return api_ok(r)

    logger.warning('[Chat] Poll %s — NOT FOUND in memory or DB! Task may have been cleaned up. '
                   'Client will receive 404 and may lose accumulated content.',
                   task_id[:8])
    return api_not_found('Task not found')


@api_v1_chat_bp.route('/api/v1/chat/flow-trace/<task_id>', methods=['GET'],
                      endpoint='ui_chat_flow_trace')
@require_scope('chat')
def chat_flow_trace(task_id):
    """Return the per-node run trace for an orchestration-flow chat task.

    The trace is the traceability record FlowExecutor accumulates: one entry
    per executed node carrying the RESOLVED delegation brief (the rendered
    role prompt — "what is this role doing?"), a bounded copy of its effective
    input context, its full bounded output, the message axis / isolation /
    loop iteration, and deliverable counts + timing. Powers the Studio
    canvas/inspector overlay.

    Served from the live in-memory task first (mid-run / just-finished), then
    the persisted ``task_results.metadata.flowTrace`` (survives reload /
    restart). Returns ``{ok, taskId, flowLabel, trace: [...]}``.
    """
    with tasks_lock:
        task = tasks.get(task_id)
    if task is not None:
        return api_ok({
            'taskId': task_id,
            'flowLabel': task.get('_flow_label', ''),
            'trace': task.get('_flow_trace') or [],
        })

    db = get_db(DOMAIN_CHAT)
    row = db.execute(
        'SELECT metadata FROM task_results WHERE task_id=?', (task_id,)
    ).fetchone()
    if row and row['metadata']:
        try:
            meta = json.loads(row['metadata'])
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning('[Chat] flow-trace %s — metadata parse failed: %s',
                           task_id[:8], e)
            meta = {}
        return api_ok({
            'taskId': task_id,
            'flowLabel': meta.get('flowLabel', ''),
            'trace': meta.get('flowTrace') or [],
        })
    return api_not_found('Task not found')


