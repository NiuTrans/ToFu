"""Autopilot mode — virtual user that auto-replies when the LLM stops.

When the model would normally hand control back to the user (either by
calling ``ask_human`` or by emitting a final assistant message with
``finish_reason='stop'``), Autopilot runs a one-shot LLM as the *user*
and feeds its reply back to the orchestrator as a brand-new turn.

Design constraints (locked in by the user, do not relax silently):

  • **Runs BEFORE the ``done`` SSE event.**  The hook fires inside
    ``_finalize_and_emit_done`` after the post-loop work but *before*
    ``append_event(done_evt)`` / ``persist_task_result``.  This lets
    the ``done`` event carry ``autopilotNextTaskId`` +
    ``autopilotVuMessage`` so the frontend attaches to the follow-up
    task directly instead of polling ``/api/chat/active`` after the
    SSE stream has already closed.  (Earlier design ran autopilot
    *after* persist; the SSE pipe was closed by the time the VU
    finished, so the synthetic user msg was invisible until manual
    refresh.)

  • **Independent of endpoint mode.**  Autopilot and endpoint mode are
    mutually exclusive — both share the same termination boundary
    ("the model stopped") so running them together would double-loop.
    The frontend hides one toggle when the other is on; this module
    additionally bails out when ``task['_endpoint_managed']`` is set.

  • **Reuse the conversation's main model.**  No separate VU model.

  • **Same tools as the worker.**  The VU runs through the full
    orchestrator (``_run_single_turn``) so it has access to every tool
    the parent task has — read_files, search, project edits, browser,
    memory, MCP, etc.  This lets the simulated user investigate before
    composing its reply (e.g. "let me check the file the assistant
    referenced before answering").  Inherited from
    ``task['config']`` verbatim — same tool list as
    ``_assemble_tool_list`` would build for the parent.

  • **Role override via a trailing directive turn**, mirroring how
    endpoint-mode's planner/critic announce their role.  We do NOT
    role-swap the conversation history: the LLM sees the real
    conversation and a final user-turn that says "for THIS turn play
    the simulated user".  Prefix-cache-friendly and avoids the
    swapped-history confusion with the orchestrator's injected
    system prompt.

  • **Full conversation passed through.**  We do NOT trim history
    here; the orchestrator's compaction layer (run_compaction_pipeline)
    handles bounding.  This keeps tool_calls / tool_result pairs
    contiguous and removes one place where context choices can drift
    between the worker and the simulated user.

  • **No turn cap, no state-change watchdog.**  The only graceful stop
    signal is the VU itself emitting ``[VU: TASK_DONE]``.  Other stops
    are: real-user abort, real-user sending a new message (handled
    automatically by ``abort_running_tasks_for_conv``), an error path,
    or the queue having a real queued message waiting (deferred to).

  • **Empty VU output does NOT stop the loop.**  An empty reply is
    treated as a valid "yeah, keep going" — the orchestrator just
    starts a fresh turn with that empty user message.  This is the
    user's explicit choice — see the design discussion in
    docs/ARCHITECTURE.md if rebooting decisions.

The "don't stop on empty output" rule means the only correctness escape
hatch is the real user clicking Stop or sending a new message.  Both are
already wired through ``task['aborted']`` and the freshness guard in
``manager._conv_latest_task``, so we don't need extra plumbing here.
"""

from __future__ import annotations

import hashlib
import threading
import time
import uuid

from lib.agent_core.events import EventType, build_event
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


from lib.agent_verdict import VU_DONE_SENTINEL as _VU_DONE_SENTINEL
from lib.agent_verdict import VU_ROLE_PROMPT as _VU_ROLE_PROMPT
from lib.agent_verdict import classify_verdict as _classify_verdict
from lib.agent_verdict import strip_machine_tokens as _strip_machine_tokens


# ``_VU_ROLE_PROMPT`` is the SINGLE-SOURCE virtual-user persona, defined once
# in ``lib.agent_verdict._handoff.VU_ROLE_PROMPT`` and imported here (the live
# standalone loop) AND by ``lib.swarm.registry`` (the FlowExecutor engine
# path).  It was previously an inline ~2000-char copy in this module that the
# registry paraphrased and drifted from; consolidating it kills that
# hand-copy.  The module-level ``_VU_ROLE_PROMPT`` name is retained (existing
# call sites + tests reference it) as an alias so nothing else changes, and
# ``VU_PROMPT_VERSION`` below still hashes the identical byte string.


# Content-derived prompt version marker.  Stamped into every VU directive turn
# so a stale-vs-live prompt mismatch is mechanically detectable (one glance at
# the directive text / message dict) instead of relying on eyeballing the prose.
# Derived from the prompt body itself, so it changes AUTOMATICALLY whenever the
# prompt text changes — no one can forget to bump it.  A directive carrying an
# old marker (or none) was produced by a server still running a pre-edit
# import-time constant; restart picks up the new prompt.  See
# tests/test_autopilot_verify.py for the regression assertion.
VU_PROMPT_VERSION = hashlib.sha256(
    _VU_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]

# Emit the loaded prompt version once at import so the RUNNING process's
# prompt is greppable in app.log without needing a directive paste to infer
# it ("is the live process current?" answered directly from logs).
logger.info('[Autopilot] VU prompt v%s loaded', VU_PROMPT_VERSION)


# ── pt_00459503 slice 1 — extracted state helpers ──────────────────
#
# The objective/run-id/budget cluster moved to
# ``lib/tasks_pkg/autopilot_state.py`` (see
# docs/AUTOPILOT_DECOMPOSITION_AUDIT.md).  Re-exported here as
# module-level attributes so every existing ``from lib.tasks_pkg.autopilot
# import _extract_objective`` (and the sibling tests that
# ``monkeypatch.setattr(ap, '_get_or_persist_...', ...)``) keep working
# byte-identically. Symbol IDENTITY is preserved (facade attr IS the
# state-module attr) — the load-bearing invariant for monkeypatch
# steering, verified by
# tests/test_autopilot_state_extraction_wire_parity.py.
from lib.tasks_pkg.autopilot_state import (  # noqa: E402
    _VU_HISTORY_CAP,  # noqa: F401  (re-export facade attr)
    _PROGRESS_LEDGER_CAP,  # noqa: F401  (re-export facade attr)
    _extract_objective,  # noqa: F401  (re-export facade attr)
    _extract_objective_from_db,  # noqa: F401  (re-export facade attr)
    _get_or_persist_objective,
    _get_or_persist_run_id,
    _record_vu_turn_and_check_budget,
    _clear_run_id,
    _resolve_recent_run_id,  # noqa: F401  (re-export facade attr)
    _resolve_run_anchor_msgid,  # noqa: F401  (re-export facade attr)
)
# ── pt_00459503 slice 3 — extracted run close-out cluster ────────────
#
# The four run-close-out functions (_store_run_record, _emit_run_concluded,
# conclude_run, _emit_run_concluded_event) moved to
# ``lib/tasks_pkg/autopilot_run_lifecycle.py`` — a LEAF module with zero
# imports from this file. Re-exported here as module-level attributes so
# every existing ``from lib.tasks_pkg.autopilot import conclude_run`` (and
# the sibling tests that ``monkeypatch.setattr(ap, 'conclude_run', ...)``)
# keep working byte-identically. Symbol IDENTITY is preserved (facade attr
# IS the leaf-module attr) — the load-bearing invariant for monkeypatch
# steering.  This slice ALSO closes the two-module cycle that slice 2
# guarded via a lazy import (autopilot_markers ↔ autopilot); with the
# close-out helpers now in a leaf module, autopilot_markers imports
# ``conclude_run`` at MODULE TOP and no cycle exists.
from lib.tasks_pkg.autopilot_run_lifecycle import (  # noqa: E402
    _store_run_record,
    _emit_run_concluded,  # noqa: F401  (re-export facade attr)
    conclude_run,  # noqa: F401  (re-export facade attr)
    _emit_run_concluded_event,
)


def is_autopilot_enabled(task: dict) -> bool:
    """True iff autopilot is active for this task AND endpoint mode is not.

    Autopilot is "active" when EITHER:
      • ``config['autopilot']`` is set (config-driven — toggle was ON at the
        real send, propagated into the task and its follow-ups), OR
      • a persistent autopilot armed-marker exists for the conversation
        (the mid-stream / idle "arm" gesture; survives page reload and is
        cancellable from the queue bar).

    Endpoint mode wins the mutual exclusion (both share the same
    "model stopped" boundary).  The VU sub-task (``_vu_subtask``) and
    inline tasks never consult the marker — only DB-backed parent/follow-up
    tasks do, so the cheap config flag covers the hot recursion guard.
    """
    cfg = task.get('config') or {}
    if cfg.get('endpointMode') or task.get('_endpoint_managed'):
        return False
    if cfg.get('autopilot'):
        return True
    # Persistent armed-marker fallback (mid-stream arm / reload survival).
    if task.get('_vu_subtask') or task.get('_inline_messages'):
        return False
    conv_id = task.get('convId') or ''
    if not conv_id:
        return False
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] marker probe failed (non-fatal): %s', e)
        return False


# ── pt_00459503 slice 5 — extracted VU event-forwarding cluster ─────
#
# ``_VU_FORWARD_TYPES`` / ``make_vu_event_transform`` /
# ``_VU_LIFECYCLE_TYPES`` / ``_emit_vu_setup_phase`` live in the LEAF
# ``lib.tasks_pkg.autopilot_event_forwarding`` (zero back-imports from
# this file).  Re-exported here as module-level attributes so every
# existing ``from lib.tasks_pkg.autopilot import _emit_vu_setup_phase``
# (and the sibling tests that
# ``monkeypatch.setattr(ap, '_emit_vu_setup_phase', ...)`` —
# tests/test_autopilot_warmup_setup_phase.py) keeps working
# byte-identically.  Symbol IDENTITY is preserved (facade attr IS the
# leaf-module attr) — the load-bearing invariant for monkeypatch
# steering, verified by
# tests/test_autopilot_event_forwarding_wire_parity.py.
# (2026-07-26: the list-subclass ``_VUEventForwarder`` was replaced by
# ``make_vu_event_transform`` — the carrier's own stream now carries the
# full VU contract, see the leaf docstring.)
from lib.tasks_pkg.autopilot_event_forwarding import (  # noqa: E402
    _VU_FORWARD_TYPES,  # noqa: F401  (re-export facade attr)
    _VU_LIFECYCLE_TYPES,  # noqa: F401  (re-export facade attr)
    make_vu_event_transform,
    _emit_vu_setup_phase,
)


# ── VU carrier stream contract (2026-07-26, conv ms1rrjchpa5pqw) ────
#
# After the pt_8dc03017 cutover the client HOPS from the parent's closed
# stream to the VU carrier's own stream (``latestLiveTaskId``).  These
# three helpers are the carrier-side wiring that makes the carrier's own
# stream carry the SAME VU contract the parent stream always did —
# fixing the second-"Agent"-bubble / visible machine sentinels /
# never-ending-回答中 triad the raw sub-task stream produced.


def _install_vu_carrier_contract(parent_task: dict, sub_task: dict,
                                 vu_msg_id: str) -> None:
    """Wire the VU carrier sub-task's own stream to the full VU contract.

    Called right after ``create_task`` in ``run_virtual_user``.  Three
    facts the rest of the machine relies on:

      * ``_vu_event_transform`` — the ``append_event`` facade seam shapes
        every frame the carrier emits (wrapped ``autopilot_vu_event``,
        verbatim lifecycle frames, everything else dropped) so the
        carrier's own stream / push channel / persisted event log all
        carry the SAME envelope the parent stream does.
      * ``_vu_msg_id`` — pinned on the task so the connect-snapshot
        builder (``chat_dispatch.build_connect_snapshot``) can name the
        VU bubble without re-deriving it.
      * ``_vu_carrier`` on the PARENT — lets ``_emit_vu_lifecycle_frame``
        find the carrier for the dual-emit, and lets
        ``_close_vu_carrier_stream`` flip it terminal at run end.

    Also seeds ``autopilot_vu_start`` onto the carrier's own stream —
    the spine its persisted event log replays for any late/cold reader.
    """
    from lib.tasks_pkg.manager import append_event as _append_evt
    sub_task['_vu_event_transform'] = make_vu_event_transform(
        parent_task, vu_msg_id or '')
    sub_task['_vu_msg_id'] = vu_msg_id or ''
    parent_task['_vu_carrier'] = sub_task
    try:
        _append_evt(sub_task, build_event(
            EventType.AUTOPILOT_VU_START, vuMsgId=vu_msg_id or ''))
    except Exception as e:
        logger.debug('[Autopilot %s] carrier vu_start seed failed: %s',
                     parent_task.get('id', '?')[:8], e)


def _emit_vu_lifecycle_frame(task: dict, event: dict) -> None:
    """Dual-emit a VU lifecycle frame onto parent stream + carrier stream.

    Post-cutover the client may be attached to EITHER stream (the
    parent's, during the pre-hop window; the carrier's, after the
    supersede hop).  Both must carry the identical lifecycle fact — a
    missed ``autopilot_vu_cancel`` leaves a live ghost bubble, a missed
    ``autopilot_vu_done`` leaves the VU turn unfinalized.  The carrier
    copy lands via the transform's lifecycle passthrough (verbatim,
    never double-wrapped).  Best-effort: either leg failing leaves the
    other intact.
    """
    from lib.tasks_pkg.manager import append_event as _append_evt
    tid = task.get('id', '?')[:8]
    try:
        _append_evt(task, event)
    except Exception as e:
        logger.debug('[Autopilot %s] lifecycle emit to parent failed: %s',
                     tid, e)
    carrier = task.get('_vu_carrier')
    if carrier is not None and carrier is not task:
        try:
            _append_evt(carrier, event)
        except Exception as e:
            logger.debug('[Autopilot %s] lifecycle emit to carrier failed: %s',
                         tid, e)


def _close_vu_carrier_stream(task: dict) -> None:
    """Flip the VU carrier terminal so its SSE stream closes.

    Carrier-scoped backstop (owner's ruling 2026-07-26): the
    endpoint-managed finalize neither flips ``status`` nor emits
    ``done``, so without this the carrier's stream keepalives forever
    and the sidebar shows 回答中 indefinitely.  The flip runs ONLY here
    — at ``maybe_run_autopilot``'s exit, AFTER the terminal lifecycle
    frame was appended — never inside ``_run_single_turn`` (endpoint
    shares that helper across worker/critic turns on ONE task dict; a
    mid-loop terminal flip would synthesize a premature late_done on
    endpoint streams).  The tick's carrier branch then synthesizes the
    minimal closing done, stamping any already-registered successor
    (the autopilot follow-up worker) so the client hops on.
    """
    carrier = task.pop('_vu_carrier', None)
    if carrier is None:
        return
    try:
        carrier['status'] = 'done'
    except Exception as e:
        logger.debug('[Autopilot %s] carrier terminal flip failed: %s',
                     task.get('id', '?')[:8], e)


def run_virtual_user(task: dict, vu_msg_id: str | None = None) -> dict | None:
    """Run the VU LLM (with full tools) and return its reply + investigation.

    The VU runs as a fresh sub-task through the orchestrator's
    ``_run_single_turn``, inheriting the parent task's config so the
    same tools (read_files, search, project edits, memory, MCP, …)
    are available.  The trailing directive user-turn announces the
    "simulated user" role for THIS turn only — the conversation
    history itself is not role-swapped.

    Returns ``{'text': str, 'rounds': list}`` on success, where
    ``rounds`` is the VU sub-task's tool round history (suitable for
    attaching to the persisted synthetic user message so the user can
    see what Autopilot probed).  Returns ``None`` when the loop should
    stop — either because the VU emitted ``[VU: TASK_DONE]``, the
    sub-task failed, or the parent task was aborted while the VU was
    thinking.  An empty ``text`` is a valid "keep going" reply.
    """
    tid = task['id'][:8]
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted', tid)
        return None

    parent_messages = task.get('messages') or []
    if not parent_messages:
        logger.warning('[Autopilot %s] No messages — stopping', tid)
        return None

    # Append the role-override directive as a trailing user turn —
    # same pattern as endpoint_review._run_planner_turn / _run_critic_turn.
    # We pass the parent's full message list verbatim so the VU sees the
    # entire conversation (including tool_calls / tool_result pairs);
    # the orchestrator's compaction layer handles context bounding.
    # Resolve the immutable objective anchor (north star).  Pinned to
    # settings.autopilotObjective so it survives across follow-up tasks and
    # compaction; falls back to deriving from the live messages.
    # ★ Attribute the (silent, up to tens of seconds) pre-stream window so the
    #   VU bubble names what's blocking instead of a bare "Autopilot…".
    _emit_vu_setup_phase(task, vu_msg_id, 'Autopilot：核对助手回答、确定下一步…')
    objective = _get_or_persist_objective(task.get('convId') or '',
                                           parent_messages)
    objective_block = ''
    if objective:
        objective_block = (
            '=== ORIGINAL OBJECTIVE (your north star — does NOT change '
            'across turns) ===\n'
            f'{objective}\n'
            '=== The assistant works for YOU toward this objective. '
            'Hold it to this, not to its own self-report. ===\n\n'
        )

    vu_messages = [dict(m) for m in parent_messages]
    vu_messages.append({
        'role': 'user',
        'content': (
            f'{objective_block}'
            f'=== Your role for THIS turn: Simulated User '
            f'[prompt v{VU_PROMPT_VERSION}] ===\n'
            f'{_VU_ROLE_PROMPT}\n'
            '=== End simulated-user role ===\n\n'
            'Based on the conversation above, produce the simulated '
            'user\'s reply now.  Verify the assistant\'s key claims with '
            'tools first, then output the reply text only.'
        ),
        '_isVuDirective': True,
        '_vuPromptVersion': VU_PROMPT_VERSION,
    })

    # Build a fresh sub-task that inherits the parent's config so
    # _assemble_tool_list constructs the same tool list the worker had.
    # pt_8dc03017 cutover — the VU sub-task now registers under the REAL
    # convId (dropping the historical convId='' opt-out). This satisfies
    # the HB-1 happens-before (design §4.1): _record_latest_task advances
    # to the VU BEFORE the parent's done event is emitted, so a client
    # reacting to end-of-turn observes the successor already in the
    # supersede index and can attach transport-agnostically. ``_vu_subtask``
    # still marks it as a CARRIER (invisible to /api/chat/active, the
    # restart guard, and the sidebar's snapshot_running_by_conv — same
    # is_carrier_task predicate), so no phantom sidebar dot lights up.
    # ``_inline_messages=True`` keeps it out of the conv DB sync path;
    # ``_endpoint_managed=True`` suppresses the orchestrator's done event
    # + autopilot recursion.
    _emit_vu_setup_phase(task, vu_msg_id, 'Autopilot：整理对话上下文，准备生成回复…')
    from lib.tasks_pkg import create_task
    from lib.tasks_pkg.orchestrator import _run_single_turn

    sub_cfg = dict(task.get('config') or {})
    # Strip checkpoint/continue flags so the sub-task starts clean.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
    ):
        sub_cfg.pop(stale_key, None)
    # Endpoint mode is gated by is_autopilot_enabled but be defensive —
    # the sub-task must never re-enter endpoint mode.
    sub_cfg['endpointMode'] = False
    # Autopilot must NOT recurse (the parent's hook already runs us).
    sub_cfg['autopilot'] = False
    # Disable ask_human for the simulated user — the VU IS the user, so
    # asking another human makes no sense and would block forever (the
    # in-handler autopilot fallback is gated on cfg.autopilot which we
    # just turned off above).
    sub_cfg['humanGuidanceEnabled'] = False

    # pt_8dc03017 HB-1: register under real convId with supersede=False so
    # create_task does NOT run abort_running_tasks_for_conv (the parent is
    # already status='done' at this point per _finalize.py:747, so it would
    # not be swept anyway, but a concurrent conv-scoped operation could
    # be — being explicit is safer). We then advance the supersede index
    # ourselves so _latest_task_for_conv(convId) == the VU task by the time
    # the parent's `done` event is appended (§4.1 HB-1). The client's
    # transport-agnostic attach reducer discovers the VU here.
    _vu_conv_id = task.get('convId') or ''
    sub_task = create_task(_vu_conv_id, vu_messages, sub_cfg, supersede=False)
    sub_task['_inline_messages'] = True
    sub_task['_vu_subtask'] = True
    sub_task['_autopilotParent'] = task.get('id', '')
    # Back-pointer for the parent's conv-sync freshness guard: the carrier
    # claims the conv→latest index BEFORE the parent's done (HB-1), so the
    # parent's trailing sync must recognise "superseded by own VU carrier"
    # as the DESIGNED handoff, not an unexpected replacement. A plain field
    # (not a registry lookup) because the carrier is discarded from the
    # registry before the parent's trailing persist runs.
    task['_vu_carrier_id'] = sub_task['id']
    if _vu_conv_id:
        # HB-1: advance the conv→latest-task index to the VU BEFORE returning
        # to _finalize.py (which then emits the parent's done event). This is
        # the exactly-once ordering that replaces the withheld baton.
        from lib.tasks_pkg.manager import _record_latest_task as _rlt
        _rlt(_vu_conv_id, sub_task['id'])
    # Turn-ctx capsule anchor: the VU sub-task's DONE frame flows through
    # _finalize_and_emit_done and would otherwise carry an empty userMsgId
    # (frontend fallback → "last user in conv" = the VU-synthesised user,
    # which is WRONG — the fact card belongs to the PARENT user turn). Inherit
    # the parent's stable _userMsgId so the reconcile lands on the parent.
    # This is a diagnostic id only — no baton / exactly-once semantics.
    if task.get('_userMsgId'):
        sub_task['_userMsgId'] = task['_userMsgId']
    # ── Peer-message fast path (Pillar #6) ──
    #   The VU sub-task now runs under the parent convId (pt_8dc03017 HB-1),
    #   so its natural swarm/inbox key IS the parent conv id. Keep the
    #   explicit drain-key stamp for clarity — matches the endpoint 'big task'
    #   contract (delivers a peer's message at the next round boundary).
    sub_task['_peer_drain_key'] = task.get('convId') or ''

    # Swap the carrier's event channel onto the full VU contract: the
    # transform (consumed by the append_event facade) wraps every
    # forwardable frame as ``autopilot_vu_event`` on BOTH the carrier's
    # own stream AND the parent's stream, passes lifecycle frames
    # verbatim, and drops everything else — so the client that hops onto
    # the carrier stream after the parent's done sees the identical VU
    # bubble contract (labeled "Autopilot", user lane), never a raw
    # agent turn.  Also pins ``_vu_msg_id`` + seeds ``autopilot_vu_start``
    # on the carrier stream.
    _install_vu_carrier_contract(task, sub_task, vu_msg_id)

    # Mirror parent abort onto the sub-task so user-clicked Stop while
    # the VU is mid-tool-loop tears the sub-task down too.  Single
    # threaded poll is fine — the sub-task is short-lived and the
    # orchestrator already polls task['aborted'] each round.
    _stop_mirror = threading.Event()

    def _mirror_abort():
        while not _stop_mirror.is_set():
            if task.get('aborted') and not sub_task.get('aborted'):
                sub_task['aborted'] = True
                sub_task['_abort_timestamp'] = time.time()
                sub_task['_abort_reason'] = 'parent_aborted'
                logger.info('[Autopilot %s] Mirroring parent abort onto '
                            'VU sub-task %s', tid, sub_task['id'][:8])
                return
            _stop_mirror.wait(0.5)

    _mirror_thread = threading.Thread(
        target=_mirror_abort,
        name=f'autopilot-abort-mirror-{tid}',
        daemon=True,
    )
    _mirror_thread.start()

    # Close the creation race: a REAL message that landed BETWEEN
    # maybe_run_autopilot's eligibility check and create_task would
    # otherwise run the WHOLE VU call before the post-call deferral fires
    # (enqueue_message's preemption only sees registered sub-tasks — a row
    # that arrived before create_task is invisible to it). Abort the
    # not-yet-started sub-task; the preemption branch below routes the
    # deferral, so the queued turn starts at the first abort checkpoint.
    if _has_pending_real_message(_vu_conv_id):
        logger.info('[Autopilot %s] Real message landed during VU setup — '
                    'aborting sub-task %s before round 1',
                    tid, sub_task['id'][:8])
        sub_task['aborted'] = True
        sub_task['_abort_timestamp'] = time.time()
        sub_task['_abort_reason'] = 'real_message_preempts_vu'

    try:
        result = _run_single_turn(sub_task)
    except Exception as e:
        logger.warning('[Autopilot %s] VU sub-task raised: %s — '
                       'stopping autopilot for this conv', tid, e,
                       exc_info=True)
        return None
    finally:
        _stop_mirror.set()
        # ★ Lifecycle owner: the VU sub-task runs synchronously under
        #   _endpoint_managed=True, which SUPPRESSES the orchestrator's
        #   terminal-status flip + persist_task_result — so it NEVER reaches
        #   a terminal status on its own. Discard it NOW so it doesn't linger
        #   in the registry (or hold the conv→latest-task index it just
        #   claimed for HB-1) past its synchronous run. discard_task also
        #   clears the _conv_latest_task entry if it still points at us —
        #   the follow-up's create_task will re-advance the index to the
        #   real successor. The local sub_task dict stays valid for the
        #   toolRounds / segment reads below (discard only unregisters it).
        try:
            from lib.tasks_pkg.manager import discard_task
            discard_task(sub_task['id'], conv_id=sub_task.get('convId') or None)
        except Exception as _disc_err:
            logger.debug('[Autopilot %s] VU carrier discard failed: %s',
                         tid, _disc_err)
        # ★ Row settle (pt_8a491f9d): discard_task only unregisters IN MEMORY.
        #   The carrier ran under _endpoint_managed=True, which BY DESIGN
        #   suppresses the orchestrator's terminal-status flip +
        #   persist_task_result — so its per-round checkpoint_task_partial
        #   writes leave a task_results row at status='running' that no
        #   in-memory pass will ever revisit (manager/_maintenance.py:285).
        #   That stale row is the zombie generator the next startup recovery
        #   sweep feeds on (the ms2gipv5 four-bubble incident). Settle it to
        #   a terminal status derived from the carrier's OWN end state, in
        #   the same breath as the registry cleanup.
        try:
            from lib.tasks_pkg.manager import write_carrier_terminal_row
            if sub_task.get('aborted'):
                _carrier_status = 'aborted'
            elif sub_task.get('status') == 'error' or sub_task.get('error'):
                _carrier_status = 'error'
            elif sub_task.get('finishReason'):
                _carrier_status = 'done'
            else:
                # Died before any finish reason (e.g. _run_single_turn raised
                # mid-round) — an honest 'error', never a fake 'done'.
                _carrier_status = 'error'
            write_carrier_terminal_row(sub_task, _carrier_status)
        except Exception as _settle_err:
            logger.warning('[Autopilot %s] VU carrier row settle failed: %s',
                           tid, _settle_err)

    # ── Real-message preemption (owner-ratified 2026-07-25) ──
    # A REAL queued message aborted this VU mid-call (enqueue_message stamps
    # _abort_reason='real_message_preempts_vu'; the pre-flight above stamps
    # the same for the creation race). Return None IMMEDIATELY — never feed
    # the partial reply through the verdict/segment pipeline below, which
    # would manufacture a synthetic user turn out of a corpse.
    # maybe_run_autopilot's None branch emits AUTOPILOT_VU_CANCEL and the
    # completion hook dispatches the queued turn — the whole point of the
    # preemption is that dispatch happens NOW, not after the full VU call.
    # (Second leg is belt-and-braces: any sub-task abort landing while a
    # real message is pending routes the same way.)
    if sub_task.get('_abort_reason') == 'real_message_preempts_vu' or (
            sub_task.get('aborted')
            and _has_pending_real_message(task.get('convId') or '')):
        logger.info('[Autopilot %s] VU sub-task %s preempted by a real queued '
                    'message — deferring immediately (queue dispatch takes over)',
                    tid, sub_task.get('id', '?')[:8])
        return None

    # A PLAIN user Stop lands on the CARRIER, not the parent: while the VU
    # thinks, the client is attached to the carrier stream, so the stop
    # button aborts the sub-task itself. Falling through here reads the
    # corpse's empty content as a valid "keep going" reply — an EMPTY VU
    # row got appended and a follow-up spawned on top of it, forcing the
    # user to stop THAT task too (ms9ow2tt 2026-08-01, 19 convs affected).
    # An aborted sub-task is a failed sub-task: stop the run. The marker
    # stays armed, same semantics as the parent-abort branch below.
    if sub_task.get('aborted'):
        logger.info('[Autopilot %s] VU sub-task %s aborted (user stop) — '
                    'stopping the run', tid, sub_task.get('id', '?')[:8])
        return None

    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted during VU sub-task — stopping', tid)
        return None

    err = result.get('error')
    if err:
        logger.warning('[Autopilot %s] VU sub-task error: %.200s — '
                       'stopping autopilot for this conv', tid, err)
        return None

    text = (result.get('content') or '').strip()

    # ── Canned-greeting guard (2026-07-28 Opus 5 incident) ──
    # The VU runs on the same upstream model as the worker, so when that
    # deployment degenerates the VU's reply is the SAME canned greeting
    # (see lib/tasks_pkg/stream_handler/_canned_greeting.py). Appending it
    # would relay the artifact into the conversation as a synthetic user
    # turn and spawn a follow-up task whose query IS the greeting — the
    # incident's amplification leg (observed: 18 such rows in 10 convs in
    # ~5h, e.g. ms3sahx7cotx3y ords 9/11/13). Stop the run instead: None
    # routes the caller into its normal cancel/concluded path, and the next
    # real event re-arms autopilot. A legitimate VU reply is never a bare
    # opener-greeting, and the directive tail of vu_messages is substantial
    # work text, so the small-talk complement inside the detector never
    # suppresses this guard on a real reply.
    from lib.tasks_pkg.stream_handler._canned_greeting import (
        is_canned_greeting_reply as _is_canned_greeting_reply,
    )
    if _is_canned_greeting_reply(text, vu_messages):
        logger.warning(
            '[Autopilot %s] VU reply is a canned upstream greeting (%r) — '
            'stopping the run instead of relaying the artifact into the '
            'conversation as a user turn', tid, text[:60])
        audit_log('autopilot_vu_canned_greeting',
                  task_id=task.get('id', ''),
                  conv_id=task.get('convId', ''),
                  text=text[:60])
        return None

    rounds = list(sub_task.get('toolRounds') or [])
    # Route the stop decision through the single source of truth.  The
    # virtual_user policy ends the loop only on an explicit TASK_DONE/STOP
    # AND downgrades that to "keep going" when the reply itself still flags
    # unresolved work (❌ / "NOT met" / "still failing" / "unresolved") — the
    # anti-premature-done guard lives in lib/agent_verdict.py, NOT here.
    verdict = _classify_verdict(text, verifier_role='virtual_user')
    if verdict['phase'] == 'stop':
        logger.info('[Autopilot %s] VU emitted TASK_DONE — stopping loop', tid)
        # Signal the hook to clear the persistent armed-marker (disarm) so the
        # loop ends and the queue-bar sentinel disappears.
        task['_vu_emitted_done'] = True
        audit_log('autopilot_stop',
                  task_id=task.get('id', ''),
                  conv_id=task.get('convId', ''),
                  reason='vu_task_done')
        return None

    # The verdict downgraded a premature TASK_DONE to "keep going": the reply
    # may still literally carry the sentinel token.  Strip it so the
    # synthetic user message we feed back is clean instructional text, not a
    # stray sentinel the next turn would mis-read.  PROGRESS lines are KEPT
    # here on purpose: the budget guard (_record_vu_turn_and_check_budget)
    # parses them for the diminishing-returns ledger — the persistence path
    # in maybe_run_autopilot strips them (same predicate) before the text
    # reaches conversation history.
    if _VU_DONE_SENTINEL in text:
        text = _strip_machine_tokens(text, keep=('progress_line',))

    # ── Segment timeline (epic pt_cb8f98b0cb9b47fb) ──
    # The VU turn must render with the IDENTICAL agent inline per-tool timeline.
    # `_run_single_turn` runs the sub-task with `_endpoint_managed=True`, which
    # SKIPS the `persist_task_result` path where `assemble_segments` normally
    # runs — so the sub-task never got a `segments` list. Assemble it here, off
    # the SAME finished sub_task (its terminal content/thinking + merged
    # toolRounds), so it can be persisted onto the VU message. This is the ONLY
    # source; `sub_task.get('segments')` is always empty at this point.
    #
    # Persist the THIN form (segments_to_json strips the `_round` mirror) —
    # `toolRounds` is co-persisted on the same VU row, so the renderer + any
    # rehydration path recover the full round. DISPLAY-ONLY: segments on a
    # role=user VU row never reach the next agent (conv_message_builder's
    # _build_user_message reads ONLY `content`; the segment-first reconstruction
    # is assistant-only), so the VU provenance-split invariant holds.
    seg_thin: list = []
    try:
        from lib.tasks_pkg.segments import assemble_segments, segments_to_json
        seg_thin = segments_to_json(assemble_segments(sub_task))
    except Exception as e:
        logger.warning('[Autopilot %s] VU segment assembly failed (timeline will '
                       'fall back to grouped render): %s', tid, e)
        seg_thin = []

    logger.info('[Autopilot %s] VU reply: %.200s%s (used %d tool round(s), %d segment(s))',
                tid, text, ' …' if len(text) > 200 else '', len(rounds), len(seg_thin))
    return {'text': text, 'rounds': rounds, 'segments': seg_thin}


# ──────────────────────────────────────────────────────────────────
#  Baton-handoff cluster extracted 2026-07-25 to autopilot_baton.py
#  (pt_00459503 slice 4, on the post-pt_8dc03017-cutover surface).
#  Facade re-exports below keep the historical import surface —
#  lib/tasks_pkg/endpoint/_translate.py imports _maybe_auto_translate_vu
#  via `from lib.tasks_pkg.autopilot import ...` at call time.
# ──────────────────────────────────────────────────────────────────
from lib.tasks_pkg.autopilot_baton import (  # noqa: E402
    _presync_parent_reply,
    _has_pending_real_message,
    _successor_already_running,
    _append_vu_message_to_conv,
    _maybe_auto_translate_vu,
    _start_followup_task,
)



def _preserve_unsent_vu_and_conclude(task: dict, conv_id: str, run_id: str,
                                     vu_msg_id: str, vu_text: str,
                                     reason: str) -> None:
    """Preserve an ALREADY-PRODUCED VU reply that will not become a turn.

    ★ YIELDING IS NOT DESTROYING. Every path that decides to stop AFTER the VU
    has produced text must call this BEFORE returning. Standing down means "do
    not chain another turn"; it has never meant "throw away the work already
    done". On 2026-07-28 those two were the same ``return None``, so a finished
    24-round / 15-minute VU reply was discarded leaving no trace anywhere but a
    truncated log line, and the run died with the frontend still attached to it.

    Two things happen here, in this order:

    1. **Persist the text into the SIDECAR** (``settings.autopilotSummaries``),
       explicitly flagged ``unsent=True`` so a reader can tell it never entered
       the conversation.

       ★ DELIBERATELY NOT ``conv.messages``. That list is the conversation
       history sent UPSTREAM on the next turn, not merely a render source. A VU
       reply that was never delivered would, if appended there, become
       something the model reads back as words the human actually said. The
       sidecar is human-only by construction and is already the channel the
       run fold renders from.

    2. **Emit the terminal fact** via ``_emit_run_concluded_event`` \u2014 the only
       signal that makes the system admit the run is over. Without it the run
       is unobservable-dead: the marker is cleared so crash-resume will never
       revisit it, while a connected client keeps holding task ids that will
       never settle (observed: SyncDrift STALLED for 2h12m).

    ``reason`` names WHY the output went unsent (e.g. ``yielded_to_human``);
    it reaches the fold as the record's reason. Best-effort throughout: this
    runs on a path that is already stopping, so a failure here must never
    replace the stop with an exception.
    """
    tid = task['id'][:8]
    text = (vu_text or '').strip()
    if text:
        try:
            _store_run_record(conv_id, run_id, reason=reason, text=text,
                              unsent=True)
        except Exception as e:
            logger.warning('[Autopilot %s] unsent-VU preserve failed '
                           '(non-fatal): %s', tid, e, exc_info=True)
    try:
        _emit_run_concluded_event(task, conv_id, run_id, reason=reason)
    except Exception as e:
        logger.warning('[Autopilot %s] conclude-on-yield failed '
                       '(non-fatal): %s', tid, e, exc_info=True)
    # Clear the run PIN (not the armed marker): this run is now concluded, so
    # the next VU turn must mint a FRESH run id. Reusing a concluded id would
    # make the fold gate (which keys on the concluded record) swallow live
    # turns. The marker is deliberately left ARMED — yielding to a human, or
    # being superseded, does not mean the user turned autopilot off, and
    # silently disarming here would end the loop instead of pausing it.
    _clear_run_id(conv_id)
    logger.info('[Autopilot %s] run concluded (reason=%s) with %d unsent VU '
                'chars preserved in the sidecar (NOT conversation history) '
                'vuMsgId=%s', tid, reason, len(text), vu_msg_id[:12])


def maybe_run_autopilot(task: dict) -> dict | None:
    """End-of-turn autopilot hook + the VU-carrier close-on-exit guarantee.

    Thin wrapper around :func:`_maybe_run_autopilot_inner`: whatever the
    inner decides (TASK_DONE / preemption / abort / budget stop /
    superseded / follow-up spawned / raise), the VU carrier sub-task is
    flipped terminal on the way out so its SSE stream synthesizes the
    minimal closing done (stamping the just-spawned follow-up when one
    registered) instead of keepaliving forever.
    """
    try:
        return _maybe_run_autopilot_inner(task)
    finally:
        _close_vu_carrier_stream(task)


def _maybe_run_autopilot_inner(task: dict) -> dict | None:
    """End-of-turn hook: run the VU and spawn a follow-up task if eligible.

    Called from ``_finalize_and_emit_done`` BEFORE ``append_event(done_evt)``
    so the returned info can be embedded in the same ``done`` SSE event
    that finishes the current turn.  This eliminates the polling race
    where the SSE stream closed before the VU had time to spawn the
    follow-up task — the synthetic user message is now delivered
    in-band on the same connection.

    Returns ``{'next_task_id': str, 'vu_msg': dict}`` when a follow-up
    was spawned, ``None`` otherwise (no autopilot, no eligible context,
    VU emitted ``[VU: TASK_DONE]``, real user message queued, or any
    failure path).  The orchestrator inlines the dict into ``done_evt``
    as ``autopilotNextTaskId`` + ``autopilotVuMessage``.
    """
    tid = task['id'][:8]

    if not is_autopilot_enabled(task):
        # Log at debug level so silencing is invisible in normal mode
        # but findable when someone wonders "why didn't it take over?".
        cfg = task.get('config') or {}
        logger.debug('[Autopilot %s] Skip — not enabled '
                     '(autopilot=%s, endpointMode=%s, _endpoint_managed=%s)',
                     tid, cfg.get('autopilot'), cfg.get('endpointMode'),
                     task.get('_endpoint_managed'))
        return None

    conv_id = task.get('convId') or ''
    if not conv_id or task.get('_inline_messages'):
        logger.debug('[Autopilot %s] Skip — no DB-backed conversation', tid)
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Skip — task aborted before VU could run', tid)
        return None
    if task.get('error'):
        logger.info('[Autopilot %s] Skip — task ended in error: %.120s',
                    tid, str(task.get('error')))
        return None
    if task.get('finishReason') == 'tool_rounds_exhausted':
        logger.info('[Autopilot %s] Skip — tool rounds exhausted', tid)
        return None

    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Skip — real user message queued '
                    '(it takes priority)', tid)
        return None
    # ``_successor_already_running`` is largely redundant in the new
    # ordering (queue dispatch happens AFTER us via persist_task_result),
    # but keep it as defense-in-depth for endpoint-mode / branch flows
    # that may have already advanced the latest-task registry for this
    # conversation.
    if _successor_already_running(task, conv_id):
        logger.info('[Autopilot %s] Skip — another task already took over '
                    'for conv=%s', tid, conv_id[:8])
        return None

    from lib.tasks_pkg.manager import append_event

    # Mint the VU message id up front and EAGERLY emit `autopilot_vu_start`
    # so the frontend creates the simulated-user bubble in the USER lane
    # the moment the worker stops — showing "Autopilot · composing…" with
    # the Autopilot avatar, exactly like a real pending user turn.  The
    # VU's thinking / tool calls / reply then stream INTO that bubble via
    # the wrapped `autopilot_vu_event` frames (see make_vu_event_transform).
    #
    # IMPORTANT — the start event is IN-MEMORY ONLY: it does NOT write
    # anything to the conv DB.  Persistence happens exactly once, on
    # success, in `_append_vu_message_to_conv` (fired right before
    # `autopilot_vu_done`).  Failure paths (TASK_DONE / abort / queued
    # real user msg) emit `autopilot_vu_cancel`, which removes the
    # in-memory bubble and leaves NO trace on disk — preserving the
    # "no ghost empty VU at the bottom" guarantee.
    vu_msg_id = str(uuid.uuid4())
    # Resolve the explicit run boundary up front so it can be stamped on
    # BOTH the VU turn (below) and the summary report (TASK_DONE branch).
    run_id = _get_or_persist_run_id(conv_id)

    # ★ Carry the parent worker's SETTLED finish metadata on vu_start so the
    #   frontend can complete the parent bubble's finish bar (model / usage /
    #   cost / finishReason) at the MOMENT the VU takes over the streaming
    #   substrate — not tens of seconds later when the parent `done` event
    #   finally fires (that event is deliberately withheld until the whole VU
    #   stream completes, so the follow-up baton can ride on it). Without this
    #   the early-finalized worker bubble shows a bar with ONLY the model tag
    #   (no tokens / no cost / no ✓) for the entire VU turn. `_committedMsg`
    #   was stamped by the pre-emit `_sync_result_to_conversation` in the
    #   orchestrator finalize (the EXACT dict written to conversations.messages,
    #   carrying finishReason/usage/cost/apiRounds). It is display-only here —
    #   the authoritative record is still shipped verbatim on the parent `done`
    #   event's `committedMessage`; this is the identical dict, just delivered
    #   early so the parent bar never renders incomplete.
    #
    #   SKIP-PATH FALLBACK: `_committedMsg` is unset when the pre-emit conv
    #   sync was skipped (freshness guard / CAS-exhaustion / inline). Emitting
    #   vu_start with NO finish payload there would leave the parent bar
    #   incomplete for the WHOLE VU turn (filled only by the late `done`) —
    #   the exact "sometimes incomplete" state the objective forbids. But the
    #   fields that DRAW the bar are already settled on the task itself by the
    #   orchestrator finalize BEFORE this hook runs: `finishReason` (_finalize
    #   ~L621), `usage` (~L622/917), `model` (~L847), `apiRounds` (~L925), and
    #   `provider_id` (lets the frontend `calcCostCny` compute the cost-tag
    #   even without a committed `cost`). So when there is no committed dict we
    #   build a MINIMAL parentMessage from those task fields. We only omit
    #   `parentMessage` entirely when the task genuinely has NOTHING to show
    #   (no finishReason AND no usage — e.g. an errored turn with no metering),
    #   the sole circumstance where the bar legitimately waits for `done`.
    _parent_msg = task.get('_committedMsg')
    if not _parent_msg:
        _fr = task.get('finishReason')
        _usg = task.get('usage')
        if _fr or _usg:
            _parent_msg = {'role': 'assistant'}
            if _fr:
                _parent_msg['finishReason'] = _fr
            if _usg:
                _parent_msg['usage'] = _usg
            for _k in ('model', 'provider_id', 'apiRounds', 'preset',
                       'toolSummary', 'thinkingDepth'):
                _v = task.get(_k)
                if _v is not None:
                    _parent_msg[_k] = _v
            logger.debug('[Autopilot %s] vu_start using task-field fallback '
                         'parentMessage (no _committedMsg; finishReason=%s)',
                         tid, _fr)
    # EAGERLY emit VU_START *before* run_virtual_user below: the VU LLM call
    # can stall for tens of seconds on a rate-limited first token, and the
    # client must lazily stand up the VU bubble (warm-up placeholder / retry
    # chip) from this frame alone. Emitting it after the call would leave the
    # user staring at nothing during exactly the window where the chip is the
    # only liveness signal.
    _start_evt = build_event(EventType.AUTOPILOT_VU_START, vuMsgId=vu_msg_id)
    if _parent_msg:
        _start_evt['parentMessage'] = _parent_msg
    try:
        append_event(task, _start_evt)
    except Exception as e:
        logger.debug('[Autopilot %s] vu_start emit failed: %s', tid, e)

    vu_result = run_virtual_user(task, vu_msg_id=vu_msg_id)
    if vu_result is None:
        # VU emitted [VU: TASK_DONE], errored, or was aborted. On a graceful
        # TASK_DONE, disarm the persistent marker so the loop ends and the
        # queue-bar sentinel disappears.  (Abort/error leave the marker intact
        # — a transient failure shouldn't silently disarm.)
        if task.get('_vu_emitted_done'):
            # The run reached its objective. CONCLUDE (fold) the run and settle
            # the turn synchronously — the A-layer close-out REPORT was removed,
            # so there is no longer a slow reporter LLM turn / EN→ZH translation
            # to keep off the hot path. All that remains is the cheap B-layer
            # fold fact: persist the concluded record (stamping `anchorMsgId`
            # while the run's boundary turn is stable — no new round has started
            # yet), emit the project-brain pulse, and emit the
            # `autopilot_run_concluded` SSE so a connected client folds the run
            # immediately. On this clean TASK_DONE path there is no follow-up
            # baton to strand — the loop is ENDING — and the terminal `done`
            # fires right after, closing the stream. Best-effort: a failure here
            # just leaves the run unfolded until the next settings round-trip.
            try:
                _emit_run_concluded_event(task, conv_id, run_id,
                                          reason='task_done')
            except Exception as e:
                logger.warning('[Autopilot %s] run conclude failed '
                               '(non-fatal): %s', tid, e, exc_info=True)
            # Disarm + clear the run pin so the turn settles now.
            try:
                from lib.message_queue import clear_autopilot_marker
                clear_autopilot_marker(conv_id)
            except Exception as e:
                logger.debug('[Autopilot %s] marker clear failed: %s', tid, e)
            _clear_run_id(conv_id)
        # Tell the frontend to discard any in-memory bubble it may
        # have lazily created from inner stream events; nothing was
        # ever persisted.  Dual-emitted: the pre-hop client (parent
        # stream) AND the post-hop client (carrier stream) both tear
        # the bubble down.
        _emit_vu_lifecycle_frame(task, build_event(
            EventType.AUTOPILOT_VU_CANCEL,
            vuMsgId=vu_msg_id,
        ))
        return None
    vu_text = vu_result['text']
    vu_rounds = vu_result.get('rounds') or []
    vu_segments = vu_result.get('segments') or []
    # Machine-control tokens ([PROGRESS: resolved=X remaining=Y] lines, plus
    # any stray [VU: TASK_DONE] remnant) must NEVER reach conversation
    # history: the next turn re-reads persisted VU rows as ordinary user
    # text and the model starts authoring the signal itself (pt_0ae59e94 —
    # 90 leaked lines across 52 convs).  Strip HERE — before
    # _append_vu_message_to_conv persists — via the single agent_verdict
    # predicate.  The budget guard below still receives vu_text VERBATIM
    # (it parses the PROGRESS line for the diminishing-returns ledger), so
    # only the persisted / translated copy is cleaned, never the guard's
    # signal.
    vu_text_clean = _strip_machine_tokens(vu_text)

    # Race-close: a real HUMAN may have submitted a message while the VU LLM
    # call was running.  Yield to that person — but PRESERVE the reply the VU
    # already produced (see _preserve_unsent_vu_and_conclude: yielding is not
    # destroying) and emit the terminal fact so the run is visibly over.
    if _has_pending_real_message(conv_id):
        logger.info('[Autopilot %s] Human message arrived during VU '
                    'call — yielding to it', tid)
        _preserve_unsent_vu_and_conclude(
            task, conv_id, run_id, vu_msg_id, vu_text_clean,
            reason='yielded_to_human')
        _emit_vu_lifecycle_frame(task, build_event(
            EventType.AUTOPILOT_VU_CANCEL, vuMsgId=vu_msg_id))
        return None
    if task.get('aborted'):
        logger.info('[Autopilot %s] Aborted while VU was running — stopping', tid)
        _preserve_unsent_vu_and_conclude(
            task, conv_id, run_id, vu_msg_id, vu_text_clean,
            reason='aborted_mid_vu')
        _emit_vu_lifecycle_frame(task, build_event(
            EventType.AUTOPILOT_VU_CANCEL, vuMsgId=vu_msg_id))
        return None

    # An empty cleaned text must never become a turn: appending it persists
    # a ghost empty VU row (the visible "empty Autopilot bubble" — there is
    # NO cleanup path for it) and the follow-up it spawns carries an empty
    # user query, which strict providers hard-400. The marker stays armed —
    # an empty reply is a transient degenerate, not a disarm signal.
    if not vu_text_clean.strip():
        logger.info('[Autopilot %s] VU reply is empty after token strip — '
                    'standing down instead of appending a ghost row', tid)
        _emit_vu_lifecycle_frame(task, build_event(
            EventType.AUTOPILOT_VU_CANCEL, vuMsgId=vu_msg_id))
        return None

    # VU produced a reply — NOW commit it to the conv DB.  But FIRST make
    # sure the parent's final assistant reply is committed: on the
    # runtime-arm path (autopilot flipped on mid-stream) the orchestrator's
    # pre-hook sync may have been skipped (it gates on is_autopilot_enabled
    # evaluated a few lines earlier), so do it here too — idempotent.
    _presync_parent_reply(task)
    vu_msg = _append_vu_message_to_conv(
        conv_id, vu_msg_id, vu_text_clean, rounds=vu_rounds, run_id=run_id,
        segments=vu_segments,
    )
    if vu_msg is None:
        # The append did not land. Every reason for that (row gone, CAS budget
        # exhausted, or a real human turn arriving mid-flight so we must NOT
        # append behind them) means the run stands down with a finished reply
        # in hand — which is exactly the case this bypass exists for. Falling
        # through to a bare `return None` here is what destroyed 5 completed VU
        # turns: yielding is not destroying.
        _preserve_unsent_vu_and_conclude(
            task, conv_id, run_id, vu_msg_id, vu_text_clean,
            reason='vu_append_not_persisted')
        _emit_vu_lifecycle_frame(task, build_event(
            EventType.AUTOPILOT_VU_CANCEL, vuMsgId=vu_msg_id))
        return None

    # Server-side auto-translate safety net for the VU turn — the append path
    # above is SEPARATE from manager._sync_result_to_conversation (which owns
    # the assistant/critic safety net), so without this a VU turn is left
    # untranslated unless a viewer fires a manual translate. Row index resolved
    # from the persisted _msgId (not guessed); best-effort, never blocks.
    _maybe_auto_translate_vu(conv_id, vu_msg_id, vu_text_clean)

    # Tell the frontend the VU bubble is fully baked.  Carries the
    # final content + rounds so a client that lazily built the bubble
    # from streaming deltas — or one that missed them entirely (cold
    # replay, late connect) — can reconcile in one shot.  Dual-emitted
    # onto parent + carrier streams.
    _emit_vu_lifecycle_frame(task, build_event(
        EventType.AUTOPILOT_VU_DONE,
        vuMsgId=vu_msg_id,
        vuMessage=vu_msg,
    ))

    # ★ BUDGET / STUCK GUARD — the mechanical backstop the loop historically
    #   lacked ("No turn cap, no state-change watchdog").  The VU turn is now
    #   persisted (its reply is preserved in history), so we count it, then
    #   decide whether the run may spawn the NEXT follow-up.  The counters are
    #   pinned per-run in settings (durable across the recursive follow-up
    #   tasks AND a crash+kick-resume), so a run that hit its turn ceiling or
    #   emitted N near-identical nudges STOPS here instead of looping forever.
    #   The check is FAIL-OPEN (a settings glitch never wedges a healthy loop).
    # Worker's touched-file set THIS turn (the churn signal for the
    # no-progress guard). modifiedFileList is populated on the parent task by
    # the orchestrator before this hook runs (orchestrator.py ~L785).
    _turn_targets = [f.get('path') for f in (task.get('modifiedFileList') or [])
                     if isinstance(f, dict) and f.get('path')]
    budget = _record_vu_turn_and_check_budget(conv_id, vu_text,
                                              targets=_turn_targets)
    if budget.get('stop'):
        reason = budget.get('reason') or 'budget_exhausted'
        logger.warning('[Autopilot %s] run STOPPED by guard (reason=%s, turn=%d) '
                       '— escalating as unfinished/needs-review', tid, reason,
                       budget.get('turn', 0))
        # Same CONCLUDE machinery as a clean close-out — record stamped
        # incomplete, feed pulse + run_concluded SSE emitted so the run folds
        # and the loop disarms — but with NO report: an abnormal end is not a
        # normal end, and the objective is unverified, so we do NOT spend an
        # LLM turn writing a debrief. The fold renders "stopped early — needs
        # review" over the last VU turn (the visible tail). This mirrors the
        # report-less manual-stop arm (conclude_run).
        try:
            _emit_run_concluded_event(task, conv_id, run_id, reason=reason)
        except Exception as e:
            logger.warning('[Autopilot %s] incomplete-conclude failed '
                           '(non-fatal): %s', tid, e, exc_info=True)
        try:
            from lib.message_queue import clear_autopilot_marker
            clear_autopilot_marker(conv_id)
        except Exception as e:
            logger.debug('[Autopilot %s] marker clear failed: %s', tid, e)
        _clear_run_id(conv_id)
        return None

    # ★ FINAL supersede recheck — the last gate before we spawn a follow-up.
    #   Everything between the post-VU abort check above and here does real
    #   work that takes wall-clock time (parent pre-sync, VU-message DB commit,
    #   the _maybe_auto_translate_vu LLM call, the budget bookkeeping). A user
    #   action that supersedes this run — a concurrent regenerate / edit / send
    #   — lands in exactly that window: it calls abort_running_tasks_for_conv
    #   (stamping this parent task['aborted']=True) and create_task (registering
    #   its OWN task as _conv_latest_task). Without this recheck we would still
    #   call _start_followup_task, whose create_task supersede invariant would
    #   then ABORT the user's just-started task — "autopilot snipes the user's
    #   regen". Re-read BOTH signals (aborted flag + latest-task registry) at
    #   the last possible moment and stand down if either says we were
    #   superseded. The already-persisted VU turn stays in history (harmless);
    #   we simply don't chain another turn on top of the user's action.
    if task.get('aborted'):
        logger.info('[Autopilot %s] Superseded (task aborted) just before '
                    'follow-up spawn — standing down', tid)
        _preserve_unsent_vu_and_conclude(
            task, conv_id, run_id, vu_msg_id, '', reason='superseded')
        return None
    if _successor_already_running(task, conv_id):
        logger.info('[Autopilot %s] Superseded (a newer task owns conv=%s) just '
                    'before follow-up spawn — standing down', tid, conv_id[:8])
        _preserve_unsent_vu_and_conclude(
            task, conv_id, run_id, vu_msg_id, '', reason='superseded')
        return None

    next_task_id = _start_followup_task(task, conv_id)
    if next_task_id is None:
        return None

    # Tell ``_dispatch_queued_message`` (which runs slightly after us
    # inside ``persist_task_result``) that autopilot already spawned a
    # successor for this conversation.  Otherwise a real user message
    # that landed in the tiny window between our post-VU queue re-check
    # and now would race-spawn its own task and abort our follow-up.
    # The queued message will be picked up when the autopilot follow-up
    # itself completes.
    task['_autopilot_spawned_followup'] = next_task_id

    return {'next_task_id': next_task_id, 'vu_msg': vu_msg}


# ──────────────────────────────────────────────────────────────────
#  Kick from idle — start the VU loop on a FINISHED conversation
# ──────────────────────────────────────────────────────────────────

def _run_autopilot_kick(task: dict) -> None:
    """Carrier-task entry: run the VU hook directly, with NO worker turn.

    Used by the "push the conversation forward" gesture (empty-Enter on a
    finished conversation with autopilot ON).  Unlike a normal task, this
    carrier never calls the LLM as the assistant — the conversation already
    ended and the last message is the agent's reply, so the virtual user
    should answer it straight away.  We reuse the SAME end-of-turn hook the
    natural-stop path runs (``maybe_run_autopilot``): it emits the
    ``autopilot_vu_*`` stream, appends the synthetic user message, spawns the
    follow-up worker task, and returns the ``next_task_id`` / ``vu_msg``
    baton.  The baton rides out on this carrier's ``done`` event (and on
    ``task['_autopilot_followup']`` for the poll path) exactly as it does at
    a natural stop, so the frontend attaches to the follow-up with no extra
    plumbing.

    Invoked from ``orchestrator.run_task`` when ``task['_autopilot_kick']``
    is set.
    """
    from lib.tasks_pkg.manager import append_event, persist_task_result

    tid = task['id'][:8]
    # The carrier produces no assistant content of its own; flip to 'done'
    # immediately (the VU runs as its own task on its own stream — no
    # decision-window withhold is needed on this carrier).
    task['status'] = 'done'

    done_evt = build_event(EventType.DONE)
    if task.get('model'):
        done_evt['model'] = task['model']

    try:
        ap_result = maybe_run_autopilot(task)
        if ap_result:
            logger.info('[Autopilot kick %s] VU took over conv=%s → follow-up %s',
                        tid, task.get('convId', '')[:8],
                        ap_result['next_task_id'][:8])
        else:
            logger.info('[Autopilot kick %s] VU declined to take over conv=%s '
                        '(TASK_DONE / no eligible context)', tid,
                        task.get('convId', '')[:8])
    except Exception as e:
        logger.error('[Autopilot kick %s] hook raised: %s', tid, e, exc_info=True)

    append_event(task, done_evt)
    persist_task_result(task)


def kick_autopilot(conv_id: str, config: dict | None = None) -> dict:
    """Start the virtual-user loop on a conversation whose reply has finished.

    The "push it forward for me" gesture: the user chatted with autopilot ON,
    the turn ended, and they want the virtual user to keep the conversation
    going WITHOUT typing anything.  Because ``maybe_run_autopilot`` only runs
    as an end-of-turn hook (there is no live task to hang it on once the reply
    finished), we spawn a thin carrier task whose ``run_task`` short-circuits
    straight to :func:`_run_autopilot_kick`.

    Refuses (``taskId=None``) when a non-VU task is already ``running`` for the
    conversation — in that case the caller should ARM the live task instead
    (see :func:`arm_autopilot`), so we never double-drive the loop.

    Also persists ``settings.autopilotEnabled=true`` so subsequent manual
    sends keep looping, mirroring the arm route.

    Returns ``{'taskId': str}`` on success, or ``{'taskId': None, 'error':
    str}`` when there is nothing to kick (no conversation, empty history, or a
    task is already running).
    """
    if not conv_id:
        return {'taskId': None, 'error': 'conv_id is required'}

    # Refuse if a live (non-VU) task is already running — arm it instead.
    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        for t in tasks.values():
            if (t.get('convId') == conv_id
                    and t.get('status') == 'running'
                    and not t.get('_vu_subtask')):
                logger.info('[Autopilot kick] conv=%s already has a running '
                            'task %s — refusing kick (arm instead)',
                            conv_id[:8], t.get('id', '?')[:8])
                return {'taskId': None, 'error': 'task_already_running'}

    cfg = dict(config or {})
    cfg['autopilot'] = True
    cfg['endpointMode'] = False
    # Strip assistantMsgId here too: this kick config is the template every
    # follow-up copies (via task['config'] → _start_followup_task), so a stray
    # client-minted id must not seed the run. See the _start_followup_task
    # comment for the collision this prevents.
    for stale_key in (
        'excludeLast', 'toolHistory', 'contentPrefix',
        'checkpointToolRounds', 'checkpointUsage', 'checkpointApiRounds',
        'checkpointModifiedFiles', 'checkpointModifiedFileList',
        'assistantMsgId', 'msgId',
    ):
        cfg.pop(stale_key, None)

    from lib.tasks_pkg import create_task, spawn_task
    from lib.tasks_pkg.conv_message_builder import build_api_messages_from_db

    api_messages = build_api_messages_from_db(conv_id, cfg)
    if api_messages is None:
        return {'taskId': None, 'error': 'conversation_not_found'}
    if not api_messages:
        return {'taskId': None, 'error': 'conversation_empty'}

    task = create_task(conv_id, api_messages, cfg)
    task['_autopilot_kick'] = True

    # Persist the setting so the loop keeps going on any later manual send.
    # Serialized read-merge-write (settings_store) so this doesn't clobber a
    # concurrent tool-state / autopilot settings write on the same row.
    try:
        from lib.conversations import set_conversation_settings
        set_conversation_settings(
            conv_id, {'autopilotEnabled': True, 'activeTaskId': task['id']})
    except Exception as e:
        logger.warning('[Autopilot kick] persist autopilotEnabled failed '
                       'conv=%s: %s', conv_id[:8], e)

    logger.info('[Autopilot kick] conv=%s spawning carrier task %s',
                conv_id[:8], task['id'][:8])
    audit_log('autopilot_kick', conv_id=conv_id, task_id=task['id'])
    spawn_task(task)
    return {'taskId': task['id']}


def resume_armed_autopilot_after_crash(
        extra_conv_ids: list[str] | None = None) -> list[str]:
    """Re-kick every autopilot run left armed when the server died.

    When the server dies while an autopilot follow-up is in flight, the
    end-of-turn hook (:func:`maybe_run_autopilot`) never finished: no VU reply
    was persisted, no follow-up spawned, and no ``done`` baton was emitted.
    Startup recovery (:func:`recover_stale_tasks_on_startup`) restores the
    interrupted assistant reply into the conversation, but it does NOT resume
    the loop — so the run is left settled-but-armed and only continues on the
    user's next manual send. This bridges that gap.

    SCOPE — the DURABLE armed-marker is the AUTHORITATIVE source, NOT the set of
    crash-recovered tasks. We enumerate :func:`list_armed_autopilot_convs` (every
    conv carrying a ``KIND_AUTOPILOT`` marker row, which survives restart) and
    re-kick each. This deliberately catches the armed-but-idle case that a
    recovered-tasks-only gate would MISS: a conversation armed from idle whose
    carrier never spawned, or whose reply already finished before the crash, has
    an armed marker but was never an interrupted task — so it is absent from
    ``recovered_conv_ids`` yet must still resume. ``extra_conv_ids`` (the
    recovery set) is unioned in for belt-and-braces, but the marker scan is what
    guarantees completeness.

    Only conversations with an armed marker are resumed — a run that concluded
    cleanly (``[VU: TASK_DONE]``) or was disarmed cleared its marker, so it is
    correctly left alone. ``kick_autopilot`` itself refuses (``taskId=None``) if
    a live non-VU task is already running for the conv, so calling it
    unconditionally is safe — no double-driving. Best-effort per conv: one
    failure never aborts the batch.

    Returns the list of conv_ids for which a resume carrier was spawned.
    """
    resumed: list[str] = []
    try:
        from lib.message_queue import (
            get_autopilot_marker_config,
            has_autopilot_marker,
            list_armed_autopilot_convs,
        )
    except Exception as e:
        logger.warning('[Autopilot] resume-after-crash: message_queue import '
                       'failed: %s', e)
        return resumed

    # Authoritative: every conv with a durable armed marker. Union the recovery
    # set only for logging symmetry — has_autopilot_marker re-gates each below,
    # so a recovered conv WITHOUT a marker (clean-closed / disarmed) is skipped.
    try:
        armed = set(list_armed_autopilot_convs())
    except Exception as e:
        logger.warning('[Autopilot] resume-after-crash: marker scan failed: %s', e)
        armed = set()
    candidates = armed | {c for c in (extra_conv_ids or []) if c}

    for conv_id in candidates:
        if not conv_id:
            continue
        try:
            if not has_autopilot_marker(conv_id):
                continue
            cfg = get_autopilot_marker_config(conv_id) or {}
            res = kick_autopilot(conv_id, cfg)
            new_tid = res.get('taskId')
            if new_tid:
                resumed.append(conv_id)
                logger.info('[Autopilot] Resumed armed run after crash for '
                            'conv=%s → carrier %s', conv_id[:8], new_tid[:8])
                audit_log('autopilot_resume_after_crash',
                          conv_id=conv_id, task_id=new_tid)
            else:
                logger.info('[Autopilot] resume-after-crash skipped conv=%s '
                            '(%s)', conv_id[:8], res.get('error', 'no task'))
        except Exception as e:
            logger.warning('[Autopilot] resume-after-crash failed for conv=%s: '
                           '%s', conv_id[:8], e, exc_info=True)
    return resumed


# ──────────────────────────────────────────────────────────────────
#  Runtime arming — extracted to lib/tasks_pkg/autopilot_markers.py
#  (pt_00459503 slice 2)
# ──────────────────────────────────────────────────────────────────
#
# The arm/disarm/marker cluster (arm_autopilot / disarm_autopilot /
# _marker_exists) moved to lib/tasks_pkg/autopilot_markers.py per
# docs/AUTOPILOT_DECOMPOSITION_AUDIT.md.  Zero pt_8dc03017 overlap.
#
# Re-exported here as module-level attributes so every existing
# ``from lib.tasks_pkg.autopilot import arm_autopilot`` /
# ``disarm_autopilot`` /  monkeypatch on ``ap.arm_autopilot`` keeps
# working byte-identically. Symbol IDENTITY is preserved (facade attr
# IS the markers-module attr) — verified by
# tests/test_autopilot_markers_extraction_wire_parity.py.
from lib.tasks_pkg.autopilot_markers import (  # noqa: E402,F401
    arm_autopilot,
    disarm_autopilot,
    _marker_exists,
)
