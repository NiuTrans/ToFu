"""Autopilot VU event-forwarding cluster.

Extracted from ``lib.tasks_pkg.autopilot`` under pt_00459503 slice 5 —
the epic-named "event-forwarding" module.  Kept as a LEAF (imports only
the shared event builder + logger, plus a lazy ``manager.append_event``
inside the transform to avoid a cycle with the task manager) so
``autopilot.py`` can re-export these symbols BY IDENTITY without any
back-import from this file.

Contents:
  * ``_VU_FORWARD_TYPES`` — the frozenset of sub-task event types that
    are part of the VU stream contract (wrapped + dual-emitted).
  * ``_VU_LIFECYCLE_TYPES`` — the VU lifecycle frames
    (``autopilot_vu_start`` / ``autopilot_vu_done`` /
    ``autopilot_vu_cancel``) that pass through to the carrier's own
    stream VERBATIM (they are never double-wrapped, and the transform
    never forwards them — ``_emit_vu_lifecycle_frame`` owns the
    parent-side copy so they can't be doubled).
  * ``make_vu_event_transform`` — the per-task event transform installed
    on the VU carrier sub-task as ``sub_task['_vu_event_transform']``
    (consumed by the ``append_event`` facade seam).
  * ``_emit_vu_setup_phase`` — the pre-stream "working" phase emitter
    used to attribute the 2.5–26.7 s silent warmup window between
    ``autopilot_vu_start`` and the sub-task's first orchestrator phase.

★ THE 2026-07-26 CONTRACT CHANGE (conv ms1rrjchpa5pqw incident)
--------------------------------------------------------------
Pre-fix, ``_VUEventForwarder`` (a list subclass) kept the carrier's own
event list RAW and only forwarded WRAPPED frames to the parent — sound in
the pre-cutover world where nobody ever attached to the carrier's stream.
After the pt_8dc03017 cutover the client HOPS from the parent's closed
stream to the carrier's own stream (``latestLiveTaskId``), so the raw
list + the agent ``state`` snapshot rendered the VU as a second "Agent"
bubble, the machine sentinels stayed visible, and the (endpoint-managed,
never-terminal) carrier stream kept the sidebar pulsing forever.

The transform replaces the list subclass: the carrier's own stream, push
channel AND persisted event log now all carry the SAME VU envelope the
parent stream always did (one transform applied at the single
``append_event`` seam → no three-way divergence), while the parent
forward is preserved verbatim for the pre-hop window.  Frames that are
not part of the VU contract (``done``, ``round_committed``, …) are
dropped from the carrier stream entirely.
"""

from __future__ import annotations

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger

logger = get_logger(__name__)


_VU_FORWARD_TYPES = frozenset({
    'delta', 'phase',
    'tool_start', 'tool_result', 'tool_progress', 'tool_complete',
    'tool_compacted',
    'stdin_request', 'stdin_resolved',
    'write_approval_request',
    'human_guidance_request', 'human_guidance_response',
})


_VU_LIFECYCLE_TYPES = frozenset({
    'autopilot_vu_start', 'autopilot_vu_done', 'autopilot_vu_cancel',
})


def _forward_to_parent(parent_task: dict, vu_msg_id: str, ev: dict) -> None:
    """Forward ONE wrapped VU frame onto the PARENT task's stream.

    The pre-hop window: the client is still attached to the parent's
    stream (its LATE/real done has not fired yet), so the parent's stream
    is the only channel — the VU bubble there must see the same frames.
    """
    from lib.tasks_pkg.manager import append_event as _ap_event
    _ap_event(parent_task, build_event(
        EventType.AUTOPILOT_VU_EVENT,
        vuMsgId=vu_msg_id,
        inner=ev,
    ))


def make_vu_event_transform(parent_task: dict, vu_msg_id: str):
    """Build the per-task event transform for a VU carrier sub-task.

    Installed as ``sub_task['_vu_event_transform']`` and consumed by the
    ``append_event`` facade (``lib.tasks_pkg.manager._events``), which
    applies it to the frame BEFORE append / persist / push — so the
    carrier's own SSE stream, its push channel and its persisted event
    log all carry the identical VU contract:

      * forward types (``_VU_FORWARD_TYPES``) → wrapped as
        ``autopilot_vu_event`` (with ``vuMsgId`` + ``inner``) on the
        carrier's own stream, AND forwarded to the parent stream (the
        pre-hop window) — dual emission, both wrapped;
      * lifecycle frames (``_VU_LIFECYCLE_TYPES``) → VERBATIM on the
        carrier's own stream, NOT forwarded (the explicit dual-emit in
        ``autopilot._emit_vu_lifecycle_frame`` owns the parent copy);
      * anything else → dropped from the carrier stream entirely.

    Args:
        parent_task: The parent worker task whose stream receives the
            forwarded copy (pre-hop window).
        vu_msg_id: The stable VU message id the frontend routes by.

    Returns:
        ``(task, event) -> event | None`` — the frame to emit on the
        carrier's own stream, or ``None`` to drop it from the stream
        (facade bookkeeping still reads the raw frame).
    """
    def _vu_transform(task: dict, ev: dict):
        et = (ev or {}).get('type')
        if et in _VU_FORWARD_TYPES:
            try:
                _forward_to_parent(parent_task, vu_msg_id, ev)
            except Exception as e:
                logger.debug('[Autopilot] event forward failed (non-fatal): %s', e)
            return build_event(
                EventType.AUTOPILOT_VU_EVENT,
                vuMsgId=vu_msg_id,
                inner=ev,
            )
        if et in _VU_LIFECYCLE_TYPES:
            return ev
        return None
    return _vu_transform


def _emit_vu_setup_phase(task: dict, vu_msg_id: str | None, detail: str) -> None:
    """Surface a pre-stream Autopilot setup step in the VU bubble.

    Diagnosis (task_events probe, debug/autopilot_warmup_window_probe.py):
    between ``autopilot_vu_start`` and the VU sub-task's first orchestrator
    phase (``llm_thinking`` / ``waiting_model``) there is a genuinely SILENT
    window — measured 2.5–26.7s across 12 real runs — during which
    ``run_virtual_user`` resolves the objective (DB read), assembles the
    message list and builds the sub-task. Nothing was emitted, so the bubble
    sat on the bare "Autopilot…" placeholder with no attribution of what was
    blocking.

    This emits a ``working`` phase wrapped as ``autopilot_vu_event`` — the
    SAME envelope the carrier's own stream carries — so it routes into the
    VU bubble by ``vuMsgId`` and renders through the existing
    ``updateStreamingUI`` ``working`` branch (``phase.detail`` shown
    verbatim). No new event type; the frontend already handles it.

    Emitted directly on the PARENT task because the sub-task (and its
    transform) does not exist yet at these steps.
    """
    if not vu_msg_id:
        return
    try:
        from lib.tasks_pkg.manager import append_event
        append_event(task, build_event(
            EventType.AUTOPILOT_VU_EVENT,
            vuMsgId=vu_msg_id,
            inner={'type': 'phase', 'phase': 'working', 'detail': detail},
        ))
    except Exception as e:
        logger.debug('[Autopilot] vu setup-phase emit failed (non-fatal): %s', e)
