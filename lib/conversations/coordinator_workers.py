"""Create/delegate/wait adapters for independent ordinary worker conversations.

Authority is the current accepted human Coordinator request, frozen tool policy
and ProjectDispatchRepository grant. This module has no executor, task state,
transcript store or polling model loop. It observes existing task notifications,
and a wait yields to input already waiting for the Coordinator's next round
(``lib.agent_inbox`` lanes and the ``project_messages`` mailbox).
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy

from lib.project_dispatch.admission import require_isolated_worker_root
from lib.project_dispatch.repository import ProjectDispatchRepository, _validate_schema
from lib.project_dispatch.delegation import source_for_task, policy_for_task, validate_delegation_policy, issued_worker_policy
from lib.project_dispatch.service import ProjectDispatchService
from lib.log import get_logger

logger = get_logger(__name__)

_TERMINAL = frozenset({'completed', 'failed', 'interrupted', 'truncated', 'superseded', 'cancelled'})
# Receipts that end a delivery wait: the Coordinator acts on or reports the reason.
_HELD_DELIVERY = frozenset({'blocked', 'failed', 'preserved'})
# A terminal notification can precede the attempt's durable status, so a wait
# rechecks promptly for a bounded window after one. The slow recheck covers
# process-local notification loss and queued task admission.
_PROMPT_RECHECK_SECONDS = 0.5
_PROMPT_RECHECK_WINDOW_SECONDS = 10.0
_DURABLE_RECHECK_SECONDS = 5.0
# Inbox lanes delivered at the Coordinator's next round boundary that end a wait,
# as they end poll's blocking waits: a steer (human, forwarded or channel input)
# and another background completion of this conversation.
_PENDING_INPUT_MODES = ['user-steer', 'background-command']


def _input_waiting(task) -> bool:
    from lib.tasks_pkg.conversation_delivery import completion_conversation_id
    conversation_id = completion_conversation_id(task)
    if not conversation_id:
        return False
    from lib.agent_inbox import has_pending
    return has_pending(conversation_id, modes=_PENDING_INPUT_MODES)


def _incoming_project_messages(task):
    """IDs of incoming project messages still awaiting input, or None without a mailbox."""
    from lib.conversations.project_messages import ProjectMessageError, execute
    from lib.storage.errors import StorageError
    try:
        page = execute('get_project_messages', {'direction': 'incoming'}, task)
    except ProjectMessageError:
        return None
    except StorageError:
        logger.warning('Coordinator wait could not read its project mailbox task=%s', task.get('id'), exc_info=True)
        return None
    # A new submission is always on the newest page.
    return {message['id'] for message in page['messages'] if message['state'] == 'submitted'}


def get_workers(task, scope, arguments, *, repository=None):
    """Page thin facts from this original request; no transcript or execution side effect."""
    _validate_schema(arguments, 'getProjectWorkers')
    from lib.project_dispatch.continuation import original_delegation, worker_summaries
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    principal, source, _ = source_for_task(repository, task)
    origin = original_delegation(task, {}, repository=repository)
    budget_id = origin.get('policy', {}).get('requestBudgetId') or getattr(task.get('_coordinatorRequestBudget'), 'budget_id', None)
    value, _ = repository._load(repository._selection(sources=[origin['attemptId']],
        budgets=[budget_id] if budget_id else []))
    project_history = arguments.get('scope', 'request') == 'project'
    page = repository.history_page(arguments.get('after_id')) if project_history else None
    rows = [row for row in (page['rows'] if page else value['assignments'])
            if row['request']['coordinatorId'] == scope['conversation_id']
            and row['grant'].get('bindingRevision') == scope['binding_revision']
            and row['grant']['tenant'] == principal.tenant_id
            and (project_history or row['request'].get('delegation', {}).get('attemptId') == origin['attemptId'])
            and row['request']['profile']['mode'] != 'coordinator']
    cursor = arguments.get('after_id')
    if cursor is not None and not project_history:
        indices = [i for i, row in enumerate(rows) if row['id'] == cursor]
        if not indices:
            raise ValueError('Worker cursor is not in this request')
        rows = rows[indices[0] + 1:]
    limit = len(rows) if project_history else arguments.get('limit', 16)
    result = {'source_attempt_id': origin['attemptId'], 'workers': worker_summaries(repository, rows[:limit]),
              'scope': 'project' if project_history else 'request',
              'next_cursor': page['olderCursor'] if page else rows[limit - 1]['id'] if len(rows) > limit else None}
    for item, row in zip(result['workers'], rows):
        item['source_turn_id'] = row['request']['authorizationSource']['turnId']
        item['current_request'] = row['request']['authorizationSource'] == source
    from lib.project_dispatch.request_budget import request_view, source_policy
    budget_id = origin['policy'].get('requestBudgetId')
    if not budget_id:
        source = source_policy(repository, task)
        budget_id = source['id'] if source is not None else None
    if budget_id:
        result['request_usage'] = request_view(repository, value, budget_id)
    return result


def delegate(task, scope, arguments, *, repository=None, service=None, create=None):
    _validate_schema(arguments, 'delegateRequest')
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    _, source, attempt = source_for_task(repository, task)
    policy = policy_for_task(repository, task, attempt)
    from lib.project_dispatch.continuation import original_delegation, ensure_open
    origin = original_delegation(task, policy, repository=repository)
    budget_id = policy.get('requestBudgetId')
    current, _ = repository._load(repository._selection(sources=[origin['attemptId']],
        budgets=[budget_id] if budget_id else []))
    ensure_open(current, origin['attemptId'])
    issued = issued_worker_policy(repository, task)
    key = hashlib.sha256(json.dumps([origin['attemptId'], arguments['idempotency_key']],
                                   separators=(',', ':')).encode()).hexdigest()
    command = 'coordinator-delegate:' + key
    original = repository.find_command(command)
    # Stable identities cover both conversation creation and grant admission.
    # Nothing is relaunched just because the tool result acknowledgement was lost.
    request = {'projectPath': repository.project, 'coordinatorId': scope['conversation_id'],
        'recipientId': original['request']['recipientId'] if original else 'pending-worker',
        'authorizationSource': source, 'brief': arguments['brief'], 'model': issued.model,
        'budget': {},
        'profile': {'mode': 'project', 'tools': list(issued.tools), 'readPaths': ['.'], 'writePaths': ['.']},
        'delegation': origin}
    if arguments.get('dependencies'):
        request['dependencies'] = deepcopy(arguments['dependencies'])
    if arguments.get('rework_of'):
        request['reworkOf'] = deepcopy(arguments['rework_of'])
    if policy.get('requestBudgetId'):
        request['delegationInputHash'] = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    _validate_schema(request, 'request')
    if original is None:
        validate_delegation_policy(repository, current, request)
    from lib.project_dispatch.delegation import validate_source
    validate_source(repository, request)
    if original is None:
        # Refuse before any conversation or grant exists: every worker would fail binding.
        require_isolated_worker_root(repository.project)
    if create is None:
        from .coordinator_tools import _create
        create = _create
    worker = create(scope, {'title': arguments['title'], 'idempotency_key': key})
    request['recipientId'] = worker['conversation_id']
    assignment = repository.authorize_from_coordinator(request, task=task, command_id=command)
    service = service or ProjectDispatchService(repository)
    outcome = service.dispatch_authorized(repository.admission(assignment['id']))
    execution = outcome.get('execution') or {}
    return {'assignment_id': assignment['id'], 'conversation_id': request['recipientId'],
            'task_id': execution.get('taskId'), 'status': execution.get('status', outcome['dispatch']),
            'dispatch': outcome['dispatch'],
            'next_step': 'Delegate independent work, then await_project_workers for results. Review the actual evidence before reporting delivery.'}


def _settled_failure(service, assignment, turn_id):
    """Bounded cause from the settled worker Turn; the dispatch view carries none."""
    turn = service.turn_get(assignment['request']['recipientId'], turn_id,
                            user_id=assignment['grant']['owner']) or {}
    error = (turn.get('settlement') or {}).get('error') or (turn.get('projection') or {}).get('error')
    if not isinstance(error, dict):
        return None
    return {key: str(error[key])[:1000] for key in ('kind', 'detail', 'hint') if error.get(key)} or None


def await_workers(task, scope, arguments, *, repository=None, service=None, runtime_context=None):
    """Wait on existing terminal notifications; no repeated LLM status calls.

    Returns when any Worker unsettled at call start settles (wait=progress names
    the pending rest), at once when all already settled, on timeout, on Stop, or
    when input waits for the Coordinator's next round (wait=pending_input names
    the pending Workers): a human message or other queued steer, another
    background completion, or a project message submitted after the call
    started. Polls read settlement evidence only; returned rows carry excerpts
    and causes.
    """
    _validate_schema(arguments, 'awaitWorkersRequest')
    repository = repository or ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    service = service or ProjectDispatchService(repository)
    ids = arguments['assignment_ids']
    if len(set(ids)) != len(ids):
        raise ValueError('Worker assignment IDs must be distinct')
    def snapshots():
        rows = []
        for identity in ids:
            assignment = repository.read(identity)
            if assignment['request']['coordinatorId'] != scope['conversation_id']:
                raise PermissionError('Worker belongs to another Coordinator')
            repository.validate_grant_binding(assignment)
            view = service.view_for_coordinator(identity, scope=scope)
            execution = view.get('execution') or {}
            result = execution.get('result') or {}
            content = result.get('content') or ''
            row = {'assignment_id': identity, 'conversation_id': assignment['request']['recipientId'],
                   'task_id': execution.get('taskId'), 'turn_id': execution.get('turnId'),
                   'result_revision': execution.get('resultRevision'), 'delivery': view.get('delivery'),
                   'canonical_delivery': view.get('canonicalDelivery', False),
                   'status': 'cancelled' if assignment['grant']['revoked'] else execution.get('status', 'queued'),
                   'result_excerpt': content[:4000] if isinstance(content, str) else '',
                   'result_truncated': isinstance(content, str) and len(content) > 4000}
            if row['status'] in ('failed', 'interrupted', 'truncated') and row['turn_id']:
                failure = _settled_failure(service, assignment, row['turn_id'])
                if failure:
                    row['error'] = failure
            rows.append(row)
        return rows
    def poll(identities):
        """Execution status and delivery receipt; no projection, excerpt or dependency reads."""
        rows = []
        for identity in identities:
            view = service.view_for_coordinator(identity, scope=scope, settlement_only=True)
            execution = view.get('execution') or {}
            rows.append({'assignment_id': identity, 'task_id': execution.get('taskId'),
                         'status': 'cancelled' if view['revoked'] else execution.get('status', 'queued'),
                         'delivery': view.get('delivery'),
                         'canonical_delivery': view.get('canonicalDelivery', False)})
        # A completed observation proves this waiter is responsive even when
        # Workers have no new output. A blocked read or transport-only ticker
        # must not refresh liveness or exempt a genuinely wedged handler.
        task['_dispatch_heartbeat'] = time.time()
        return rows
    def settled(row):
        if arguments.get('wait_for') == 'delivery':
            return (row['canonical_delivery'] or (row['delivery'] or {}).get('status') in _HELD_DELIVERY
                    or row['status'] in _TERMINAL - {'completed'})
        return row['status'] in _TERMINAL
    # Only Workers unsettled at call start can end the wait early, so a call that
    # also names already-settled Workers never returns at once in a model loop.
    rows = [row for row in poll(ids) if not settled(row)]
    opened = [row['assignment_id'] for row in rows]
    outcome = 'settled'
    if opened:
        from lib.agent_core.admission import on_terminal
        changed, subscriptions = threading.Event(), {}
        if runtime_context is not None:
            runtime_context.register_cancel_callback(changed.set)
        deadline = time.monotonic() + arguments.get('timeout_seconds', 1800)
        prompt_until = 0.0
        # Messages already waiting at call start are acquired by the Coordinator's
        # rounds; only later submissions end the wait, as with Workers above.
        known_messages = _incoming_project_messages(task)
        def message_arrived():
            current = _incoming_project_messages(task) if known_messages is not None else None
            return bool(current and current - known_messages)
        try:
            while True:
                if any(settled(row) for row in rows):
                    outcome = 'progress'
                    break
                if task.get('aborted'):
                    outcome = 'cancelled'
                    break
                if (runtime_context is not None
                        and runtime_context.resource_cancel_event.is_set()):
                    outcome = 'timeout'
                    break
                if _input_waiting(task) or message_arrived():
                    outcome = 'pending_input'
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    outcome = 'timeout'
                    break
                fresh = [row['task_id'] for row in rows if row['task_id'] and row['status'] not in _TERMINAL
                         and row['task_id'] not in subscriptions]
                for task_id in fresh:
                    subscriptions[task_id] = on_terminal(task_id, lambda _task_id: changed.set())
                if fresh:
                    rows = poll(opened)  # Close subscribe-versus-settlement race.
                    continue
                # The wait is interruptible and allocates no thread or model request.
                # Durable attempt/delivery evidence alone declares settlement.
                now = time.monotonic()
                check_at = now + min(remaining, _PROMPT_RECHECK_SECONDS if now < prompt_until
                                     else _DURABLE_RECHECK_SECONDS)
                while (not task.get('aborted') and not _input_waiting(task)
                       and not changed.wait(min(0.25, max(0, check_at - time.monotonic())))):
                    if time.monotonic() >= check_at:
                        break
                if changed.is_set():
                    changed.clear()
                    prompt_until = time.monotonic() + _PROMPT_RECHECK_WINDOW_SECONDS
                rows = poll(opened)
        finally:
            for unsubscribe in subscriptions.values():
                unsubscribe()
    rows = snapshots()
    if outcome == 'cancelled':
        return {'workers': rows, 'wait': 'cancelled'}
    pending = [row['assignment_id'] for row in rows if not settled(row)]
    if outcome == 'timeout' and set(opened) <= set(pending):
        return {'workers': rows, 'wait': 'timeout', 'instruction': 'Workers remain active; timeout is not failure. Wait again with these same assignment IDs.'}
    held = [row['assignment_id'] for row in rows if arguments.get('wait_for') == 'delivery'
            and not row['canonical_delivery'] and (row['delivery'] or {}).get('status') in _HELD_DELIVERY]
    delivery = (' Delivery of ' + ', '.join(held) + ' is blocked, failed or preserved, not canonical: act on '
                'its delivery.reason (review_project_work with the exact checkpoint_sha if publication was '
                'never requested, rework for a conflict or failed gate) or report a cause you cannot resolve. '
                'Awaiting only held deliveries returns at once.') if held else ''
    # Workers that all settled meanwhile return as settled; the input still follows.
    if outcome == 'pending_input' and pending:
        return {'workers': rows, 'wait': 'pending_input',
                'instruction': 'New input is waiting for you and arrives right after this result: a human '
                               'message or steer, a project message, or a background completion. Workers keep '
                               'running; still pending: ' + ', '.join(pending) + '. Answer or act on that input '
                               'first, then call await_project_workers again with only the pending assignment IDs '
                               'and the same wait_for. Ending a wait for input is not failure.' + delivery}
    if pending:
        return {'workers': rows, 'wait': 'progress',
                'instruction': 'Some selected workers settled; still pending: ' + ', '.join(pending) + '. '
                               'Review the settled evidence and arrange follow-up work now, then call '
                               'await_project_workers again with only the pending assignment IDs and the same '
                               'wait_for. Failed/cancelled work is not delivery.' + delivery}
    return {'workers': rows, 'wait': 'settled',
            'instruction': 'Read full worker evidence using the conversation/Turn links where needed. Failed/cancelled work is not delivery; verify and continue the original objective.' + delivery}
