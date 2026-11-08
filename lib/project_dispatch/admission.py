"""Trusted project dispatch admission; no new loop, budget or task lifecycle.

The project repository owns explicit human grants and conserved physical-attempt
receipts. ExecutionBudget remains the admission/settlement owner. Project and
Coordinator grants carry no spend ceiling: occurrences are recorded and missing
receipts stay lower bounds. Earlier finite grants keep their recorded ceilings.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass
from collections.abc import Mapping
from pathlib import Path
import time

from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError, ToolAdmissionRefused
from lib.log import get_logger
from lib.tools.execution_timing import timing_phase

KEY = '_projectDispatchAdmission'
WORKSPACE_TASK_KEY = '_projectDispatchWorkspaceTaskId'
ROOT_TASK_KEY = '_projectDispatchRootTask'
logger = get_logger(__name__)


class _LiveDispatchRoot:
    """Process-local reference to the bound Worker task; copies share, never clone, it."""
    __slots__ = ('task',)

    def __init__(self, task):
        self.task = task

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def share_dispatch_root(parent, carrier):
    """Let a sub-agent carrier borrow its parent's live dispatch authority.

    The carrier holds no grant of its own: every admission revalidates the live
    root task, so the Worker's revocation, Stop or settlement fences it too.
    """
    if parent is None or not is_dispatch_task(parent):
        return
    carrier[ROOT_TASK_KEY] = _LiveDispatchRoot(dispatch_root(parent))


def dispatch_root(task):
    """The bound physical task whose grant admits ``task`` (itself for a root)."""
    holder = task.get(ROOT_TASK_KEY)
    if holder is None:
        return task
    root = holder.task if isinstance(holder, _LiveDispatchRoot) else None
    if (not isinstance(root, dict) or ROOT_TASK_KEY in root
            or task.get('_principalContext') != root.get('_principalContext')
            or task.get('convId') != root.get('convId')
            or task.get('_executionBudget') is not root.get('_executionBudget')
            or task.get('aborted')):
        raise AbortedError('Project dispatch sub-agent no longer shares its Worker authority')
    return root


def authority_stop_reason(error):
    """``dispatch_grant_revoked`` when a revocation caused ``error``, else ``dispatch_authority_lost``."""
    from lib.turn_verdict import DISPATCH_AUTHORITY_LOST, DISPATCH_GRANT_REVOKED
    from .repository import DispatchGrantRevoked
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, DispatchGrantRevoked):
            return DISPATCH_GRANT_REVOKED
        seen.add(id(error))
        error = error.__cause__ if error.__cause__ is not None else (
            None if error.__suppress_context__ else error.__context__)
    return DISPATCH_AUTHORITY_LOST


def stop_on_authority_failure(task, error):
    """Halt the task before any further provider/tool call, naming the dispatch cause.

    The first cause wins: a task already stopped by a person, a fence or an
    earlier authority failure keeps its own reason.
    """
    event = task.get('abort_event')
    if task.get('aborted') or (event is not None and event.is_set()):
        return
    reason = authority_stop_reason(error)
    task['_abort_reason'] = reason
    task['_abortReasonDetail'] = f'Authority check refused: {error}'
    task['_abort_timestamp'] = time.time()
    task['aborted'] = True
    if event is not None:
        event.set()
    logger.warning('[ProjectDispatch] authority stopped task=%s reason=%s: %s',
                   str(task.get('id') or '?')[:8], reason, error)


def repository_for(binding):
    from .repository import ProjectDispatchRepository
    return ProjectDispatchRepository(binding['owner'], binding['projectPath'])


def wait_for_dispatch(task, operation, *, label, cancellable=True, effect_free=False):
    """``wait_for_persistence`` that re-runs one dispatch read or ledger write through contention.

    A lost head CAS or a torn snapshot proves only that another writer
    committed: the operation reads and validates the live grant again and
    proceeds, or meets a real verdict. It is bounded by the persistence-wait
    liveness contract (ACK, owner Stop, server shutdown), never by a count or
    timeout of its own; its existing backoff lets a burst of writers drain.
    """
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    from .repository import transient_dispatch_failure
    return wait_for_persistence(task, operation, label=label, cancellable=cancellable,
                                effect_free=effect_free, transient=transient_dispatch_failure)


def reread_on_contention(task, operation, *, label, cancellable=True, effect_free=False):
    """Run ``operation`` once; only a transient dispatch failure re-runs it in ``wait_for_dispatch``.

    For call sites without a persistence fence of their own: an uncontended call
    keeps its existing Stop handling, and only a lost CAS, a torn snapshot or a
    transient storage failure enters the wait.
    """
    from .repository import transient_dispatch_failure
    try:
        return operation()
    except Exception as error:
        if not transient_dispatch_failure(error):
            raise
    return wait_for_dispatch(task, operation, label=label, cancellable=cancellable, effect_free=effect_free)


def principal_for(binding):
    return PrincipalContext.system(subject_id='project-dispatch:' + binding['assignmentId'],
        owner_user_id=binding['owner'], tenant_id=binding['tenant'], scopes={'chat'})


def profile_for(assignment):
    if assignment['request'].get('profile') is not None:
        from .scope import profile_for as checked_profile
        return checked_profile(assignment)
    return {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}


def asks_human(profile):
    """Only a Coordinator continuation whose issued policy froze ask_human may ask.

    The Coordinator conversation is the human's channel, so its questions reach
    a reader even without an attending request. Older policies never gain this.
    """
    return profile['mode'] == 'coordinator' and 'ask_human' in profile['tools']


def requires_isolated_workspace(task):
    """File grants retain reviewed candidates regardless of direct preference.

    This selects storage policy only; task/account/IO admission still validates
    the bound physical executor. Ordinary tasks never read dispatch storage.
    """
    if not is_dispatch_task(task):
        return False
    binding = (task.get('config') or {}).get(KEY)
    if not isinstance(binding, dict):
        raise AbortedError('Project dispatch lost its accepted grant carrier')
    assignment = repository_for(binding).validate(binding)
    return profile_for(assignment)['mode'] in ('files', 'project')


def owned_workspace(task, binding=None):
    """Neither config, cwd nor a cached receipt can transfer a worktree."""
    task = dispatch_root(task)
    binding = binding or (task.get('config') or {}).get(KEY)
    if not isinstance(binding, dict):
        raise ToolAdmissionRefused('File execution has no accepted project binding')
    from lib.integration_state_repository import find_workspace
    from lib.tasks_pkg.workspace_delivery import execution_repositories, workspace_work_id
    # The grant keeps the conversation's project key. Integration registers the
    # physical root; compare it with the captured binding so retargeted aliases
    # cannot adopt another checkout. The checkout is keyed by the assignment's
    # work id that bind_account stamped from the validated association.
    project_root = str(Path(binding['projectPath']).expanduser().resolve())
    work_id = workspace_work_id(task)
    with timing_phase('dispatch_authority'):
        row = find_workspace(project_root, work_id, user_id=binding['owner'])
    cfg = task.get('config') or {}
    if (not isinstance(row, dict) or row.get('user_id') != binding['owner']
            or row.get('task_id') != work_id or row.get('project_root') != project_root
            or row.get('state') == 'discarded' or row.get('workspace_path') != cfg.get('projectPath')
            or row.get('workspace_path') == project_root
            or not any(item.get('canonicalRoot') == project_root
                       and item.get('workspacePath') == row.get('workspace_path')
                       and item.get('workId') == work_id
                       for item in execution_repositories(task))):
        raise ToolAdmissionRefused('File execution requires its registered owned isolated worktree')
    return row


class WorkerWorkspaceUnavailable(ValueError):
    """The project root cannot host the isolated Git checkout every worker needs.

    The refusal stays true until the human changes the project, so it carries
    its own Coordinator recovery instead of the generic retry advice.
    """
    execution_started = False
    recovery = ('No worker was created. Delegating again fails the same way until the human '
                'changes the project root; report this blocker and its remedy to the human '
                'instead of retrying.')


def require_isolated_worker_root(project_path):
    """Refuse delegation before any worker conversation or grant exists."""
    from lib.tasks_pkg.workspace_delivery import isolated_workspace_refusal
    refusal = isolated_workspace_refusal(project_path)
    if refusal is not None:
        raise WorkerWorkspaceUnavailable(refusal.message())



# Model-view selections of the human request a Coordinator continuation resumes.
# Never authority: the grant fixes capabilities, tools and the model.
REQUEST_SELECTION_FIELDS = ('chatMode', 'memoryEnabled', 'thinkingEnabled', 'defaultThinkingDepth')
REQUEST_ROUTE_FIELDS = ('modelRef', 'preferredProviderId', 'routing', 'preset', 'provider_id', 'providerId')


def request_selection(binding, assignment):
    """Selections a Coordinator continuation keeps from its accepted human request.

    The continuation resumes that request's conversation and model view. Admitted
    from the grant alone it ran under another route and replay policy, so every
    alternation with a human Turn rebuilt the whole history. Experiment blocks
    are copied normalized (the grant owns ``tools``); a route naming another
    model than the grant's is not inherited.
    """
    from lib.context_experiment_flags import normalize_context_experiment_flags
    request = assignment['request']
    # validate() already fenced this exact source attempt of the delegation.
    accepted = repository_for(binding).accepted_attempt_config(request['delegation']['attemptId'])
    selection = {key: deepcopy(accepted[key]) for key in REQUEST_SELECTION_FIELDS if key in accepted}
    selection.update((block, value) for block, value in
                     normalize_context_experiment_flags(accepted).items() if block != 'tools')
    route = accepted.get('modelRef')
    if (route.get('model_id') if isinstance(route, dict) else accepted.get('model')) == request['model']:
        selection.update((key, deepcopy(accepted[key])) for key in REQUEST_ROUTE_FIELDS if key in accepted)
    return selection


def execution_limits(assignment):
    """Absent request dimensions are unbounded; earlier records keep their values."""
    budget = assignment['request']['budget']
    return {'max_tokens': budget.get('maxTokens'), 'max_tool_calls': profile_for(assignment).get('maxToolCalls'),
            'deadline': budget.get('deadline')}


def unbounded_dispatch(assignment):
    """Project/Coordinator grants issued without ceilings record usage, never refuse it."""
    return profile_for(assignment)['mode'] in ('project', 'coordinator') and all(
        value is None for value in execution_limits(assignment).values())


@dataclass(frozen=True)
class TrustedDispatch:
    """Internal keyword-only authority; a public config cannot instantiate it."""
    binding: dict

    def checked(self):
        return repository_for(self.binding).validate(self.binding)

    @property
    def principal(self):
        self.checked()
        return principal_for(self.binding)

    def capture(self, config, owner, principal, recipient):
        assignment = self.checked()
        if (owner != self.binding['owner'] or recipient != self.binding['recipientId']
                or principal != principal_for(self.binding)):
            raise PermissionError('Project dispatch identity does not match its authorization')
        from lib.conversation_sync.execution_identity import public_execution_fields
        config = public_execution_fields(config)

        profile = profile_for(assignment)
        if profile['mode'] in ('project', 'coordinator'):
            config = deepcopy(assignment['request']['delegation']['policy']['config'])
        if profile['mode'] == 'coordinator':
            config = {**request_selection(self.binding, assignment), **config}
        config.update({KEY: deepcopy(self.binding),
            WORKSPACE_TASK_KEY: self.task_id,
            '_turnPrincipalTenantId': self.binding['tenant'] or '',
            'canonicalProjectPath': self.binding['projectPath'], 'projectPath': self.binding['projectPath'],
            'projectPaths': [], 'readOnlyPaths': [self.binding['projectPath']] if profile['mode'] == 'consult' else [],
            'model': assignment['request']['model'], 'executionLimits': execution_limits(assignment),
            'planMode': False, 'autopilot': False, 'activeFlow': '', 'humanGuidanceEnabled': asks_human(profile),
            'autoTranslate': False})
        if profile['mode'] != 'project':
            config['tools'] = {**config.get('tools', {}), 'programmaticCalling': 'off'}
        if profile['mode'] not in ('project', 'coordinator'):
            config.update(searchEnabled=False, fetchEnabled=False, codeExecEnabled=False,
                          browserEnabled=False, desktopEnabled=False)
        return config

    def bind_execution(self, turn_id, attempt_id):
        self.checked()
        repository = repository_for(self.binding)
        # One assignment owns its checkout across all of its resumed attempts.
        task_id = self.task_id
        return repository.bind_execution(self.binding['assignmentId'], turn_id, attempt_id,
                                         task_id=task_id)

    def continue_execution(self, turn_id, attempt_id):
        assignment = self.checked()
        execution = assignment['execution']
        if execution is None:
            return self.bind_execution(turn_id, attempt_id)
        if execution['attemptId'] == attempt_id and execution['turnId'] == turn_id:
            return assignment
        return repository_for(self.binding).continue_execution(
            self.binding['assignmentId'], execution, turn_id, attempt_id)

    @property
    def repository(self):
        return repository_for(self.binding)

    @property
    def task_id(self):
        assignment = self.checked()
        execution = assignment.get('execution') or {}
        if execution.get('taskId'):
            return execution['taskId']
        if execution:
            predecessor = self.repository.get_attempt(execution['attemptId'])
            if predecessor and predecessor.get('taskId'):
                return predecessor['taskId']
        return 'project-worker:' + self.binding['assignmentId']


def continue_recovered_execution(config, turn_id, attempt_id):
    """Bind or advance the association for an attempt started by recovery.

    Accepted-attempt recovery can claim an attempt whose delegation committed it
    but has not yet bound it. Under that exclusive claim this performs the same
    idempotent bind/exact continuation from the accepted config's server-captured
    carrier, so binding still precedes executor birth. Other configs are untouched.
    """
    if KEY not in config:
        return None
    if not isinstance(config[KEY], dict):
        raise PermissionError('Malformed project dispatch authorization')
    return TrustedDispatch(config[KEY]).continue_execution(turn_id, attempt_id)


def restore_dispatch_identity(config, *, task=None):
    principal, attended, _ = _restored_identity(config, task=task)
    return principal, attended


def _restored_identity(config, *, task=None):
    """The restored identity plus the assignment row its one live validation returned."""
    binding = config.get(KEY)
    if not isinstance(binding, dict):
        raise PermissionError('Malformed project dispatch authorization')
    assignment = repository_for(binding).validate(binding)
    runtime_path = config.get('projectPath')
    profile = profile_for(assignment)
    if (config.get('_turnOwnerUserId') != binding['owner']
            or config.get('_turnPrincipalTenantId', '') != (binding['tenant'] or '')
            or config.get('projectPaths') != []
            or config.get('readOnlyPaths') != ([binding['projectPath']] if profile['mode'] == 'consult' else [])
            or config.get('canonicalProjectPath') != binding['projectPath']
            or config.get('executionLimits') != execution_limits(assignment)
            or config.get('model') != assignment['request']['model']
            or config.get('autopilot') is not False or config.get('planMode') is not False
            or config.get('activeFlow') or config.get('humanGuidanceEnabled') is not asks_human(profile)
            or config.get('autoTranslate') is not False
            or '_conversationExecutionIdentity' in config):
        raise PermissionError('Project dispatch config differs from its accepted grant')
    if runtime_path != binding['projectPath']:
        if task is None or profile['mode'] not in ('files', 'project'):
            raise PermissionError('Project dispatch project path differs from its accepted grant')
        owned_workspace(task, binding)
    if profile['mode'] in ('project', 'coordinator'):
        original = assignment['request']['delegation']['policy']['config']
        # humanGuidanceEnabled is fenced exactly above: the shared policy config
        # disables it for Workers, while a Coordinator continuation may ask.
        for key, value in original.items():
            if key not in ('tools', 'humanGuidanceEnabled') and config.get(key) != value:
                raise PermissionError('Project worker capability policy changed after admission')
        expected = (original.get('tools', {}) if profile['mode'] == 'project'
                    else {**original.get('tools', {}), 'programmaticCalling': 'off'})
        if config.get('tools', {}) != expected:
            raise PermissionError('Project worker programmatic policy changed after admission')
    return principal_for(binding), False, assignment


def is_file_dispatch_task(task):
    """Only descriptor-confined file grants use the fixed first-party handler."""
    if not is_dispatch_task(task):
        return False
    _, assignment = validated_dispatch(task)
    return profile_for(assignment)['mode'] == 'files'


def is_dispatch_task(task):
    payload = task.get('_principalContext') or {}
    return (KEY in (task.get('config') or {}) or '_projectDispatchBinding' in task
            or isinstance(payload, dict) and payload.get('kind') == 'system'
            and str(payload.get('subject_id', '')).startswith('project-dispatch:'))


def validate_task(task, *, require_account=True):
    """Re-establish the bound authority; a failure also stops the task with its cause."""
    validated = validated_dispatch(task, require_account=require_account)
    return None if validated is None else validated[0]


def validated_dispatch(task, *, require_account=True):
    """``validate_task`` that also returns the assignment row its single validation read.

    The row serves only the caller's current decision (its immutable request and
    execution association); it is never retained, so every later admission
    validates live authority again.
    """
    if not is_dispatch_task(task):
        return None
    try:
        with timing_phase('dispatch_authority'):
            return wait_for_dispatch(task,
                lambda: _validated_binding(task, require_account=require_account),
                label='project dispatch authority', effect_free=True)
    except AbortedError as error:
        stop_on_authority_failure(task, error)
        raise


def _validated_binding(task, *, require_account):
    config = task.get('config') or {}
    if not is_dispatch_task(task):
        return None
    root = dispatch_root(task)
    if root is not task:
        return _validated_binding(root, require_account=require_account)
    if KEY not in config:
        raise AbortedError('Project dispatch lost its accepted grant carrier')
    try:
        # The validated row is current evidence: its association is immutable once
        # bound, and binding precedes executor birth, so a reread cannot differ.
        principal, attended, assignment = _restored_identity(config, task=task)
    except Exception as error:
        raise AbortedError(f'Project dispatch authority is no longer available: {error}') from error
    execution = assignment['execution']
    if (execution is None or task.get('_turnId') != execution['turnId']
            or task.get('_attemptId') != execution['attemptId']):
        raise AbortedError('Project dispatch execution does not match its immutable birth association')
    if (task.get('_principalContext') != principal.to_payload() or task.get('_attended') is not attended
            or task.get('convId') != config[KEY]['recipientId']
            or task.get('_parentTaskId') or task.get('aborted')):
        raise AbortedError('Project dispatch task identity is no longer authorized')
    if require_account:
        repository = repository_for(config[KEY])
        if repository.attempt_get is None:
            from lib.turn_lifecycle import get_attempt
            attempt = get_attempt(execution['attemptId'], user_id=config[KEY]['owner'])
        else:
            attempt = repository.attempt_get(execution['attemptId'], user_id=config[KEY]['owner'])
        if (not isinstance(attempt, dict) or attempt.get('taskId') != task.get('id')
                or attempt.get('attemptId') != execution['attemptId']
                or attempt.get('turnId') != execution['turnId']
                or attempt.get('conversationId') != config[KEY]['recipientId']
                or attempt.get('status') not in ('pending', 'running')):
            raise AbortedError('Project dispatch physical task is not the current bound executor')
    if require_account and (task.get('_executionBudget') is None
            or id(task['_executionBudget']) != task.get('_projectDispatchAccountId')):
        raise AbortedError('Project dispatch requires its bound conserved execution account')
    return config[KEY], assignment


def bind_account(task):
    binding = validate_task(task, require_account=False)
    if binding is None:
        return

    task['_projectDispatchBinding'] = deepcopy(binding)
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)

    from .accounting import holder_consumption
    assigned = wait_for_dispatch(task,
        lambda: repository_for(binding).read(binding['assignmentId']),
        label='project dispatch account', effect_free=True)
    # The checkout and the occurrence ledger belong to the assignment. Each
    # physical attempt is its own durable-worker task, so every attempt borrows
    # the association's work id: it keys the checkout registration and is the
    # ledger's root, and a resumed attempt continues both.
    from lib.tasks_pkg.workspace_delivery import WORKSPACE_WORK_ID_KEY
    work_id = repository_for(binding).workspace_task_id(assigned)
    task[WORKSPACE_WORK_ID_KEY] = work_id

    resume = (task.get('config') or {}).get('_resumeSource') or {}
    account_nonce = next((row['account_nonce'] for row in assigned.get('modelAttempts', [])
                          if row.get('account_nonce')), None) if resume else None
    if account_nonce is None and resume:
        account_nonce = (assigned.get('toolReservations') or {}).get('accountNonce')
    if account_nonce is None:
        import uuid
        account_nonce = uuid.uuid4().hex
    shared_id = assigned['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId')
    limited = not unbounded_dispatch(assigned)
    # An admitted restart Continue of this same root continues its stored ordinals.
    execution = assigned.get('execution') or {}
    restarted = ((task.get('config') or {}).get('_turnOperation') in (
                     'continue', 'checkpoint_resume', 'answer_guidance')
                 and resume.get('cause') == 'server_restart'
                 and bool(resume.get('attemptId'))
                 and resume.get('attemptId') != execution.get('attemptId'))
    if restarted:
        predecessor = repository_for(binding).get_attempt(resume['attemptId'])
        if (not isinstance(predecessor, Mapping)
                or predecessor.get('turnId') != execution.get('turnId')
                or predecessor.get('conversationId') != assigned['request']['recipientId']
                or predecessor.get('status') != 'interrupted'):
            raise AbortedError('Project dispatch resume source is not its interrupted predecessor')

    def observe_reservation(event):
        validate_task(task)
        def record(row):
            counter = row['toolReservations']
            if (counter['rootTaskId'] != work_id or counter['accountNonce'] != account_nonce
                    or event['reservation_ordinal'] not in (counter['count'], counter['count'] + 1)):
                raise AbortedError('Worker tool reservation differs from its physical occurrence')
            counter['count'] = event['reservation_ordinal']
        wait_for_dispatch(task,
            lambda: repository_for(binding).mutate(binding['assignmentId'], record),
            label='project dispatch tool reservation')
        validate_task(task)

    reservations = 0
    if shared_id:
        def register_counter(row):
            counter = {'rootTaskId': work_id, 'accountNonce': account_nonce, 'count': 0}
            stored = row.get('toolReservations')
            if stored is None:
                row['toolReservations'] = counter
            elif stored != counter and not (restarted and {**stored, 'count': 0} == counter):
                raise AbortedError('Worker tool reservations cannot be replenished')
        registered = wait_for_dispatch(task,
            lambda: repository_for(binding).mutate(binding['assignmentId'], register_counter),
            label='project dispatch reservation account')
        reservations = registered['toolReservations']['count']
    def observe(event):

        event = {**event, 'root_task_id': work_id, 'account_nonce': account_nonce}
        repository = repository_for(binding)
        if event['phase'] == 'dispatch_intent':
            validate_task(task)
        def record(assignment):
            from .accounting import record_model_event, fold_settled_models, folded_model_event
            if folded_model_event(assignment, event):
                return assignment
            if event['phase'] == 'dispatch_intent' and (assignment['grant']['revoked']
                    or assignment['grant']['revision'] != binding['revision']):
                from .repository import DispatchGrantRevoked
                raise DispatchGrantRevoked('Project dispatch revocation refuses a new request')
            attempts = assignment['modelAttempts']
            if event['phase'] == 'dispatch_intent' and limited and event not in attempts:
                # Earlier finite grants keep their recorded ceiling.
                # An exact intent replay after a lost ACK is not a new request.
                policy = assignment['request']['budget']
                if (len(attempts) >= policy['maxModelRequests']
                        or sum(row['reported_tokens'] for row in attempts) + event['reserved_tokens'] > policy['maxTokens']
                        or any(not row['usage_complete'] and row['root_task_id'] == event['root_task_id']
                               and row['phase'] != 'dispatch_intent' for row in attempts)):
                    raise AbortedError('Model allowance exhausted or prior consumption is unconfirmed')
            record_model_event(attempts, event)
            if not limited:
                fold_settled_models(assignment)
            return assignment
        try:
            wait_for_dispatch(task,
                lambda: repository.mutate(binding['assignmentId'], record),
                label='project dispatch model occurrence',
                cancellable=event['phase'] == 'dispatch_intent')
        except AbortedError:
            raise
        except Exception as error:
            stop_on_authority_failure(task, error)
            from .repository import DispatchGrantRevoked
            message = (str(error) if isinstance(error, DispatchGrantRevoked)
                       else 'Project dispatch usage could not be durably acknowledged')
            raise AbortedError(message) from error
        if event['phase'] == 'dispatch_intent':
            validate_task(task)
    # ExecutionBudget serializes observer admission/settlement. Only locally
    # live occurrences from this exact account may overlap; an intent whose
    # settlement failed remains durable and blocks further effects.
    active_tools = set()

    def observe_tool(event):
        if event['phase'] == 'intent':
            validate_task(task)
        selected = {key: event[key] for key in ('tool_ordinal', 'task_id',
                    'tool_call_id', 'tool_name', 'phase')}
        selected.update(root_task_id=work_id, account_nonce=account_nonce)
        def record(row):
            from .accounting import fold_settled_tools, folded_tool_event
            from .scope import granted_tools
            if folded_tool_event(row, selected):
                return row
            profile = profile_for(row)
            attempts = row.setdefault('toolAttempts', [])
            old = next((item for item in attempts if item['root_task_id'] == selected['root_task_id']
                        and item['tool_ordinal'] == selected['tool_ordinal']), None)
            if old is not None:
                if (old['account_nonce'] != account_nonce or any(old[key] != selected[key]
                        for key in ('task_id', 'tool_call_id', 'tool_name'))):
                    raise AbortedError('Tool settlement belongs to another immutable account occurrence')
                if old['phase'] != 'intent' and old != selected:
                    raise AbortedError('Tool occurrence is already terminal')
                old.update(deepcopy(selected))
            elif selected['phase'] == 'intent':
                # Distinct refusals: a Coordinator must not read an unconfirmed
                # effect as a call budget (none exists unless maxToolCalls is set).
                if selected['tool_name'] not in granted_tools(profile):
                    raise AbortedError(f"Tool {selected['tool_name']!r} is not in this Worker's grant")
                if profile.get('maxToolCalls') is not None and len(attempts) >= profile['maxToolCalls']:
                    raise AbortedError(f"Tool call limit reached: maxToolCalls={profile['maxToolCalls']}")
                blocking = next((item for item in attempts if item['root_task_id'] != selected['root_task_id']
                                 or item['phase'] == 'intent' and not (
                                     profile['mode'] in ('project', 'coordinator')
                                     and item['account_nonce'] == account_nonce
                                     and item['tool_ordinal'] in active_tools)), None)
                if blocking is not None:
                    raise AbortedError(
                        'Prior tool effect remains unconfirmed (not a call budget): %s #%d %s phase=%s; '
                        'its completion was never durably recorded, so further effects are refused'
                        % (blocking['tool_name'], blocking['tool_ordinal'], blocking['tool_call_id'],
                           blocking['phase']))
                attempts.append(deepcopy(selected))
            elif selected['phase'] != 'not_entered':
                raise AbortedError('Cannot settle an unrecorded tool occurrence')
            if not limited:
                from .repository import CONTRACT
                fold_settled_tools(row, CONTRACT['terminalToolPhases'])
            return row
        try:
            wait_for_dispatch(task,
                lambda: repository_for(binding).mutate(binding['assignmentId'], record),
                label='project dispatch tool occurrence',
                cancellable=event['phase'] == 'intent')
        finally:
            if event['phase'] != 'intent':
                active_tools.discard(event['tool_ordinal'])
        if event['phase'] == 'intent':
            validate_task(task)
            active_tools.add(event['tool_ordinal'])
    budget.bind_model_attempt_observer(observe, tool_observer=observe_tool,
        tool_reservation_observer=observe_reservation if shared_id else None)
    if restarted:
        tokens, _, complete = holder_consumption(assigned)
        ordinal = max([0, *[row['attempt_ordinal'] for row in assigned.get('modelAttempts', [])],
                       *assigned.get('settledModelUsage', {}).get('watermarks', {}).values()])
        budget.restore_model_account(
            root_task_id=task['id'], consumed_tokens=tokens,
            last_attempt_ordinal=ordinal, usage_complete=complete)
        tool_ordinal = max([0, *[row['tool_ordinal'] for row in assigned.get('toolAttempts', [])],
                            *assigned.get('settledToolUsage', {}).get('watermarks', {}).values()])
        budget.restore_tool_account(root_task_id=task['id'], last_tool_ordinal=tool_ordinal,
                                    last_reservation_ordinal=reservations)
    task['_projectDispatchAccountId'] = id(budget)
