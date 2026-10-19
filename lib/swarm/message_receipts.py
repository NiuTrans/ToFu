"""Public receipt projection from committed mailbox facts; never input authority.

Reuses task replay, conversation push and Swarm snapshots. Publication failures
cannot undo a mailbox commit or acknowledge model consumption.
"""
from __future__ import annotations

from lib.log import get_logger

from lib.swarm.message_contract import LIMITS

logger = get_logger(__name__)
RECEIPT_FIELDS = ('state', 'senderTaskId', 'submittedAt', 'content', 'kind',
                  'messageSequence', 'inputInjectedAt',
                  'inputTaskId', 'inputRound', 'inputInjectedRound', 'inputConfirmedAt')


def receipt_fields(binding: dict, item: dict) -> dict:

    # Defense in depth for old/corrupt rows: never truncate authored evidence.
    if 'content' in item:
        content = item['content']
        if (not isinstance(content, str) or len(content) > LIMITS['messageChars']
                or len(content.encode('utf-8')) > LIMITS['messageBytes']):
            raise ValueError('Invalid bounded receipt content')
    return {'swarmRunId': binding['swarmRunId'], 'agentId': binding['agentId'],
            'messageId': item['id'],
            **{key: item[key] for key in RECEIPT_FIELDS if key in item}}


def publish_receipt(binding: dict, item: dict) -> None:
    from lib.agent_core.events import EventType, build_event
    from lib.tasks_pkg.manager import append_event, task_user_id
    from lib.tasks_pkg.manager.runtime import chat_task_runtime

    event = build_event(EventType.SWARM_MESSAGE_RECEIPT, **receipt_fields(binding, item))
    # Parent replay and the sending task are independent observers. Never route
    # an old receipt onto a replacement task or infer ownership from a handle.
    for task_id in {binding['swarmRunId'], item['senderTaskId']}:
        try:
            task = chat_task_runtime.get(task_id)
            if (task is not None and task_user_id(task) == binding['owner']
                    and str(task.get('_tenant_id') or '') == binding['tenant']
                    and task.get('convId') == binding['conversationId']):
                append_event(task, dict(event))
        except Exception:
            logger.warning('Swarm message replay publication failed')
    if binding['conversationId']:
        try:
            from lib.agent_core.push import push_event
            push_event('swarm', binding['conversationId'],
                       {**event, 'swarmId': binding['swarmRunId']}, user_id=binding['owner'])
        except Exception:
            logger.warning('Swarm message push publication failed')


def persist_receipt_snapshot(binding: dict) -> None:
    """Refresh once per mailbox commit, including batched injection/close."""
    try:
        from lib.swarm.integration import get_active_session
        session = get_active_session(binding['swarmKey'])
        if (session is not None and session.task_id == binding['swarmRunId']
                and session.user_id == binding['owner']
                and session.conv_id == binding['conversationId']
                and str(session._parent_task_proxy.get('_tenant_id') or '') == binding['tenant']):
            session.persist_message_receipts()
    except Exception:
        logger.warning('Swarm message snapshot publication failed')


def snapshot_receipts(session, agents: list[dict], specs: list) -> dict:
    """Read bounded owned mailboxes once; the parent is not a child row.

    Child invocation IDs are random task identities, not assignment IDs. Only
    an exact live binding or a mailbox input witness can join the two. A resumed
    assignment can therefore have more than one participant task identity.
    """
    from lib.swarm.run_messages import RunMessageRepository, binding_for, validate_target
    from lib.swarm.team_messages import coordinator_binding
    from lib.tasks_pkg.manager import task_user_id

    receipts, participants, versions = [], [], {}
    available = True
    agent_rows = {agent['id']: agent for agent in agents}
    targets = [(spec.id, spec.role, spec) for spec in specs]
    # Run parent identity is topology, not the project Coordinator role.
    targets.append((session.task_id, 'Main agent', None))
    for agent_id, label, spec in targets:
        try:
            binding = (binding_for(session._parent_task_proxy, spec, session.inbox_key)
                       if spec is not None else coordinator_binding(session))
            if (binding['owner'] != session.user_id
                    or binding['conversationId'] != session.conv_id
                    or binding['swarmRunId'] != session.task_id):
                raise ValueError('Snapshot owner does not match mailbox owner')
            validate_target(binding, active_retained=False)
            payload, revision = RunMessageRepository(binding).load()
            agent = agent_rows.get(agent_id)
            rows = [receipt_fields(binding, {**item, 'messageSequence': sequence,
                    **({'state': 'unconfirmed'} if agent is not None
                       and agent['status'] in ('done', 'failed', 'aborted')
                       and item['state'] == 'submitted' else {})})
                    for sequence, item in enumerate(payload['messages'], 1)]
            task_ids = {row['inputTaskId'] for row in rows
                        if isinstance(row.get('inputTaskId'), str) and row['inputTaskId']}
            if spec is None:
                task_ids = {session.task_id}
            else:
                child = getattr(session._agents.get(agent_id), '_child', None)
                if (isinstance(child, dict) and child.get('_swarmMessageBinding') == binding
                        and task_user_id(child) == binding['owner']
                        and str(child.get('_tenant_id') or '') == binding['tenant']
                        and child.get('_parentTaskId') == session.task_id
                        and isinstance(child.get('id'), str) and child['id']):
                    task_ids.add(child['id'])
            participants.extend({'id': agent_id, 'label': label, 'taskId': task_id}
                                for task_id in sorted(task_ids))
            if not task_ids:
                participants.append({'id': agent_id, 'label': label})
            receipts.extend(rows)
            versions[agent_id] = revision
            if agent is not None:
                agent['messageReceiptsVersion'] = revision
                agent['messageReceipts'] = rows
        except Exception:
            available = False
            logger.warning('Swarm receipt snapshot read failed')
    return {'messageReceipts': receipts, 'messageReceiptsVersions': versions,
            'messageReceiptsAvailable': available, 'messageParticipants': participants}
