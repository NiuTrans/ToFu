"""Public receipt projection from committed mailbox facts; never input authority.

Reuses task replay, conversation push and Swarm snapshots. Publication failures
cannot undo a mailbox commit or acknowledge model consumption.
"""
from __future__ import annotations

from collections import OrderedDict
import logging
import os
import re
import threading
import time

from lib.log import get_logger, log_event

from lib.swarm.message_contract import LIMITS

logger = get_logger(__name__)
RECEIPT_FIELDS = ('state', 'senderTaskId', 'submittedAt', 'content', 'kind',
                  'messageSequence', 'inputInjectedAt',
                  'inputTaskId', 'inputRound', 'inputInjectedRound', 'inputConfirmedAt')

# Activity polls and snapshot persists re-read every mailbox, so one broken
# run would otherwise warn on each poll. Report at most one failure and one
# recovery line per run per interval, carrying the suppressed repeat count.
_SNAPSHOT_FAILURE_LOG_INTERVAL_S = 300.0
_SNAPSHOT_FAILURE_LOG_RUNS = 256
_snapshot_failures_lock = threading.Lock()
_snapshot_failures: OrderedDict[str, dict] = OrderedDict()
_ERROR_CODE = re.compile(r'[a-z][a-z0-9_]{0,63}')


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


def _failure_signature(error: BaseException) -> str:
    """Class, typed code and raise site; exception text may carry mailbox content."""
    signature = type(error).__name__
    code = getattr(error, 'code', None)
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        signature += ':' + code
    frame = error.__traceback__
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    if frame is not None:
        origin = frame.tb_frame.f_code
        signature += f'@{os.path.basename(origin.co_filename)}:{origin.co_name}'
    return signature[:200]


def _report_snapshot_failure(run_id: str, signature: str, failed: int, total: int) -> None:
    now = time.monotonic()
    with _snapshot_failures_lock:
        state = _snapshot_failures.get(run_id)
        if state is not None and now - state['reported_at'] < _SNAPSHOT_FAILURE_LOG_INTERVAL_S:
            state['suppressed'] += 1
            state['failing'] = True
            _snapshot_failures.move_to_end(run_id)
            return
        suppressed = state['suppressed'] if state is not None else 0
        _snapshot_failures[run_id] = {'reported_at': now, 'suppressed': 0, 'failing': True}
        _snapshot_failures.move_to_end(run_id)
        while len(_snapshot_failures) > _SNAPSHOT_FAILURE_LOG_RUNS:
            _snapshot_failures.popitem(last=False)
    log_event(logger, logging.WARNING, 'swarm_receipt_snapshot_read_failed',
              'Swarm receipt snapshot read failed run=%s error=%s failed_targets=%d/%d '
              'suppressed_repeats=%d', run_id, signature, failed, total, suppressed,
              swarm_run_id=run_id, error=signature, failed_targets=failed,
              total_targets=total, suppressed_repeats=suppressed)


def _report_snapshot_success(run_id: str) -> None:
    now = time.monotonic()
    with _snapshot_failures_lock:
        state = _snapshot_failures.get(run_id)
        # Inside the window a flapping run keeps its state, so failure and
        # recovery lines stay bounded by the same interval.
        if (state is None or not state['failing']
                or now - state['reported_at'] < _SNAPSHOT_FAILURE_LOG_INTERVAL_S):
            return
        del _snapshot_failures[run_id]
    if state['suppressed']:
        logger.info('Swarm receipt snapshot read recovered run=%s suppressed_repeats=%d',
                    run_id, state['suppressed'])


def snapshot_receipts(session, agents: list[dict], specs: list) -> dict:
    """Read bounded owned mailboxes once; the parent is not a child row.

    Child invocation IDs are random task identities, not assignment IDs. Only
    an exact live binding or a mailbox input witness can join the two. A resumed
    assignment can therefore have more than one participant task identity.
    """
    from lib.swarm.run_messages import RunMessageRepository, binding_for, scope_key, validate_scope
    from lib.swarm.team_messages import coordinator_binding
    from lib.tasks_pkg.manager import task_user_id

    receipts, participants, versions = [], [], {}
    failures: list[BaseException] = []
    # Every target shares the session's conversation scope: read it once per
    # snapshot, not once per agent (an O(N) snapshot per event made it O(N^2)).
    scope_verdicts: dict[tuple, BaseException | None] = {}
    agent_rows = {agent['id']: agent for agent in agents}
    targets = [(spec.id, spec.role, spec) for spec in specs]
    # Run parent identity is topology, not the project Coordinator role.
    targets.append((session.task_id, 'Main agent', None))
    for agent_id, label, spec in targets:
        try:
            binding = (binding_for(session._parent_task_proxy, spec, session.inbox_key,
                                   run_id=session.task_id)
                       if spec is not None else coordinator_binding(session))
            if (binding['owner'] != session.user_id
                    or binding['conversationId'] != session.conv_id):
                raise ValueError('Snapshot owner does not match mailbox owner')
            key = scope_key(binding)
            if key not in scope_verdicts:
                try:
                    validate_scope(binding)
                    scope_verdicts[key] = None
                except Exception as error:
                    # One scope's failure must not hide another agent's mailbox.
                    logger.debug('Swarm snapshot scope rejected for %s: %s', agent_id, error)
                    scope_verdicts[key] = error
            if scope_verdicts[key] is not None:
                failures.append(scope_verdicts[key])
                continue
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
                # This session's live invocation of the assignment is the proof;
                # its execution parent may be a later caller, not the run root.
                child = getattr(session._agents.get(agent_id), '_child', None)
                if (isinstance(child, dict) and child.get('_swarmMessageBinding') == binding
                        and task_user_id(child) == binding['owner']
                        and str(child.get('_tenant_id') or '') == binding['tenant']
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
        except Exception as error:
            failures.append(error)
    if failures:
        _report_snapshot_failure(session.task_id, _failure_signature(failures[0]),
                                 len(failures), len(targets))
    else:
        _report_snapshot_success(session.task_id)
    return {'messageReceipts': receipts, 'messageReceiptsVersions': versions,
            'messageReceiptsAvailable': not failures, 'messageParticipants': participants}
