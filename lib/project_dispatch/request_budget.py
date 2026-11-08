"""Original-request usage accounts over existing ExecutionBudget occurrence receipts.

Coordinator requests have no spending ceiling. This module registers the human
source account, records its physical model/tool occurrences in the dispatch
CAS and projects combined source + Worker usage for the human and Coordinator.
It never refuses a model or tool call for spend. Authority is still revalidated
on every request: Stop, rebinding, a changed human source or a replaced
physical account fails closed. A refused model dispatch raises
``ModelAdmissionRefused``, which no credential, slot or fallback model can
repair; a Stop stays a plain abort.
Entry points: bind_source_account, usage, source_policy, request_view.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import uuid

from .accounting import fold_settled_models, folded_model_event, holder_consumption, record_model_event


@dataclass(frozen=True)
class SourceBudgetBinding:
    budget_id: str
    nonce: str
    account_id: int
    task_id: str


def budget_row(value, identity):
    row = next((row for row in value.get('requestBudgets', []) if row['id'] == identity), None)
    if row is None:
        raise PermissionError('Original request account is unavailable')
    return row


def validate_policy(repository, value, request):
    """Worker grants must belong to the request account they claim."""
    delegation = request.get('delegation') or {}
    policy = delegation.get('policy') or {}
    identity = policy.get('requestBudgetId')
    if not identity:
        return None
    row = budget_row(value, identity)
    if (request['authorizationSource'] != row['authorizationSource']
            or request['coordinatorId'] != row['coordinatorId']
            or not any(account['attemptId'] == delegation['attemptId'] and account['taskId'] == delegation['taskId']
                       and account['turnId'] == delegation['turnId'] for account in row['accounts'])):
        raise PermissionError('Worker policy differs from its original request account')
    repository.validate_binding(row['coordinatorId'], row['bindingRevision'])
    return row


def usage(repository, value, identity):
    """Observed source + Worker usage. A missing receipt makes the total a lower bound."""
    source = budget_row(value, identity)
    tokens, requests, complete = holder_consumption(source)
    tools = sum(account['toolCalls'] for account in source['accounts'])
    active = 0
    for row in value['assignments']:
        if row['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId') != identity:
            continue
        amount, count, known = holder_consumption(row)
        tokens += amount
        requests += count
        counter = row.get('toolReservations')
        folded_tools = (row.get('settledToolUsage') or {}).get('calls', 0)
        tools += counter['count'] if counter is not None else len(row.get('toolAttempts', [])) + folded_tools
        complete = complete and known
        execution = row['execution']
        attempt = repository.get_attempt(execution['attemptId']) if execution else None
        terminal = bool(attempt and attempt.get('attemptId') == execution['attemptId']
            and attempt.get('status') in ('completed', 'failed', 'interrupted', 'truncated', 'superseded'))
        if not terminal and not row['grant']['revoked'] and row['request']['profile']['mode'] != 'coordinator':
            active += 1
    return {'tokens': tokens, 'modelRequests': requests, 'toolCalls': tools,
            'usageComplete': complete, 'activeWorkers': active}


def request_view(repository, value, identity):
    """One passive projection for the human interface and Coordinator planning."""
    source = budget_row(value, identity)
    view = {'id': source['id'], 'sourceConversationId': source['coordinatorId'],
            'sourceTurnId': source['authorizationSource']['turnId'], 'createdAt': source['createdAt'],
            'usage': usage(repository, value, identity)}
    if source.get('pendingDecision'):
        view['pendingDecision'] = deepcopy(source['pendingDecision'])
    return view


def source_policy(repository, task):
    binding = task.get('_coordinatorRequestBudget')
    if binding is None:
        return None
    if (not isinstance(binding, SourceBudgetBinding) or binding.task_id != task.get('id')
            or binding.account_id != id(task.get('_executionBudget'))):
        raise PermissionError('Original request account lost its physical account')
    row = budget_row(repository._load(repository._selection(budgets=[binding.budget_id], cohort=False))[0], binding.budget_id)
    if not any(account['nonce'] == binding.nonce and account['taskId'] == task['id']
               and account['attemptId'] == task['_attemptId'] for account in row['accounts']):
        raise PermissionError('Original request account is not registered')
    return row


def _stopped(task):
    abort_event = task.get('abort_event')
    return bool(task.get('aborted') or (abort_event is not None and abort_event.is_set()))


def bind_source_account(task, *, repository=None, expected_input=None):
    """App composition, after accepted Turn/task binding and before provider work.

    Call it once the accepted input is final: the account pins the human
    source Turn's projection revision. ``expected_input`` is the
    ``(turnId, projectionRevision)`` a deferred translation verdict settled
    for the model view; a source that changed since then refuses.
    """
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from .admission import is_dispatch_task, reread_on_contention
    from .repository import ProjectDispatchError
    if is_dispatch_task(task) or not (scope := coordinator_scope(task)):
        return None
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    if principal.kind != 'user' or task.get('_attended') is False:
        return None
    if _stopped(task):
        # A stopped request unwinds before provider work and opens no account.
        return None
    if repository is None:
        from .repository import ProjectDispatchRepository
        repository = ProjectDispatchRepository(scope['user_id'], scope['project_path'])
    from .delegation import source_for_task
    from .repository import SyntheticSource
    try:
        principal, source, attempt = reread_on_contention(
            task, lambda: source_for_task(repository, task, for_budget=True),
            label='coordinator request source', effect_free=True)
    except SyntheticSource:
        # Report relays, channel events and other nonhuman input authorize no
        # Workers, so their request opens no account; delegation still refuses.
        return None
    if (expected_input is not None and expected_input[1] is not None
            and (source['turnId'], source['projectionRevision']) != tuple(expected_input)):
        raise PermissionError('Original request source changed before its account was bound')
    if task.get('_coordinatorRequestBudget') is not None:
        return source_policy(repository, task)
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)
    if budget.consumption() != (0, 0):
        raise PermissionError('Request account must be attached before consumption')
    identity = hashlib.sha256(json.dumps([scope['binding_revision'], source], sort_keys=True).encode()).hexdigest()
    nonce = uuid.uuid4().hex
    account = {'turnId': task['_turnId'], 'attemptId': task['_attemptId'], 'taskId': task['id'], 'nonce': nonce, 'toolCalls': 0}
    proposed = {'id': identity, 'coordinatorId': task['convId'], 'bindingRevision': scope['binding_revision'],
        'authorizationSource': source, 'tenant': principal.tenant_id, 'subject': principal.subject_id,
        'createdAt': repository.clock(), 'accounts': [account], 'modelAttempts': []}
    def register(value):
        source_for_task(repository, task, for_budget=True)
        rows = value.setdefault('requestBudgets', [])
        old = next((row for row in rows if row['id'] == identity), None)
        if old is None:
            rows.append(deepcopy(proposed)); return proposed
        if any(old[key] != proposed[key] for key in ('coordinatorId', 'bindingRevision', 'authorizationSource', 'tenant', 'subject')):
            raise PermissionError('Original request account identity changed')
        prior = next((row for row in old['accounts'] if row['taskId'] == task['id']), None)
        if prior is not None and prior != account:
            raise PermissionError('A physical source account cannot be replenished')
        if prior is None:
            old['accounts'].append(deepcopy(account))
        return old
    accepted = reread_on_contention(
        task, lambda: repository._change(register, selected=repository._selection(budgets=[identity], cohort=False)),
        label='coordinator request account')
    marker = SourceBudgetBinding(identity, nonce, id(budget), task['id'])
    task['_coordinatorRequestBudget'] = marker
    live_models = set()

    def validate():
        current_principal, current_source, _ = source_for_task(repository, task, for_budget=True)
        if (current_source != accepted['authorizationSource'] or current_principal.tenant_id != accepted['tenant']
                or current_principal.subject_id != accepted['subject']):
            raise PermissionError('Original request source or principal changed')
        source_policy(repository, task)

    def admit_dispatch():
        try:
            reread_on_contention(task, validate, label='coordinator request authority', effect_free=True)
        except (PermissionError, ProjectDispatchError) as error:
            from lib.llm.llm_errors import AbortedError, ModelAdmissionRefused
            if _stopped(task):
                raise AbortedError('Coordinator request stopped before dispatch') from error
            raise ModelAdmissionRefused(f'Coordinator request authority ended: {error}') from error

    def observe(event):
        event = {**event, 'account_nonce': nonce}
        if event['phase'] == 'dispatch_intent':
            admit_dispatch()
        def record(value):
            row = budget_row(value, identity)
            if folded_model_event(row, event):
                return
            record_model_event(row['modelAttempts'], event, live_intents=live_models, prior_accounts=True)
            fold_settled_models(row)
        try:
            reread_on_contention(
                task, lambda: repository._change(record, selected=repository._selection(budgets=[identity], cohort=False)),
                label='coordinator request model occurrence', cancellable=event['phase'] == 'dispatch_intent')
        finally:
            if event['phase'] != 'dispatch_intent': live_models.discard(event['attempt_ordinal'])
        if event['phase'] == 'dispatch_intent':
            admit_dispatch(); live_models.add(event['attempt_ordinal'])

    def reserve_tool(event):
        reread_on_contention(task, validate, label='coordinator request authority', effect_free=True)
        def record(value):
            row = budget_row(value, identity)
            current = next(item for item in row['accounts'] if item['taskId'] == task['id'] and item['nonce'] == nonce)
            if event['root_task_id'] != task['id'] or event['reservation_ordinal'] not in (current['toolCalls'], current['toolCalls'] + 1):
                raise PermissionError('Source tool occurrence changed its ordinal')
            current['toolCalls'] = event['reservation_ordinal']
        reread_on_contention(
            task, lambda: repository._change(record, selected=repository._selection(budgets=[identity], cohort=False)),
            label='coordinator request tool reservation')

    budget.bind_model_attempt_observer(observe, tool_reservation_observer=reserve_tool)
    return accepted
