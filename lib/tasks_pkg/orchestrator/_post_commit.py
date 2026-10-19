"""Shared post-commit observers and reconstructible task-state release.

Entry finish_committed_task. Call only after terminal persistence and producer
joining; does not stamp/publish/commit/reprice a terminal. Python and native
reuse the same observer order and existing optional background owners.
"""
from lib.log import get_logger
from lib.tasks_pkg.manager import notify_terminal_conversation_change

logger=get_logger(__name__)


def finish_committed_task(task,*,messages,cfg,project_enabled,project_path,
                          commit_round=None,consolidate_profile=None):
    tid=(task.get('id') or '')[:8]
    if commit_round is None:
        from lib.tasks_pkg.commit_round._commit import _spawn_async_commit_round
        commit_round=_spawn_async_commit_round
    if consolidate_profile is None:
        from lib.tasks_pkg.commit_round._profile import _spawn_async_profile_consolidation
        consolidate_profile=_spawn_async_profile_consolidation
    from lib.tasks_pkg.event_invariants import maybe_check_task_log
    maybe_check_task_log(task)
    # Terminal busy-state broadcast (): the CLEAR half of the busy
    # channel must be event-driven like the SET half is, not ride the next
    # incidental write. The status is terminal and the finalize latch is gone.
    notify_terminal_conversation_change(task)

    # The driver's only settle callback may have arrived while this Turn
    # still owned the lane. Its results remain unread; now a successor may
    # safely claim the committed lane through the same bounded policy.
    try:
        from lib.swarm.turn_handoff import resume_swarm_after_turn
        from lib.swarm.delivery import restore_unconsumed_results
        restore_unconsumed_results(task)
        from lib.tasks_pkg.manager import task_user_id
        resume_swarm_after_turn(task, user_id=int(task_user_id(task)))
    except Exception as _swarm_resume_error:
        logger.warning('[Task:%s] swarm completion handoff failed: %s',
                       tid, _swarm_resume_error, exc_info=True)
    if project_enabled and project_path and task.get('convId'):
        try:
            from lib.agent_core.settlement import (
                notify_project_task_settled,
            )
            from lib.tasks_pkg.manager import task_user_id

            notify_project_task_settled(
                task,
                project_path,
                user_id=int(task_user_id(task)),
            )
        except Exception as _project_settlement_error:
            logger.debug(
                '[Task:%s] project settlement hooks failed: %s',
                tid,
                _project_settlement_error,
            )
    # Capture the commit observer's one tool-round fact before release.
    commit_round(task, project_enabled, project_path,
                              project_paths=cfg.get('projectPaths'))

    # The existing release owner decides which terminal fields may be reclaimed.
    from lib.tasks_pkg.manager._persist import _release_heavy_task_state
    _release_heavy_task_state(task)

    # Layer-3 preference consolidation — OFF the hot path.
    #   Mirrors _spawn_async_commit_round: runs AFTER the done event +
    #   persist, in a daemon thread, so the user sees the turn finish
    #   WITHOUT waiting on a cheap-LLM round-trip (most turns yield no
    #   preference change, and under rate-limiting the cheap call can stall
    #   for seconds). Any learned/staged preference is delivered as a
    #   post-done `preference_learned` event (best-effort live via the same
    #   SSE/push fan-out) + persisted to the conversation DB for reload.
    #   Gated on the independent My Context capability inside the spawner, so
    #   headless surfaces that did not opt in still learn nothing.
    try:
        consolidate_profile(task, messages, cfg)
    finally:
        # Tool policy retires only after the joined terminal's observers have
        # captured their inputs. Early persistence still retains these graphs.
        from lib.tasks_pkg.manager._persist import _release_terminal_tool_state
        _release_terminal_tool_state(task)
