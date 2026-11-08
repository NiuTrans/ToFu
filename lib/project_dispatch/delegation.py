"""Human-request delegation through the existing ProjectDispatch grant owner.

The ordinary Coordinator tool assembly issues a worker capability ceiling. A
tool call may narrow it, never manufacture it from arguments. Durable source
Turn/Attempt checks and the repository CAS own authorization and frozen policy.
Requests have no spend or concurrency ceiling. Worker execution remains the
ordinary command/Turn/task pipeline. Workers run on the Coordinator
conversation's ``workerModel`` setting, else the owner's default model, never
implicitly on the Coordinator's own model (docs/ROUTER.md R5): resolve_worker_model
and admit_worker_model are that resolver and its route admission.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from lib.identity import PrincipalContext
from .repository import CONTRACT, ProjectDispatchError, _validate_schema


@dataclass(frozen=True)
class CoordinatorWorkerPolicy:
    """Issued Worker ceiling. ``model`` is the Workers' model, None until the first
    delegation of a request resolves it (resolve_worker_model reads storage, so
    per-round tool assembly never does); continuations of the Coordinator
    itself keep ``coordinator_model``."""
    tools: tuple[str, ...]
    model: str | None
    config: dict
    coordinator_tools: tuple[str, ...]
    coordinator_model: str


class CoordinatorModelMissing(PermissionError):
    """This Coordinator request runs without a model, so it can issue no Worker.

    Continuations record the Coordinator's own model; a request that started
    without one is refused identically on every delegation, so the refusal
    carries its own effect and recovery instead of the generic retry advice.
    """
    execution_started = False
    recovery = ('No worker was created. Delegating again in this request fails the same way; '
                'report this blocker and its remedy to the human instead of retrying.')


def owner_default_model(owner: int) -> str:
    """The model an owner's new conversations default to."""
    from lib.identity import require_user_id
    require_user_id(owner, context='owner default model')
    # TODO(enterprise): the default model is server configuration
    # (model_defaults.default_model); a per-owner default belongs in that
    # owner's model-routing settings.
    import lib
    return str(getattr(lib, 'LLM_MODEL', '') or '')


def admit_worker_model(owner: int, model: str, *, tenant_id: str | None = None) -> str:
    """Admit an owner's Worker model choice through the ordinary route resolver."""
    if not isinstance(model, str) or not model.strip() or len(model) > 256:
        raise ValueError('workerModel must contain 1–256 characters')
    from lib.conv_config import canonicalise_model_id
    from lib.model_routing import ModelRoutingError, ModelRoutingRepository, OwnerBoundary, \
        resolve_compatible_model
    model = canonicalise_model_id(model.strip())
    document = ModelRoutingRepository().get(OwnerBoundary.create(owner, tenant_id)).document
    try:
        resolve_compatible_model(document, model)
    except ModelRoutingError as error:
        raise ValueError(f'workerModel {model!r} is not routable for this owner: {error}') from error
    return model


def resolve_worker_model(task) -> str:
    """The Workers' model for this Coordinator request, pinned on the task.

    The bound Coordinator conversation's ``workerModel`` wins; otherwise the
    owner's default model. Models never choose it: only the owner's setting
    (written through ProjectCoordinationRepository.set_worker_model) does.
    """
    pinned = task.get('_coordinatorWorkerModel')
    if isinstance(pinned, str) and pinned:
        return pinned
    from lib.tasks_pkg.coordinator_policy import coordinator_scope
    from lib.tasks_pkg.manager import task_user_id
    scope = coordinator_scope(task)
    owner = scope['user_id'] if scope else task_user_id(task)
    model = ''
    if scope:
        from lib.conversations.project_coordination import ProjectCoordinationRepository
        model = ProjectCoordinationRepository(owner).worker_model(scope['project_path'])
    model = model or owner_default_model(owner)
    if not model:
        raise PermissionError('No Worker model: configure a default model or this Coordinator’s '
                              'Worker model, then delegate again')
    task['_coordinatorWorkerModel'] = model
    return model


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
        issued = task['_coordinatorWorkerPolicy']
        return issued if issued.model else replace(issued, model=resolve_worker_model(task))
    request = prior['request']; policy = request['delegation']['policy']
    controls = policy.get('continuation') or {}
    # The first Worker's request records the Workers' model; continuation
    # controls record the Coordinator's own model.
    return CoordinatorWorkerPolicy(tuple(controls.get('workerTools', request['profile']['tools'])),
        request['model'], deepcopy(policy['config']), tuple(controls.get('tools', ())),
        controls.get('model', request['model']))


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


def capture_worker_policy(task, config, catalog, model, *, worker_model=None):
    """Called before the Coordinator's own planning-only tool projection.

    ``model`` is the Coordinator's own model. ``worker_model`` pins the Workers'
    model; omitted, the first delegation resolves it with resolve_worker_model.
    """
    from .continuation import current_continuation
    continuation = current_continuation(task)
    if continuation is not None:
        policy = continuation['request']['delegation']['policy']
        controls = policy['continuation']
        # Grants issued before separate Worker models recorded one shared model.
        task['_coordinatorWorkerPolicy'] = CoordinatorWorkerPolicy(
            tuple(controls['workerTools']), controls.get('workerModel', controls['model']),
            deepcopy(policy['config']), tuple(controls['tools']), controls['model'])
        return
    denied = set(CONTRACT['workerDeniedTools'])
    # A Worker has the capabilities of an ordinary conversation of this human:
    # the gateway's program action is admitted like any other tool, and its
    # tool settings are the human's own, not the Coordinator role's.
    names = tuple(sorted({'search_tools', 'execute_tools', 'execute_program'} | {
        item['function']['name'] for item in catalog if item['function']['name'] not in denied}))
    keys = CONTRACT['$defs']['workerPolicy']['properties']['config']['properties']
    selected = {key: deepcopy(config[key]) for key in keys if config.get(key) is not None}
    from lib.tasks_pkg.coordinator_policy import submitted_tool_settings
    selected['tools'] = submitted_tool_settings(config)
    selected.update(schedulerEnabled=False, humanGuidanceEnabled=False)
    visible = {item['function']['name'] for item in catalog} | {'search_tools', 'execute_tools'}
    controls = tuple(name for name in CONTRACT['continuationTools'] if name in visible)
    if worker_model is not None:
        task['_coordinatorWorkerModel'] = worker_model
    task['_coordinatorWorkerPolicy'] = CoordinatorWorkerPolicy(names, worker_model, selected, controls, model)


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
    for path in ([] if for_budget else (task.get('config') or {}).get('readOnlyPaths', [])):
        if Path(repository.project).resolve().is_relative_to(Path(path).resolve()):
            raise PermissionError('Read-only project cannot authorize a writing worker')
    turn = repository.turn_get(scope['conversation_id'], task.get('_turnId'), user_id=repository.owner,
                               include_projection=False)
    attempt = repository.get_attempt(task.get('_attemptId'))
    if (not isinstance(turn, dict) or turn.get('actor') != 'assistant'
            or turn.get('conversationId') != scope['conversation_id']
            or turn.get('turnId') != task.get('_turnId')
            or not isinstance(attempt, dict) or attempt.get('status') not in ('pending', 'running')
            or attempt.get('attemptId') != task.get('_attemptId') or attempt.get('taskId') != task.get('id')
            or attempt.get('turnId') != turn['turnId'] or attempt.get('conversationId') != turn['conversationId']):
        raise PermissionError('Delegation source is not the current owned Coordinator attempt')
    # validate_human_source rereads this Turn with its projection for the human-input fence.
    human = repository.turn_get(scope['conversation_id'], turn.get('parentTurnId'), user_id=repository.owner,
                                include_projection=False)
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
    if not isinstance(policy, CoordinatorWorkerPolicy) or not policy.tools:
        raise PermissionError('Coordinator worker capabilities have not been issued by tool assembly')
    if not policy.coordinator_model:
        raise CoordinatorModelMissing(
            'This Coordinator request has no model, so it cannot issue Workers: the human selects '
            'a model in this conversation (or configures a default model), then sends the request again')
    _, prior = request_delegation(repository, task)
    if prior is not None:
        return deepcopy(prior['request']['delegation']['policy'])
    from .request_budget import source_policy
    shared = source_policy(repository, task)
    result = {'config': deepcopy(policy.config)}
    if shared is not None:
        result['requestBudgetId'] = shared['id']
    if {'delegate_project_work', 'get_project_workers', 'finish_project_request', 'report_to_user'} <= set(policy.coordinator_tools):
        result['continuation'] = {'version': 1, 'model': policy.coordinator_model,
            'workerModel': policy.model or resolve_worker_model(task), 'workerTools': list(policy.tools),
            'tools': list(policy.coordinator_tools),
            'maxStalledTurns': CONTRACT['delegationDefaults']['maxStalledTurns']}
    _validate_schema(result, 'workerPolicy')
    return result


def validate_source(repository, request, *, value=None):
    """Live policy/birth validation shared by admission, tools and model sends.

    ``value`` is the caller's loaded dispatch snapshot. When it holds the request
    budget row, that row decides: its authority fields are immutable and its
    accounts append-only, so rereading it within the same check cannot differ.
    """
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
                or request['profile']['tools'] != controls['tools']):
            raise PermissionError('Coordinator continuation differs from its original issued policy')
    elif 'coordinatorWake' in request:
        raise PermissionError('Worker cannot carry a Coordinator continuation')
    source = repository.turn_get(request['coordinatorId'], delegation['turnId'], user_id=repository.owner,
                                 include_projection=False)
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
        budget = policy['requestBudgetId']
        if value is None or not any(row['id'] == budget for row in value.get('requestBudgets', [])):
            value = repository._load(repository._selection(budgets=[budget], cohort=False))[0]
        validate_policy(repository, value, request)
    from .repository import stopped_after
    if stopped_after(repository.owner, request['coordinatorId'], attempt.get('createdAt', 0) / 1000,
                     client=repository.client):
        raise PermissionError('Original Coordinator request was stopped')


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
        worker_control(repository, task, row)
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
        checkpoint = arguments.get('checkpoint_sha')
        old = row.get('publication')
        # The same execution can gain later projection revisions after review
        # (late background-command output). Re-affirming the identical accepted
        # checkpoint at the newer current revision is the only allowed change.
        reaffirm = (row['review'] is not None and row['review'] != review and old is not None
                    and row['review'].get('decision') == 'accept' and review['decision'] == 'accept'
                    and checkpoint == old['checkpointSha']
                    and review['resultRevision'] > row['review']['resultRevision'])
        if row['review'] is not None and row['review'] != review and not reaffirm:
            raise ValueError('Worker review is immutable; delegate a new repair assignment')
        if checkpoint:
            project_root = str(Path(repository.project).expanduser().resolve())
            work_id = repository.workspace_task_id(row)
            workspace = repository.get_workspace(project_root, work_id, user_id=repository.owner)
            if (not workspace or workspace.get('checkpoint_sha') != checkpoint
                    or workspace.get('task_id') != work_id
                    or workspace.get('user_id') != repository.owner or workspace.get('state') == 'discarded'
                    or workspace.get('project_root') != project_root
                    or (workspace.get('origin') or {}).get('_projectDispatchAdmission') != repository._admission(row)):
                raise ValueError('Publication requires the exact registered worker checkpoint')
            origin = row['request']['delegation']
            publication = {'resultRevision': arguments['result_revision'], 'grantRevision': row['grant']['revision'],
                'checkpointSha': checkpoint, 'subject': 'coordinator:' + origin['taskId'], 'requestedAt': repository.clock()}
            if reaffirm:
                old = None
            if old is not None and any(old[key] != publication[key]
                                      for key in ('resultRevision', 'grantRevision', 'checkpointSha')):
                raise ValueError('Worker publication is immutable')
            # Human steering may review earlier work. Keep publication anchored
            # to that Worker's original grant, including when an authorized
            # repeat review repairs an older receipt attributed to the reviewer.
            row['publication'] = {**(old or publication), 'subject': publication['subject']}
        row['review'] = review
        return row
    return repository._change(record, selected=repository._assignment_selection(arguments['assignment_id']))


def validate_delegation_policy(repository, value, request):
    """Fence request identity and issued capabilities inside the grant CAS.

    Legacy maxConcurrent values remain immutable receipt data, never admission
    limits. Neither active Worker count nor local memory can refuse a grant.
    """
    delegation = request['delegation']
    from .request_budget import validate_policy
    validate_policy(repository, value, request)
    for row in value['assignments']:
        prior = row['request'].get('delegation')
        if not prior or prior['attemptId'] != delegation['attemptId']:
            continue
        if prior != delegation:
            raise ProjectDispatchError('Delegation source policy changed within one request')
