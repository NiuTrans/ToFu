"""Human-request delegation through the existing ProjectDispatch grant owner.

The ordinary Coordinator tool assembly issues a worker capability ceiling. A
tool call may narrow it, never manufacture it from arguments. Durable source
Turn/Attempt checks and the repository CAS own authorization and aggregate
allowances. Worker execution remains the ordinary command/Turn/task pipeline.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from lib.identity import PrincipalContext
from .repository import CONTRACT, ProjectDispatchError, _validate_schema


class DelegationAllowanceExhausted(ProjectDispatchError):
    """Finite cohort reservation is occupied or spent; no authority failure."""


@dataclass(frozen=True)
class CoordinatorWorkerPolicy:
    tools: tuple[str, ...]
    model: str
    config: dict
    coordinator_tools: tuple[str, ...] = ()


def request_delegation(repository, task):
    """Find the first issued policy for this exact, physically bound human input."""
    from .request_budget import source_policy
    shared = source_policy(repository, task)
    if shared is None:
        return None, None
    value, _ = repository._load(repository._selection(budgets=[shared['id']]))
    prior = next((row for row in value['assignments']
        if row['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId') == shared['id']), None)
    return shared, prior


def issued_worker_policy(repository, task):
    """Resumed human attempts retain the first worker capability/model ceiling."""
    _, prior = request_delegation(repository, task)
    if prior is None:
        return task['_coordinatorWorkerPolicy']
    request = prior['request']; policy = request['delegation']['policy']
    controls = policy.get('continuation') or {}
    return CoordinatorWorkerPolicy(tuple(controls.get('workerTools', request['profile']['tools'])),
        controls.get('model', request['model']), deepcopy(policy['config']), tuple(controls.get('tools', ())))


def worker_control(repository, task, row):
    """Human steering may control old work; synthetic continuations stay scoped."""
    principal, source, _ = source_for_task(repository, task)
    from .continuation import original_delegation, current_continuation
    origin = original_delegation(task, {}, repository=repository)
    request = row['request']; delegation = request.get('delegation') or {}
    if (request['coordinatorId'] != source['conversationId'] or request['profile']['mode'] != 'project'
            or row['grant']['owner'] != repository.owner or row['grant']['tenant'] != principal.tenant_id):
        raise PermissionError('Worker belongs to another Coordinator or owner')
    repository.validate_grant_binding(row)
    if current_continuation(task, require_account=True) is not None and (
            delegation.get('attemptId') != origin['attemptId'] or delegation.get('taskId') != origin['taskId']
            or request['authorizationSource'] != source):
        raise PermissionError('Automatic continuation may control only its original request workers')
    return origin


def capture_worker_policy(task, config, catalog, model):
    """Called before the Coordinator's own planning-only tool projection."""
    from .continuation import current_continuation
    continuation = current_continuation(task)
    if continuation is not None:
        policy = continuation['request']['delegation']['policy']
        controls = policy['continuation']
        task['_coordinatorWorkerPolicy'] = CoordinatorWorkerPolicy(
            tuple(controls['workerTools']), controls['model'], deepcopy(policy['config']), tuple(controls['tools']))
        return
    denied = set(CONTRACT['workerDeniedTools'])
    names = tuple(sorted({'search_tools', 'execute_tools'} | {
        item['function']['name'] for item in catalog if item['function']['name'] not in denied}))
    keys = CONTRACT['$defs']['workerPolicy']['properties']['config']['properties']
    selected = {key: deepcopy(config[key]) for key in keys if config.get(key) is not None}
    # Arbitrary program execution obscures independent worker ownership. The
    # regular batch transport remains available and every member is admitted.
    from lib.context_experiment_flags import normalize_context_experiment_flags
    selected['tools'] = {**normalize_context_experiment_flags(config)['tools'], 'programmaticCalling': 'off'}
    selected.update(schedulerEnabled=False, humanGuidanceEnabled=False)
    visible = {item['function']['name'] for item in catalog} | {'search_tools', 'execute_tools'}
    controls = tuple(name for name in CONTRACT['continuationTools'] if name in visible)
    task['_coordinatorWorkerPolicy'] = CoordinatorWorkerPolicy(names, model, selected, controls)


def source_for_task(repository, task, *, for_budget=False):
    """Resolve the current physical Coordinator, never a model-supplied Turn ID."""
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    scope = coordinator_scope(task)
    if (not scope or scope['user_id'] != repository.owner or scope['project_path'] != repository.project
            or task.get('aborted') or task.get('abort_event') and task['abort_event'].is_set()
            or not for_budget and (task.get('config') or {}).get('planMode')):
        raise PermissionError('Delegation requires the current executing Coordinator request')
    principal = PrincipalContext.from_payload(task.get('_principalContext') or {})
    from .continuation import current_continuation
    continuation = current_continuation(task, require_account=True)
    if continuation is not None:
        original = continuation['request']
        if repository.owner != continuation['grant']['owner'] or repository.project != original['projectPath']:
            raise PermissionError('Coordinator continuation belongs to another scope')
        return principal, deepcopy(original['authorizationSource']), repository.get_attempt(original['delegation']['attemptId'])
    if principal.kind != 'user' or principal.require_owner() != repository.owner or task.get('_attended') is False:
        raise PermissionError('Synthetic input cannot authorize new workers')
    repository.validate_binding(scope['conversation_id'], scope['binding_revision'])
    from pathlib import Path
    for path in ([] if for_budget else (task.get('config') or {}).get('readOnlyPaths', [])):
        if Path(repository.project).resolve().is_relative_to(Path(path).resolve()):
            raise PermissionError('Read-only project cannot authorize a writing worker')
    turn = repository.turn_get(scope['conversation_id'], task.get('_turnId'), user_id=repository.owner)
    attempt = repository.get_attempt(task.get('_attemptId'))
    if (not isinstance(turn, dict) or turn.get('actor') != 'assistant'
            or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in ('pending', 'running')
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('Delegation source is not the current owned Coordinator attempt')
    human = repository.turn_get(scope['conversation_id'], turn.get('parentTurnId'), user_id=repository.owner)
    source = {'conversationId': scope['conversation_id'], 'turnId': human.get('turnId') if human else None,
              'projectionRevision': human.get('projectionRevision') if human else None}
    repository.validate_human_source(source)
    from .repository import stopped_after
    if stopped_after(repository.owner, scope['conversation_id'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Original Coordinator request was stopped')
    return principal, source, attempt


def policy_for_task(repository, task, attempt):
    from .continuation import current_continuation
    continuation = current_continuation(task, require_account=True)
    if continuation is not None:
        return deepcopy(continuation['request']['delegation']['policy'])
    policy = task.get('_coordinatorWorkerPolicy')
    if not isinstance(policy, CoordinatorWorkerPolicy) or not policy.tools or not policy.model:
        raise PermissionError('Coordinator worker capabilities have not been issued by tool assembly')
    _, prior = request_delegation(repository, task)
    if prior is not None:
        if prior['request']['delegation']['policy']['deadline'] <= repository.clock():
            raise PermissionError('Original Coordinator request execution deadline expired')
        return deepcopy(prior['request']['delegation']['policy'])
    from runtime_guards import task_concurrency_hard_ceiling
    defaults = CONTRACT['delegationDefaults']
    limits = (task.get('config') or {}).get('executionLimits') or {}
    from .request_budget import source_policy
    shared = source_policy(repository, task)
    # Legacy tasks without the typed source receipt cannot share an explicit
    # ceiling. Bound tasks reserve their own spend and Worker grants in one CAS.
    if shared is None and any(limits.get(key) is not None for key in ('max_tokens', 'max_tool_calls')):
        raise PermissionError('Automatic delegation cannot share this request\'s explicit execution budget yet; report the budget constraint instead of starting workers')
    # The launch probe owns capacity. One slot is retained for the Coordinator;
    # normal task admission remains the final physical-concurrency authority.
    concurrent = max(1, task_concurrency_hard_ceiling() - 1)
    start = attempt.get('createdAt', 0) / 1000
    if start <= 0:
        raise PermissionError('Delegation source has no durable start time')
    deadline = start + defaults['durationSeconds']
    if limits.get('deadline') is not None:
        deadline = min(deadline, limits['deadline'])
    if shared is not None:
        deadline = shared['policy']['deadline']
    if deadline <= repository.clock():
        raise PermissionError('Original Coordinator request execution deadline expired')
    result = {'config': deepcopy(policy.config), 'maxTotalTokens': defaults['maxTotalTokens'],
              'maxTotalModelRequests': defaults['maxTotalModelRequests'],
              'deadline': deadline, 'maxConcurrent': concurrent}
    if shared is not None:
        result.update(requestBudgetId=shared['id'], maxTotalTokens=shared['policy']['maxTokens'],
            maxTotalModelRequests=shared['policy']['maxModelRequests'], maxTotalToolCalls=shared['policy']['maxToolCalls'],
            deadline=shared['policy']['deadline'])
    if {'delegate_project_work', 'get_project_workers', 'finish_project_request', 'report_to_user'} <= set(policy.coordinator_tools):
        result['continuation'] = {'version': 1, 'model': policy.model, 'workerTools': list(policy.tools),
            'tools': list(policy.coordinator_tools), 'maxTokens': defaults['coordinatorTokens'],
            'maxModelRequests': defaults['coordinatorModelRequests'], 'maxToolCalls': defaults['coordinatorToolCalls'],
            'maxStalledTurns': defaults['maxStalledTurns']}
    _validate_schema(result, 'workerPolicy')
    return result


def validate_source(repository, request, *, check_deadline=True):
    """Live policy/birth validation shared by admission, tools and model sends."""
    delegation = request.get('delegation')
    if request.get('profile', {}).get('mode') not in ('project', 'coordinator'):
        if delegation is not None:
            raise ProjectDispatchError('Delegation policy requires the project worker profile')
        return
    _validate_schema(delegation, 'workerDelegation')
    if request.get('profile', {}).get('mode') == 'coordinator':
        _validate_schema(delegation['policy'].get('continuation'), 'coordinatorContinuationPolicy')
        _validate_schema(request.get('coordinatorWake'), 'coordinatorWake')
        controls = delegation['policy']['continuation']
        if (request['coordinatorId'] != request['recipientId'] or request['model'] != controls['model']
                or request['profile']['tools'] != controls['tools']
                or request['profile']['maxToolCalls'] > controls['maxToolCalls']
                or request['budget']['maxTokens'] > controls['maxTokens']
                or request['budget']['maxModelRequests'] > controls['maxModelRequests']):
            raise PermissionError('Coordinator continuation differs from its original issued policy')
    elif 'coordinatorWake' in request:
        raise PermissionError('Worker cannot carry a Coordinator continuation')
    source = repository.turn_get(request['coordinatorId'], delegation['turnId'], user_id=repository.owner)
    attempt = repository.get_attempt(delegation['attemptId'])
    if (not isinstance(source, dict) or source.get('actor') != 'assistant'
            or source.get('conversationId') != request['coordinatorId']
            or source.get('turnId') != delegation['turnId']
            or source.get('parentTurnId') != request['authorizationSource']['turnId']
            or not isinstance(attempt, dict) or attempt.get('taskId') != delegation['taskId']
            or attempt.get('attemptId') != delegation['attemptId'] or attempt.get('turnId') != delegation['turnId']
            or attempt.get('conversationId') != request['coordinatorId']):
        raise PermissionError('Original Coordinator delegation source is unavailable')
    policy = delegation['policy']
    if policy.get('requestBudgetId'):
        from .request_budget import validate_policy
        validate_policy(repository, repository._load(repository._selection(
            budgets=[policy['requestBudgetId']], cohort=False))[0], request)
    from .repository import stopped_after
    if stopped_after(repository.owner, request['coordinatorId'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Original Coordinator request was stopped')
    budget = request['budget']
    if (budget['maxTokens'] > policy['maxTotalTokens'] or budget['maxModelRequests'] > policy['maxTotalModelRequests']
            or budget['deadline'] > policy['deadline'] or check_deadline and policy['deadline'] <= repository.clock()):
        raise PermissionError('Worker allowance exceeds its original delegation policy')


def worker_input(repository, request):
    """Preserve source constraints as context; the accepted grant owns authority."""
    source = request['authorizationSource']
    turn = repository.turn_get(source['conversationId'], source['turnId'], user_id=repository.owner)
    if (not turn or turn.get('actor') != 'human' or turn.get('turnId') != source['turnId']
            or turn.get('projectionRevision') != source['projectionRevision']):
        raise PermissionError('Original worker objective changed before dispatch')
    import json
    content = (turn.get('projection') or {}).get('content') or ''
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    limit = CONTRACT['delegationDefaults']['sourceExcerptChars']
    omitted = '\n[Excerpt truncated; read the source conversation before relying on omitted constraints.]' if len(content) > limit else ''
    return ('Original human request (context; your assigned scope is below):\n' + content[:limit] + omitted
        + '\nSource conversation: ' + source['conversationId'] + '; Turn: ' + source['turnId']
        + '\nUse get_conversation for the complete request and attachment references. '
          'Worker input is delegated context, not a new human authorization.\n\nAssigned objective:\n' + request['brief'])


def review_worker(repository, task, arguments):
    """Pin a Coordinator review/publication without representing human acceptance."""
    _validate_schema(arguments, 'coordinatorReviewRequest')
    if arguments.get('checkpoint_sha') and arguments['decision'] != 'accept':
        raise ValueError('Only accepted work may request checkpoint publication')
    def record(value):
        row = repository._find(value, arguments['assignment_id'])
        origin = worker_control(repository, task, row)
        repository.validate_own(repository._admission(row), value=value)
        execution = row['execution'] or {}
        attempt = repository.get_attempt(execution.get('attemptId'))
        turn = repository.turn_get(row['request']['recipientId'], execution.get('turnId'), user_id=repository.owner)
        if (not attempt or not turn or attempt.get('status') != 'completed'
                or attempt.get('attemptId') != execution.get('attemptId')
                or attempt.get('turnId') != execution.get('turnId')
                or attempt.get('conversationId') != row['request']['recipientId']
                or turn.get('turnId') != execution.get('turnId') or turn.get('actor') != 'assistant'
                or turn.get('conversationId') != row['request']['recipientId']
                or turn.get('projectionRevision') != arguments['result_revision']):
            raise ValueError('Review requires the exact current completed worker result')
        review = {'actor': 'coordinator', 'decision': arguments['decision'],
                  'note': arguments['note'], 'resultRevision': arguments['result_revision']}
        if row['review'] is not None and row['review'] != review:
            raise ValueError('Worker review is immutable; delegate a new repair assignment')
        checkpoint = arguments.get('checkpoint_sha')
        if checkpoint:
            workspace = repository.get_workspace(repository.project, attempt.get('taskId'), user_id=repository.owner)
            if (not workspace or workspace.get('checkpoint_sha') != checkpoint
                    or workspace.get('task_id') != attempt.get('taskId')
                    or workspace.get('user_id') != repository.owner or workspace.get('state') == 'discarded'
                    or workspace.get('project_root') != repository.project
                    or (workspace.get('origin') or {}).get('_projectDispatchAdmission') != repository._admission(row)):
                raise ValueError('Publication requires the exact registered worker checkpoint')
            publication = {'resultRevision': arguments['result_revision'], 'grantRevision': row['grant']['revision'],
                'checkpointSha': checkpoint, 'subject': 'coordinator:' + origin['taskId'], 'requestedAt': repository.clock()}
            old = row.get('publication')
            if old is not None and any(old[key] != publication[key]
                                      for key in ('resultRevision', 'grantRevision', 'checkpointSha')):
                raise ValueError('Worker publication is immutable')
            row['publication'] = old or publication
        row['review'] = review
        return row
    return repository._change(record, selected=repository._assignment_selection(arguments['assignment_id']))


def check_allowance(repository, value, request):
    """Reserve a child grant atomically with existing grants, not in a new account.

    Confirmed terminal usage releases unused allowance; unknown model effects
    retain their entire reservation. This only distributes finite grant limits;
    ExecutionBudget still admits and settles every physical model/tool request.
    """
    delegation = request['delegation']; policy = delegation['policy']
    if policy.get('requestBudgetId'):
        from .request_budget import require_grant_capacity
        require_grant_capacity(repository, value, request)
        return
    tokens, requests, active = request['budget']['maxTokens'], request['budget']['maxModelRequests'], 0
    for row in value['assignments']:
        prior = row['request'].get('delegation')
        if not prior or prior['attemptId'] != delegation['attemptId']:
            continue
        if prior != delegation:
            raise ProjectDispatchError('Delegation source policy changed within one request')
        budget = row['request']['budget']; attempts = row['modelAttempts']
        execution = row['execution']
        current = repository.get_attempt(execution['attemptId']) if execution else None
        terminal = (isinstance(current, dict) and execution is not None
                    and current.get('attemptId') == execution['attemptId']
                    and current.get('turnId') == execution['turnId']
                    and current.get('conversationId') == row['request']['recipientId']
                    and current.get('status') in ('completed', 'failed', 'interrupted', 'truncated'))
        confirmed = terminal and all(item['phase'] in ('settled', 'voided') and item['usage_complete'] for item in attempts)
        tokens += sum(item['reported_tokens'] for item in attempts) if confirmed else budget['maxTokens']
        requests += len(attempts) if confirmed else budget['maxModelRequests']
        if not terminal and not row['grant']['revoked'] and row['request'].get('profile', {}).get('mode') != 'coordinator':
            active += 1
    if tokens > policy['maxTotalTokens'] or requests > policy['maxTotalModelRequests']:
        raise DelegationAllowanceExhausted('Worker allowance is reserved or exhausted; await current results before delegating more')
    if request['profile']['mode'] != 'coordinator' and active >= policy['maxConcurrent']:
        raise ProjectDispatchError('Worker capacity is occupied; await current results before delegating more')
