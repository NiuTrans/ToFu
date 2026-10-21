"""Original-request allowances over existing ExecutionBudget occurrence receipts.

The dispatch CAS owns source/Worker reservation conservation. Human source
accounts and independent Worker accounts remain ordinary physical accounts;
no wallet, model executor, polling loop or alternative task state is introduced.
Entry points: bind_source_account, allowance, validate_policy, require_capacity.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import uuid

from lib.llm.llm_errors import AbortedError
from .repository import CONTRACT, ProjectDispatchError
from .accounting import model_consumption, record_model_event


@dataclass(frozen=True)
class SourceBudgetBinding:
    budget_id: str
    nonce: str
    account_id: int
    task_id: str


def budget_row(value, identity):
    row = next((row for row in value.get('requestBudgets', []) if row['id'] == identity), None)
    if row is None:
        raise PermissionError('Original request budget is unavailable')
    return row


def validate_policy(repository, value, request):
    delegation = request.get('delegation') or {}
    policy = delegation.get('policy') or {}
    identity = policy.get('requestBudgetId')
    if not identity:
        return None  # Legacy grants retain their original narrower accounting.
    row = budget_row(value, identity)
    if (request['authorizationSource'] != row['authorizationSource']
            or request['coordinatorId'] != row['coordinatorId']
            or policy['maxTotalTokens'] != row['policy']['maxTokens']
            or policy['maxTotalModelRequests'] != row['policy']['maxModelRequests']
            or policy.get('maxTotalToolCalls') != row['policy']['maxToolCalls']
            or policy['deadline'] != row['policy']['deadline']
            or not any(account['attemptId'] == delegation['attemptId'] and account['taskId'] == delegation['taskId']
                       and account['turnId'] == delegation['turnId'] for account in row['accounts'])):
        raise PermissionError('Worker policy differs from its original request budget')
    repository.validate_binding(row['coordinatorId'], row['bindingRevision'])
    return row


def allowance(repository, value, identity):
    """Observe charged/held allowances; terminal evidence alone releases unused grants."""
    source = budget_row(value, identity)
    observed, held, complete = model_consumption(source['modelAttempts'])
    requests = held_requests = len(source['modelAttempts'])
    tools = held_tools = sum(account['toolCalls'] for account in source['accounts'])
    active = 0
    for row in value['assignments']:
        if row['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId') != identity:
            continue
        amount, reserved, known = model_consumption(row['modelAttempts'])
        observed += amount
        requests += len(row['modelAttempts'])
        counter = row.get('toolReservations')
        calls = counter['count'] if counter is not None else 0
        tools += calls
        execution = row['execution']
        attempt = repository.get_attempt(execution['attemptId']) if execution else None
        terminal = (attempt and attempt.get('attemptId') == execution['attemptId']
            and attempt.get('turnId') == execution['turnId'] and attempt.get('conversationId') == row['request']['recipientId']
            and attempt.get('status') in ('completed', 'failed', 'interrupted', 'truncated', 'superseded'))
        confirmed = terminal and known
        held += reserved if confirmed else max(reserved, row['request']['budget']['maxTokens'])
        held_requests += len(row['modelAttempts']) if confirmed else row['request']['budget']['maxModelRequests']
        # A missing tool counter never proves that reserved but unentered slots
        # were free; it is retained until exact accounting is available.
        held_tools += calls if terminal and counter is not None else max(calls, row['request']['profile']['maxToolCalls'])
        complete = complete and known and (not terminal or counter is not None)
        if not terminal and not row['grant']['revoked'] and row['request']['profile']['mode'] != 'coordinator':
            active += 1
    policy = source['policy']
    return {'observedTokens': observed, 'heldTokens': held, 'remainingTokens': max(0, policy['maxTokens'] - held),
            'modelRequests': requests, 'heldModelRequests': held_requests,
            'remainingModelRequests': max(0, policy['maxModelRequests'] - held_requests),
            'toolCalls': tools, 'heldToolCalls': held_tools,
            'remainingToolCalls': max(0, policy['maxToolCalls'] - held_tools),
            'usageComplete': complete, 'activeWorkers': active}


def require_capacity(repository, value, identity, *, extra_tokens=0, extra_requests=0, extra_tools=0):
    from .delegation import DelegationAllowanceExhausted
    row = budget_row(value, identity)
    state = allowance(repository, value, identity)
    for field, used, extra in (('maxTokens', 'heldTokens', extra_tokens),
                              ('maxModelRequests', 'heldModelRequests', extra_requests),
                              ('maxToolCalls', 'heldToolCalls', extra_tools)):
        if state[used] + extra > row['policy'][field]:
            raise DelegationAllowanceExhausted('Original request allowance is reserved or exhausted: ' + field)
    return state


def request_view(repository, value, identity):
    """One passive projection for the human interface and Coordinator planning."""
    source = budget_row(value, identity)
    consumption = allowance(repository, value, identity)
    consumption.pop('activeWorkers')
    return {'id': source['id'], 'sourceConversationId': source['coordinatorId'],
            'sourceTurnId': source['authorizationSource']['turnId'], 'createdAt': source['createdAt'],
            'policy': deepcopy(source['policy']), 'consumption': consumption}


def require_grant_capacity(repository, value, request):
    row = validate_policy(repository, value, request)
    for prior in value['assignments']:
        delegation = prior['request'].get('delegation')
        if delegation and delegation['attemptId'] == request['delegation']['attemptId'] and delegation != request['delegation']:
            raise ProjectDispatchError('Delegation source policy changed within one request')
    state = require_capacity(repository, value, row['id'], extra_tokens=request['budget']['maxTokens'],
        extra_requests=request['budget']['maxModelRequests'], extra_tools=request['profile']['maxToolCalls'])
    if request['profile']['mode'] != 'coordinator' and state['activeWorkers'] >= request['delegation']['policy']['maxConcurrent']:
        raise ProjectDispatchError('Worker capacity is occupied; await current results before delegating more')


def source_policy(repository, task):
    binding = task.get('_coordinatorRequestBudget')
    if binding is None:
        return None
    if (not isinstance(binding, SourceBudgetBinding) or binding.task_id != task.get('id')
            or binding.account_id != id(task.get('_executionBudget'))):
        raise PermissionError('Original request budget lost its physical account')
    row = budget_row(repository._load(repository._selection(budgets=[binding.budget_id], cohort=False))[0], binding.budget_id)
    if not any(account['nonce'] == binding.nonce and account['taskId'] == task['id']
               and account['attemptId'] == task['_attemptId'] for account in row['accounts']):
        raise PermissionError('Original request budget account is not registered')
    return row


def bind_source_account(task, *, repository=None, settings=None):
    """App composition, after accepted Turn/task binding and before provider work."""
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from .admission import is_dispatch_task
    config = task.get('config') or {}
    if is_dispatch_task(task) or not (scope := coordinator_scope(task)):
        return None
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    if principal.kind != 'user' or task.get('_attended') is False:
        return None
    if repository is None:
        from .repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    from .delegation import source_for_task
    principal, source, attempt = source_for_task(repository, task, for_budget=True)
    if task.get('_coordinatorRequestBudget') is not None:
        return source_policy(repository, task)
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)
    if budget.consumption() != (0, 0):
        raise PermissionError('Request budget must be attached before consumption')
    if settings is None:
        from lib.conversations.coordinator_budget import CoordinatorBudgetSettings
        settings = CoordinatorBudgetSettings(repository.owner, repository.project, client=repository.client).read()
    limits = config.get('executionLimits') or {}
    selected = settings['settings']
    deadline = attempt['createdAt'] / 1000 + selected['durationSeconds']
    policy = {'maxTokens': min(selected['maxTokens'], limits.get('max_tokens') if limits.get('max_tokens') is not None else selected['maxTokens']),
              'maxModelRequests': selected['maxModelRequests'],
              'maxToolCalls': min(selected['maxToolCalls'], limits.get('max_tool_calls') if limits.get('max_tool_calls') is not None else selected['maxToolCalls']),
              'deadline': min(deadline, limits.get('deadline') if limits.get('deadline') is not None else deadline)}
    identity = hashlib.sha256(json.dumps([scope['binding_revision'], source], sort_keys=True).encode()).hexdigest()
    nonce = uuid.uuid4().hex
    account = {'turnId': task['_turnId'], 'attemptId': task['_attemptId'], 'taskId': task['id'], 'nonce': nonce, 'toolCalls': 0}
    proposed = {'id': identity, 'coordinatorId': task['convId'], 'bindingRevision': scope['binding_revision'],
        'authorizationSource': source, 'tenant': principal.tenant_id, 'subject': principal.subject_id,
        'settingsRevision': settings['revision'], 'createdAt': repository.clock(), 'policy': policy,
        'accounts': [account], 'modelAttempts': []}
    def register(value):
        source_for_task(repository, task, for_budget=True)
        rows = value.setdefault('requestBudgets', [])
        old = next((row for row in rows if row['id'] == identity), None)
        if old is None:
            rows.append(deepcopy(proposed)); return proposed
        if any(old[key] != proposed[key] for key in ('coordinatorId', 'bindingRevision', 'authorizationSource', 'tenant', 'subject')):
            raise PermissionError('Original request budget identity changed')
        prior = next((row for row in old['accounts'] if row['taskId'] == task['id']), None)
        if prior is not None and prior != account:
            raise PermissionError('A physical source account cannot be replenished')
        if prior is None:
            old['accounts'].append(deepcopy(account))
        # Continuing the same accepted human input retains its first policy;
        # changing project defaults does not replenish that request.
        return old
    accepted = repository._change(register, selected=repository._selection(budgets=[identity], cohort=False))
    terms = accepted['policy']
    budget.narrow_limits({'max_tokens': terms['maxTokens'], 'max_tool_calls': terms['maxToolCalls'], 'deadline': terms['deadline']})
    marker = SourceBudgetBinding(identity, nonce, id(budget), task['id'])
    task['_coordinatorRequestBudget'] = marker
    live_models = set()

    def validate():
        current_principal, current_source, _ = source_for_task(repository, task, for_budget=True)
        if (current_source != accepted['authorizationSource'] or current_principal.tenant_id != accepted['tenant']
                or current_principal.subject_id != accepted['subject']):
            raise PermissionError('Original request budget source or principal changed')
        source_policy(repository, task)
        if repository.clock() >= terms['deadline']:
            raise AbortedError('Original request execution deadline expired')

    def observe(event):
        event = {**event, 'account_nonce': nonce}
        if event['phase'] == 'dispatch_intent':
            validate()
        def record(value):
            row = budget_row(value, identity)
            record_model_event(row['modelAttempts'], event, max_tokens=terms['maxTokens'],
                max_requests=terms['maxModelRequests'], live_intents=live_models, prior_accounts=True)
            if event['phase'] == 'dispatch_intent':
                require_capacity(repository, value, identity)
        try:
            repository._change(record, selected=repository._selection(budgets=[identity]))
        finally:
            if event['phase'] != 'dispatch_intent': live_models.discard(event['attempt_ordinal'])
        if event['phase'] == 'dispatch_intent':
            validate(); live_models.add(event['attempt_ordinal'])

    def reserve_tool(event):
        from lib.agent_core.execution_budget import ModelBudgetExceeded
        from .delegation import DelegationAllowanceExhausted
        validate()
        def record(value):
            row = budget_row(value, identity)
            current = next(item for item in row['accounts'] if item['taskId'] == task['id'] and item['nonce'] == nonce)
            if event['root_task_id'] != task['id'] or event['reservation_ordinal'] not in (current['toolCalls'], current['toolCalls'] + 1):
                raise PermissionError('Source tool reservation changed its occurrence')
            current['toolCalls'] = event['reservation_ordinal']
            require_capacity(repository, value, identity)
        try:
            repository._change(record, selected=repository._selection(budgets=[identity]))
        except DelegationAllowanceExhausted as error:
            raise ModelBudgetExceeded('execution_tool_budget_exhausted') from error

    def remaining():
        validate()
        return allowance(repository, repository._load(repository._selection(budgets=[identity]))[0], identity)['remainingTokens']
    budget.bind_model_attempt_observer(observe, tool_reservation_observer=reserve_tool, model_allowance_provider=remaining)
    return accepted
