"""Create/delegate/wait adapters for independent ordinary worker conversations.

Authority is the current accepted human Coordinator request, frozen tool policy
and ProjectDispatchRepository grant. This module has no executor, task state,
transcript store or polling model loop. It observes existing task notifications.
"""
from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy

from lib.project_dispatch.repository import CONTRACT, ProjectDispatchRepository, _validate_schema
from lib.project_dispatch.delegation import source_for_task, policy_for_task, check_allowance, issued_worker_policy
from lib.project_dispatch.service import ProjectDispatchService


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
        result['request_allowance'] = request_view(repository, value, budget_id)
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
    issued = issued_worker_policy(repository, task); defaults = CONTRACT['delegationDefaults']
    key = hashlib.sha256(json.dumps([origin['attemptId'], arguments['idempotency_key']],
                                   separators=(',', ':')).encode()).hexdigest()
    command = 'coordinator-delegate:' + key
    original = repository.find_command(command)
    # Stable identities cover both conversation creation and grant admission.
    # Nothing is relaunched just because the tool result acknowledgement was lost.
    request = {'projectPath': repository.project, 'coordinatorId': scope['conversation_id'],
        'recipientId': original['request']['recipientId'] if original else 'pending-worker',
        'authorizationSource': source, 'brief': arguments['brief'], 'model': issued.model,
        'budget': {'maxTokens': arguments.get('max_tokens', defaults['workerTokens']),
                   'maxModelRequests': arguments.get('max_model_requests', defaults['workerModelRequests']),
                   'deadline': policy['deadline']},
        'profile': {'mode': 'project', 'tools': list(issued.tools), 'readPaths': ['.'], 'writePaths': ['.'],
                    'maxToolCalls': arguments.get('max_tool_calls', defaults['workerToolCalls'])},
        'delegation': origin}
    if arguments.get('dependencies'):
        request['dependencies'] = deepcopy(arguments['dependencies'])
    if arguments.get('rework_of'):
        request['reworkOf'] = deepcopy(arguments['rework_of'])
    if policy.get('requestBudgetId') and original is None:
        from lib.project_dispatch.request_budget import allowance
        available = allowance(repository, current, policy['requestBudgetId'])
        request['budget']['maxTokens'] = max(1, min(request['budget']['maxTokens'], available['remainingTokens']))
        request['budget']['maxModelRequests'] = max(1, min(request['budget']['maxModelRequests'], available['remainingModelRequests']))
        request['profile']['maxToolCalls'] = max(1, min(request['profile']['maxToolCalls'], available['remainingToolCalls']))
    elif original is not None and policy.get('requestBudgetId'):
        # Replays retain the exact accepted allocation even after headroom moves.
        request['budget'] = original['request']['budget']
        request['profile']['maxToolCalls'] = original['request']['profile']['maxToolCalls']
    if policy.get('requestBudgetId'):
        request['delegationInputHash'] = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    _validate_schema(request, 'request')
    if original is None:
        check_allowance(repository, current, request)
    from lib.project_dispatch.delegation import validate_source
    validate_source(repository, request)
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
            'dispatch': outcome['dispatch'], 'budget': request['budget'],
            'next_step': 'Delegate independent work, then await_project_workers for results. Review the actual evidence before reporting delivery.'}


def await_workers(task, scope, arguments, *, repository=None, service=None):
    """Wait on existing terminal notifications; no repeated LLM status calls."""
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
            rows.append({'assignment_id': identity, 'conversation_id': assignment['request']['recipientId'],
                         'task_id': execution.get('taskId'), 'turn_id': execution.get('turnId'),
                         'result_revision': execution.get('resultRevision'), 'delivery': view.get('delivery'),
                         'canonical_delivery': view.get('canonicalDelivery', False),
                         'status': 'cancelled' if assignment['grant']['revoked'] else execution.get('status', 'queued'),
                         'result_excerpt': content[:4000] if isinstance(content, str) else '',
                         'result_truncated': isinstance(content, str) and len(content) > 4000})
        return rows
    terminal = {'completed', 'failed', 'interrupted', 'truncated', 'superseded', 'cancelled'}
    def settled(row):
        if arguments.get('wait_for') == 'delivery':
            return row['canonical_delivery'] or row['status'] in terminal - {'completed'}
        return row['status'] in terminal
    deadline = time.monotonic() + arguments.get('timeout_seconds', 1800)
    rows = snapshots()
    while any(not settled(row) for row in rows):
        if task.get('aborted'):
            return {'workers': rows, 'wait': 'cancelled'}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {'workers': rows, 'wait': 'timeout', 'instruction': 'Workers remain active; timeout is not failure. Wait again with these same assignment IDs.'}
        from lib.agent_core.admission import on_terminal
        import threading
        changed = threading.Event()
        unsubscribers = [on_terminal(row['task_id'], lambda _task_id: changed.set()) for row in rows
                         if row['task_id'] and row['status'] not in terminal]
        try:
            rows = snapshots()  # Close subscribe-versus-settlement race.
            if all(settled(row) for row in rows):
                break
            # The wait is interruptible and allocates no thread or model request.
            # A bounded recheck covers process-local notification loss and queued
            # task admission; durable Turn evidence alone declares completion.
            check_at = time.monotonic() + min(remaining, 5.0)
            while not task.get('aborted') and not changed.wait(min(0.25, max(0, check_at - time.monotonic()))):
                if time.monotonic() >= check_at:
                    break
        finally:
            for unsubscribe in unsubscribers:
                unsubscribe()
        rows = snapshots()
    return {'workers': rows, 'wait': 'settled',
            'instruction': 'Read full worker evidence using the conversation/Turn links where needed. Failed/cancelled work is not delivery; verify and continue the original objective.'}
