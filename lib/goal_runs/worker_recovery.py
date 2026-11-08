"""Re-elect an interrupted Goal after its durable worker proves physical exit.

Entry: resume_after_worker_exit, called after the fenced Attempt/result receipt.
The existing continuity pass and Goal recovery owner make every decision. This
adapter owns no queue, journal, budget or retry and never enters the old task.
"""
def resume_after_worker_exit(context, attempt, *, application=None):
    if attempt.get('status') != 'interrupted':
        return
    from lib import turn_lifecycle as turns
    owner = context.principal.require_owner(context='Goal worker recovery')
    turn = turns.get_turn(attempt['conversationId'], attempt['turnId'], user_id=owner)
    if (turn.get('settlement') or {}).get('cause') != 'server_restart':
        return
    from lib.goal_runs.contract import GOAL_RUN_CREATED_BY, goal_run_id_for_task
    from lib.orchestration.run_service import OrchestrationRunService
    from lib.orchestration.sidecar_run_store import SidecarOrchestrationRunStore
    tenant = context.principal.tenant_id or ''
    runs = OrchestrationRunService(SidecarOrchestrationRunStore(owner, tenant_id=tenant))
    source = goal_run_id_for_task(context.claim.task_id)
    run = runs.get_execution_snapshot(source)
    if run is None or run.get('created_by') != GOAL_RUN_CREATED_BY:
        return
    from lib.goal_runs.native_recovery import recovery_options
    from lib.goal_runs.recovery import resume_chat_goal
    from lib.orchestration.continuity import run_boot_continuity_pass
    def resume(*args, **kwargs):
        return resume_chat_goal(*args, **kwargs, **recovery_options(application=application))
    return run_boot_continuity_pass(
        candidates=[dict(run_id=source, user_id=owner, tenant_id=tenant,
                         created_by=GOAL_RUN_CREATED_BY)],
        auto_created_by=frozenset({GOAL_RUN_CREATED_BY}),
        run_service_for_owner=lambda accepted_owner, accepted_tenant: runs,
        resume_for_created_by={GOAL_RUN_CREATED_BY: resume},
        source_cursor_bound_created_by={GOAL_RUN_CREATED_BY})
