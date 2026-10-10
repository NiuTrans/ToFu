"""Parent-turn handoff for conversation-owned background swarms.

``prepare_swarm_turn_end`` detaches work before the parent commits its terminal
Turn; ``resume_swarm_after_turn`` closes the last-inbox-drain/settlement race
AFTER that commit. This adapter owns no queue, retry budget or lifecycle state:
the session/inbox retain child evidence and integration owns continuation policy.
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
    unread = {str(item.get('agent_id') or '')
              for item in agent_inbox.pending_items(key, modes=['swarm-update'])}
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
