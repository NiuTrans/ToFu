"""Parent-turn handoff for conversation-owned background swarms.

``prepare_swarm_turn_end`` detaches work before the parent commits its terminal
Turn; ``resume_swarm_after_turn`` closes the last-inbox-drain/settlement race
AFTER that commit. ``own_detached_swarm`` keeps a durable spawning attempt's
worker claim live for that detached work after its Turn settles. This adapter
owns no queue, retry budget or lifecycle state: the session/inbox retain child
evidence and integration owns continuation policy.
"""

from __future__ import annotations

from lib import agent_inbox
from lib.identity import require_user_id
from lib.log import get_logger

logger = get_logger(__name__)


def prepare_swarm_turn_end(
    task: dict, *, user_id: int, finish_reason: str,
) -> None:
    """Detach without consuming evidence; an explicit Stop only signals work.

    A terminal child result may have arrived after the parent's final inbox
    drain. Neither natural settlement nor Stop deletes its durable session.
    The bounded session registry retains results for review/recovery and owns
    eventual memory cleanup. Stop tombstones only the swarm notification lane;
    unrelated background-command/peer evidence must not be discarded.
    """
    from lib.swarm.integration import (
        SWARM_AUTOCONTINUE_ENABLED,
        get_active_session,
        swarm_key_for,
    )

    owner_id = require_user_id(user_id, context='swarm parent handoff')
    key = swarm_key_for(task)
    session = get_active_session(key)
    if session is None or session.user_id != owner_id:
        return
    if task.get('aborted'):
        if task.get('_abort_reason') == 'server_shutdown':
            session.suspend_for_restart()
            return
        session.abort()
        agent_inbox.clear(key, modes=['swarm-update'])
        task.pop('_waiting_on', None)
        return

    # Turn completion is distinct from delivery completion. Use canonical
    # specs/status and unread inbox evidence, not prose or advisory checklists.
    if task.get('error') or finish_reason not in {'stop', 'end_turn', 'incomplete'}:
        task.pop('_waiting_on', None)
        return
    status = session.get_status()
    presented = task.get('_swarm_inject_pending') or []
    unread = {str(item.get('agent_id') or '')
              for item in [*agent_inbox.pending_items(key, modes=['swarm-update']), *presented]
              if not item.get('swarm_run_id') or item['swarm_run_id'] == session.task_id}
    dependencies = []
    for agent_id, info in status.items():
        if not isinstance(info, dict) or info.get('required_for_completion', True) is False:
            continue
        child_status = str(info.get('status') or 'unknown')
        # A terminated session with an unknown child is unavailable, not done.
        running = child_status in {'pending', 'running', 'retrying', 'stalled'} and not session.is_terminated
        unavailable = child_status == 'unknown'
        if running or str(agent_id) in unread or unavailable:
            dependencies.append({'id': str(agent_id), 'role': str(info.get('role') or ''),
                                 'status': child_status,
                                 'state': ('running' if running else 'unavailable' if unavailable else 'awaiting_integration')})
    if not dependencies:
        task.pop('_waiting_on', None)
        return
    logger.info('[Swarm:%s] parent turn ended with %d unresolved delivery dependencies', key, len(dependencies))
    task['_waiting_on'] = {
        'kind': 'swarm', 'swarmKey': key,
        'autoResume': bool(SWARM_AUTOCONTINUE_ENABLED),
        'agents': dependencies[:8], 'totalAgents': len(dependencies),
    }


def resume_swarm_after_turn(task: dict, *, user_id: int) -> None:
    """Recheck a settled session after the parent's durable done event.

    The driver may have stood down because this parent was still running, or
    its dispatch may have lost the lane-busy race during terminal persistence.
    Reuse exactly the same source identity and policy as the driver; it owns
    duplicate suppression, cancellation fences and bounded continuation. An
    active swarm is never a reason to wake the parent before its batch settles.
    """
    from lib.swarm.integration import get_active_session, swarm_key_for

    owner_id = require_user_id(user_id, context='swarm parent continuation')
    if not task.get('convId') or task.get('aborted'):
        return
    key = swarm_key_for(task)
    session = get_active_session(key)
    if (session is None or session.user_id != owner_id
            or not (session.is_terminated or getattr(session, 'delivery_dependencies_settled', False))):
        return
    if not agent_inbox.has_pending(key, modes=['swarm-update']):
        return
    if session.on_settled is not None:
        session.on_settled()


def own_detached_swarm(context, *, resume: bool) -> None:
    """Keep the spawning attempt's claim for its detached work, then release it.

    Called by the conversation worker after the attempt's outcome is known and
    outside its dispatch stripe. ``resume`` is the takeover case: the session
    retained by a previous owner restarts under this claim first. Neither step
    may change the attempt's already settled outcome; a failure leaves the
    retained checkpoints inspectable and releases the claim.
    """
    try:
        if resume:
            from lib.swarm.integration import resume_detached_swarm_for_claim
            resume_detached_swarm_for_claim(context)
        hold_detached_swarm(context)
    except Exception:
        logger.error('[Swarm] detached work of %s could not keep its worker claim',
                     context.claim.task_id, exc_info=True)


def hold_detached_swarm(context) -> None:
    """Return once no live session can still run or birth work under this attempt.

    Every birth, checkpoint and model admission of a durable attempt's
    descendants requires that attempt's current claim, and a swarm outlives
    its parent Turn (``prepare_swarm_turn_end``). So the attempt's worker job
    stays claimed until those sessions terminate. Its Stop reaches them through
    the session's cancellation observer (``MasterOrchestrator``). Work left
    unfinished when this process shuts down keeps the job unfinished: the
    worker shutdown that follows defers this claim, and the next owner resumes
    the retained session (``resume_detached_swarm_for_claim``). Shutdown may
    suspend a session before this attempt first looks, so the retained row,
    not the live registry, decides that (``detached_swarm_unfinished``).
    """
    from lib.swarm.integration import detached_swarm_unfinished, sessions_holding_parent
    import lib.tasks_pkg.manager as manager
    while sessions := sessions_holding_parent(context.claim.task_id):
        for session in sessions:
            session.wait_terminated()
    if context.shutdown_event is None or not manager.task_admission_closed():
        return
    try:
        unfinished = detached_swarm_unfinished(context)
    except Exception:
        # Deferring is always safe at shutdown: the next owner decides again.
        logger.warning('[Swarm] retained session of %s is unreadable at shutdown; '
                       'deferring its claim', context.claim.task_id, exc_info=True)
        unfinished = True
    if unfinished:
        context.shutdown_event.wait()
