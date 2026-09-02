"""Layer-3 preference (memory-profile) consolidation daemon.

  - ``_spawn_async_profile_consolidation`` / ``_run_profile_consolidation_async``
    — run the per-turn cheap-LLM preference consolidation in a daemon thread so
    it never sits on the loop-exit → ``done`` path.
  - ``_patch_turn_with_prefs`` — fold learned preferences into the settled
    turn's provenance block after the SSE reader may have closed.

Dependency is one-directional: imports from ``lib.agent_core.events`` +
``lib.tasks_pkg.manager`` (append_event), plus ``lib.memory.profile_consolidate``
(lazily inside the daemon body), never the reverse.
"""

from __future__ import annotations

import threading

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager._events import append_event

logger = get_logger(__name__)

# My Context is one owner-scoped document. Serialize the best-effort learner so
# concurrent conversations cannot race a read-modify-write or create an
# unbounded burst of background model calls on a personal computer. A busy
# learner drops the later advisory pass; the conversation itself is unaffected.
_PROFILE_CONSOLIDATION_SLOT = threading.BoundedSemaphore(value=1)


def _spawn_async_profile_consolidation(task: dict, messages: list,
                                       cfg: dict | None = None) -> None:
    """Run the layer-3 preference consolidation in a daemon thread.

    Decoupled from ``_finalize_and_emit_done`` so the per-turn cheap-LLM
    consolidation round-trip can NEVER sit on the path between loop-exit and
    the ``done`` event — the user sees the turn finish immediately, and any
    "Noted: you prefer X" moment arrives a beat later as a post-done
    ``preference_learned`` event (best-effort live + persisted for reload).

    Gated on ``task['_profileConsolidateEligible']`` (the independent My Context
    capability) and a clean finish (no error). ``messages`` is captured by
    reference — the consolidation pass only READS it (recent-surface
    extraction), so the post-done snapshot is fine. At most one pass is active
    process-wide; saturation fails soft instead of queuing unbounded work.
    """
    if task.get('error') or not task.get('_profileConsolidateEligible'):
        return
    if not task.get('id'):
        return
    if not _PROFILE_CONSOLIDATION_SLOT.acquire(blocking=False):
        logger.debug('[Task:%s] profile consolidation skipped: worker busy',
                     task['id'][:8])
        return
    try:
        threading.Thread(
            target=_run_profile_consolidation_with_slot,
            args=(task, messages),
            name=f'profile-consolidate-{task["id"][:8]}',
            daemon=True,
        ).start()
    except Exception as e:
        _PROFILE_CONSOLIDATION_SLOT.release()
        logger.warning('[Task:%s] failed to spawn consolidation thread: %s',
                       task['id'][:8], e, exc_info=True)


def _run_profile_consolidation_with_slot(task: dict, messages: list) -> None:
    """Own the single background slot across model dispatch and profile write."""
    try:
        _run_profile_consolidation_async(task, messages)
    finally:
        _PROFILE_CONSOLIDATION_SLOT.release()


def _run_profile_consolidation_async(task: dict, messages: list) -> None:
    """Daemon-thread body: run consolidation, emit + persist learned prefs."""
    tid = task['id'][:8]
    try:
        from lib.memory.profile_consolidate import run_profile_consolidation
        learned = run_profile_consolidation(messages, task=task)
    except Exception as e:
        logger.warning('[Task:%s] profile consolidation failed: %s',
                       tid, e, exc_info=True)
        return
    if not learned:
        return

    task['_preferencesLearned'] = learned
    # Best-effort LIVE delivery: append_event fans out over SSE + push to any
    # still-connected client (and a disconnected client recovers it via the
    # DB patch below on reload).
    for pref in learned:
        try:
            append_event(task, build_event(
                EventType.PREFERENCE_LEARNED,
                kind=pref.get('kind', ''),
                summary=pref.get('summary', ''),
                pending=bool(pref.get('pending')),
                id=pref.get('id', ''),
                change_id=pref.get('change_id', pref.get('id', '')),
                item_id=pref.get('item_id', ''),
                context_type=pref.get('type', ''),
            ))
        except Exception as e:
            logger.debug('[Task:%s] preference_learned emit failed: %s', tid, e)

    # Fold onto the settled turn so the chip survives a reload even when the
    # SSE reader already closed.
    try:
        _patch_turn_with_prefs(task, learned)
    except Exception as e:
        logger.warning(
            '[Task:%s] persist preferences_learned failed: %s',
            tid, e, exc_info=True)


def _patch_turn_with_prefs(task: dict, learned: list) -> None:
    """Fold learned preferences into THIS task's settled turn projection.

    Called from the consolidation daemon AFTER ``persist_task_result`` ran, so
    the chip is recoverable on reload. The daemon runs after the attempt may
    already be terminal, so ordinary attempt events can no longer mutate the
    row. Use the settled-turn CAS command and retry only a genuine concurrent
    projection revision; never overwrite siblings. This is the ONLY
    persistence path — turn projections strip identity keys (``_taskId``) on
    write, so the retired legacy bridge that located the row by
    ``projection._taskId`` could never match.
    """
    conv_id = task.get('convId') or ''
    task_id = task.get('id') or ''
    if not (conv_id and task_id and learned):
        return
    from lib.tasks_pkg.manager._registry import task_user_id
    user_id = task_user_id(task)

    turn_id = task.get('_turnId') or ''
    if turn_id:
        try:
            from lib.turn_lifecycle import (
                LifecycleConflict,
                _task_projection,
                get_turn,
                update_turn_projection,
            )
            for attempt in range(3):
                current = get_turn(conv_id, turn_id, user_id=user_id)
                current_projection = dict(current.get('projection') or {})
                enriched = _task_projection(task, current_projection)
                provenance = enriched.get('provenance')
                if provenance == current_projection.get('provenance'):
                    break
                next_projection = dict(current_projection)
                next_projection['provenance'] = provenance
                try:
                    update_turn_projection(
                        conv_id,
                        turn_id,
                        projection=next_projection,
                        expected_projection_revision=int(
                            current.get('projectionRevision') or 0),
                        user_id=user_id,
                    )
                    logger.info(
                        '[Task:%s] persisted %d preference_learned to turn=%s',
                        task_id[:8], len(learned), turn_id[:8],
                    )
                    break
                except LifecycleConflict:
                    if attempt == 2:
                        raise
        except Exception as exc:
            logger.warning(
                '[Task:%s] turn-native preferences_learned patch failed: %s',
                task_id[:8], exc, exc_info=True,
            )
