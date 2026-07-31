"""Autopilot arm/disarm marker cluster — extracted from ``autopilot.py`` (pt_00459503 slice 2).

**Extraction context** (board epic ``pt_00459503f23b4c0e``, slice 2):
carved out of ``lib/tasks_pkg/autopilot.py`` per
``docs/AUTOPILOT_DECOMPOSITION_AUDIT.md``. Chose a SIBLING module
(``autopilot_markers.py``) rather than a full module→package conversion,
matching slice 1's ``autopilot_state.py`` pattern: converting a heavily-
imported module into a package on a shared-HEAD cross-sibling worktree
carries much bigger merge risk than adding one new sibling file, and the
wire-parity contract (re-export identity through ``autopilot.py``) is
byte-equivalent either way.

**pt_8dc03017 sequencing constraint**: the sibling epic
``pt_8dc030176bad450b`` (owner-parked, human-gated) plans to mutate
``_VUEventForwarder``, the ``_autopilot_deciding`` latch, and the VU
``convId=''`` opt-out. This module carries NONE of those symbols — the
arm/disarm marker cluster is strictly disjoint from that mutation surface.

**What's in here**: three arm/disarm helpers that manipulate the
persistent autopilot-armed queue-lane marker + the live-task
``config['autopilot']`` flag:

  * :func:`arm_autopilot` — runtime-arm gesture: flip
    ``config['autopilot']=True`` on live tasks + persist the queue-lane
    marker.  Refuses when endpoint mode is live (mutual exclusion).
  * :func:`disarm_autopilot` — the inverse: clear the marker + flip
    live-config off + emit the run-concluded record.
  * :func:`_marker_exists` — the marker-probe helper the arm result uses
    to compute the final ``armed`` flag.

The facade module ``lib.tasks_pkg.autopilot`` re-exports every symbol
identity-preservingly so existing ``from lib.tasks_pkg.autopilot import
X`` call sites (routes/chat_queue.py, lib/chat_dispatch.py) and
``monkeypatch.setattr(ap, 'arm_autopilot', ...)`` patch points
(tests/test_autopilot_arm.py) keep working byte-identically.

**Deferred to a later slice** (per audit's ordering, to stay clear of
pt_8dc03017 mutation coupling): ``kick_autopilot`` /
``resume_armed_autopilot_after_crash`` — both indirectly wire to
``maybe_run_autopilot`` / ``_run_autopilot_kick`` which touch the
_VUEventForwarder / _autopilot_deciding surface.
"""

from __future__ import annotations

from lib.log import audit_log, get_logger

# pt_00459503 slice 3 — ``conclude_run`` now lives in a LEAF module
# (autopilot_run_lifecycle.py), so we can import it at MODULE TOP
# without recreating the cycle slice 2 had to guard with a lazy
# import.  autopilot_run_lifecycle has ZERO dependencies on
# autopilot.py — the dependency graph is now strictly one-way.
from lib.tasks_pkg.autopilot_run_lifecycle import conclude_run

logger = get_logger(__name__)


def arm_autopilot(conv_id: str) -> dict:
    """Arm autopilot for a conversation whose task is already in flight.

    Use case: the user chatted with autopilot OFF, then decides to step
    away mid-reply and wants the virtual user to take over at the next
    natural stop.  Toggling the frontend button only affects the NEXT
    task — the in-flight task's ``config['autopilot']`` was frozen at
    creation time, so its end-of-turn hook would never fire.

    This flips ``config['autopilot'] = True`` on every live (status=
    ``running``) task for the conversation.  Because ``_finalize_and_emit_done``
    re-reads ``is_autopilot_enabled(task)`` at finalize, the running task
    will now run the VU hook when it stops.  Mutating ``config`` (rather
    than a side flag) also means the value propagates to autopilot
    follow-ups via ``_start_followup_task``'s ``dict(task['config'])``,
    so the loop continues until the VU emits ``[VU: TASK_DONE]``.

    Endpoint-managed tasks are skipped — autopilot and endpoint mode are
    mutually exclusive (they share the same termination boundary).

    Returns ``{'armed': bool, 'taskIds': [...]}`` — ``armed`` is True iff
    at least one live task was flipped.  When no task is live (the reply
    already finished), ``armed`` is False and the caller should rely on
    the persisted ``autopilotEnabled`` setting to kick off the loop on the
    user's next send.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    armed_ids: list[str] = []
    marker_cfg: dict = {}
    endpoint_blocked = False
    with tasks_lock:
        # Pass 1 — mutual exclusion: if ANY live task for the conv is endpoint
        # mode, refuse to arm autopilot (they share the same termination
        # boundary; running both double-loops).
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('status') != 'running':
                continue
            if t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if t.get('_endpoint_managed') or (isinstance(cfg, dict) and cfg.get('endpointMode')):
                endpoint_blocked = True
                break
        # Pass 2 — flip config.autopilot on live non-endpoint tasks + capture
        # a config to seed the marker.
        if not endpoint_blocked:
            for tid, t in tasks.items():
                if t.get('convId') != conv_id or t.get('status') != 'running':
                    continue
                if t.get('_endpoint_managed') or t.get('_vu_subtask'):
                    continue
                cfg = t.get('config')
                if not isinstance(cfg, dict):
                    continue
                if not marker_cfg:
                    marker_cfg = dict(cfg)
                if not cfg.get('autopilot'):
                    cfg['autopilot'] = True
                    armed_ids.append(tid)

    if endpoint_blocked:
        logger.info('[Autopilot] Arm refused for conv=%s — endpoint mode is '
                    'live (mutually exclusive)', conv_id[:8])
        return {'armed': False, 'taskIds': [], 'markerAdded': False}

    # Persist the armed-marker sentinel in the queue so the arm survives a
    # page reload, shows in the queue bar (cancellable), and — critically —
    # keeps autopilot armed even when no task is live (the "I'll step away,
    # take over when the current reply finishes" gesture works whether or not
    # a reply is still streaming).  Idempotent: at most one marker per conv.
    marker_added = False
    try:
        from lib.message_queue import arm_autopilot_marker
        res = arm_autopilot_marker(conv_id, marker_cfg)
        marker_added = res.get('armed', False)
    except Exception as e:
        logger.warning('[Autopilot] failed to persist armed-marker for '
                       'conv=%s: %s', conv_id[:8], e)

    if armed_ids:
        logger.info('[Autopilot] Armed %d live task(s) for conv=%s: %s '
                    '(marker_added=%s)', len(armed_ids), conv_id[:8],
                    [t[:8] for t in armed_ids], marker_added)
    else:
        logger.info('[Autopilot] Arm requested for conv=%s — no live task to '
                    'flip; persistent marker now governs (marker_added=%s)',
                    conv_id[:8], marker_added)
    audit_log('autopilot_armed', conv_id=conv_id, task_ids=armed_ids,
              marker_added=marker_added)

    # ``armed`` reflects whether autopilot is now armed for the conv — True if
    # a live task was flipped OR a marker is in place.
    armed = bool(armed_ids) or marker_added or _marker_exists(conv_id)
    return {'armed': armed, 'taskIds': armed_ids, 'markerAdded': marker_added}


def _marker_exists(conv_id: str) -> bool:
    try:
        from lib.message_queue import has_autopilot_marker
        return has_autopilot_marker(conv_id)
    except Exception as e:
        logger.debug('[Autopilot] _marker_exists probe failed for conv=%s: %s',
                     conv_id[:8] if conv_id else '?', e)
        return False


def disarm_autopilot(conv_id: str) -> dict:
    """Cancel autopilot for a conversation: clear the marker + live config.

    The inverse of :func:`arm_autopilot`.  Removes the persistent armed-marker
    sentinel AND flips ``config['autopilot']=False`` on any live task so the
    loop stops at the current turn's natural end.  Used by the queue-bar
    cancel button and the toggle-OFF gesture.

    Returns ``{disarmed, markerCleared, taskIds}``.
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    marker_cleared = False
    try:
        from lib.message_queue import clear_autopilot_marker
        marker_cleared = clear_autopilot_marker(conv_id)
    except Exception as e:
        logger.warning('[Autopilot] disarm: marker clear failed for conv=%s: %s',
                       conv_id[:8], e)

    cleared_ids: list[str] = []
    with tasks_lock:
        for tid, t in tasks.items():
            if t.get('convId') != conv_id or t.get('_vu_subtask'):
                continue
            cfg = t.get('config')
            if isinstance(cfg, dict) and cfg.get('autopilot'):
                cfg['autopilot'] = False
                cleared_ids.append(tid)

    # ★ Symmetric close-out — the manual-stop arm of the conclude contract.
    #   Historically disarm was "dumb": it cleared the marker/flag but emitted
    #   NO run-level fact, forcing the frontend to INFER run-end from stream
    #   absence (the inter-turn-gap heuristic behind premature folds). Now we
    #   write the BACKEND-AUTHORITATIVE concluded record (reason=stopped, no
    #   report) so the fold keys on a durable fact — and return it so the
    #   calling client (which may have NO live SSE stream, the idle-disarm
    #   case) can fold instantly without a reload. Self-guards: no run id →
    #   None (nothing was ever an autopilot run to conclude).
    #
    # pt_00459503 slice 3: ``conclude_run`` was moved into a leaf module
    # (``autopilot_run_lifecycle``), so we import it at MODULE TOP now.
    # The cycle slice 2 guarded via lazy-import no longer exists — the
    # dependency graph is strictly one-way (see autopilot_run_lifecycle
    # docstring for the full picture).
    concluded = None
    try:
        concluded = conclude_run(conv_id, reason='stopped')
    except Exception as e:
        logger.warning('[Autopilot] disarm: conclude_run failed for conv=%s: %s',
                       conv_id[:8], e, exc_info=True)

    logger.info('[Autopilot] Disarmed conv=%s (markerCleared=%s, tasks=%s, concluded=%s)',
                conv_id[:8], marker_cleared, [t[:8] for t in cleared_ids],
                bool(concluded))
    audit_log('autopilot_disarmed', conv_id=conv_id,
              marker_cleared=marker_cleared, task_ids=cleared_ids,
              concluded=bool(concluded))
    result = {'disarmed': marker_cleared or bool(cleared_ids),
              'markerCleared': marker_cleared, 'taskIds': cleared_ids}
    if concluded is not None:
        result['runConcluded'] = concluded
    return result


__all__ = ['arm_autopilot', 'disarm_autopilot', '_marker_exists']
