"""Layer-3 preference (memory-profile) consolidation daemon.

  - ``_spawn_async_profile_consolidation`` / ``_run_profile_consolidation_async``
    — run the per-turn cheap-LLM preference consolidation in a daemon thread so
    it never sits on the loop-exit → ``done`` path.
  - ``_patch_assistant_message_with_prefs`` — persist ``_preferencesLearned``
    onto the conversation's assistant message after the SSE reader may close.

The daemon body resolves ``append_event`` and ``_patch_assistant_message_with_prefs``
THROUGH the facade module (``lib.tasks_pkg.commit_round``) at call time, so a
test/consumer that reassigns ``commit_round.append_event`` steers the call.

Dependency is one-directional: imports from ``lib.agent_core.events`` +
``lib.tasks_pkg.manager`` (append_event), plus ``lib.memory.profile_consolidate``
(lazily inside the daemon body), never the reverse.
"""

from __future__ import annotations

import threading

from lib.agent_core.events import EventType, build_event
from lib.log import get_logger
from lib.tasks_pkg.manager import append_event  # noqa: F401  (facade re-export target)

logger = get_logger(__name__)


def _spawn_async_profile_consolidation(task: dict, messages: list,
                                       cfg: dict | None = None) -> None:
    """Run the layer-3 preference consolidation in a daemon thread.

    Decoupled from ``_finalize_and_emit_done`` so the per-turn cheap-LLM
    consolidation round-trip can NEVER sit on the path between loop-exit and
    the ``done`` event — the user sees the turn finish immediately, and any
    "Noted: you prefer X" moment arrives a beat later as a post-done
    ``preference_learned`` event (best-effort live + persisted for reload).

    Gated on ``task['_profileConsolidateEligible']`` (set at the prefetch gate
    where ``memory_enabled``/``has_real_tools`` are in scope) and a clean
    finish (no error). ``messages`` is captured by reference — the consolidation
    pass only READS it (recent-surface extraction), so the post-done snapshot
    is fine.
    """
    if task.get('error') or not task.get('_profileConsolidateEligible'):
        return
    if not task.get('id'):
        return
    try:
        threading.Thread(
            target=_run_profile_consolidation_async,
            args=(task, messages),
            name=f'profile-consolidate-{task["id"][:8]}',
            daemon=True,
        ).start()
    except Exception as e:
        logger.warning('[Task:%s] failed to spawn consolidation thread: %s',
                       task['id'][:8], e, exc_info=True)


def _run_profile_consolidation_async(task: dict, messages: list) -> None:
    """Daemon-thread body: run consolidation, emit + persist learned prefs."""
    # Resolve event/DB helpers THROUGH the facade so a test's
    # ``monkeypatch.setattr(commit_round, 'append_event', ...)`` (and the
    # ``_patch_assistant_message_with_prefs`` stub) steers this daemon body.
    import lib.tasks_pkg.commit_round as _facade

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
            _facade.append_event(task, build_event(
                EventType.PREFERENCE_LEARNED,
                kind=pref.get('kind', ''),
                summary=pref.get('summary', ''),
                pending=bool(pref.get('pending')),
                id=pref.get('id', ''),
            ))
        except Exception as e:
            logger.debug('[Task:%s] preference_learned emit failed: %s', tid, e)

    # Persist onto the conversation's assistant message so the chip survives a
    # reload even when the SSE reader already closed (mirrors
    # _patch_assistant_message_with_git).
    try:
        _facade._patch_assistant_message_with_prefs(task, learned)
    except Exception as e:
        from lib.database import log_db_finalize_error
        log_db_finalize_error(logger, 'warning', e,
                              f'[Task:{tid}] persist preferences_learned failed')


def _patch_assistant_message_with_prefs(task: dict, learned: list) -> None:
    """Write ``_preferencesLearned`` onto the conversation's assistant message.

    Called from the consolidation daemon AFTER ``persist_task_result`` ran, so
    the chip is recoverable on reload. Mirrors
    :func:`_patch_assistant_message_with_git`: a field-level, rev-CAS patch of
    the one message this task owns — NOT a whole-transcript rewrite, which would
    erase rows a concurrent writer (autopilot VU append) added in between.
    """
    conv_id = task.get('convId') or ''
    task_id = task.get('id') or ''
    if not (conv_id and task_id and learned):
        return
    from lib.agent_core.store import get_conversation_store
    store = get_conversation_store()
    try:
        if store.patch_message_fields_by_task(
                conv_id, task_id, {'_preferencesLearned': learned}):
            logger.info('[Task:%s] persisted %d preference_learned to conv=%s',
                        task_id[:8], len(learned), conv_id[:8])
    except Exception as e:
        logger.warning('[Task:%s] preferences_learned DB write failed: %s',
                       task_id[:8], e, exc_info=True)
