"""Trusted project consult admission; no new loop, budget or task lifecycle.

The project repository owns explicit human grants and conserved physical-attempt
receipts. ExecutionBudget remains token admission/settlement owner. This first
slice has no tools/children; uncertain or former-task usage refuses new spending.
"""
from __future__ import annotations
from copy import deepcopy
from dataclasses import dataclass

from lib.identity import PrincipalContext
from lib.llm.llm_errors import AbortedError

KEY = '_projectDispatchAdmission'


def repository_for(binding):
    from .repository import ProjectDispatchRepository
    return ProjectDispatchRepository(binding['owner'], binding['projectPath'])


def principal_for(binding):
    return PrincipalContext.system(subject_id='project-dispatch:' + binding['assignmentId'],
        owner_user_id=binding['owner'], tenant_id=binding['tenant'], scopes={'chat'})


def profile_for(assignment):
    if assignment['request'].get('profile') is not None:
        from .scope import profile_for as checked_profile
        return checked_profile(assignment)
    return {'mode': 'consult', 'tools': [], 'readPaths': [], 'writePaths': [], 'maxToolCalls': 0}


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
    binding = binding or (task.get('config') or {}).get(KEY)
    if not isinstance(binding, dict):
        raise AbortedError('File execution has no accepted project binding')
    from lib.integration_state_repository import find_workspace
    from lib.tasks_pkg.workspace_delivery import execution_repositories
    row = find_workspace(binding['projectPath'], str(task.get('id') or ''), user_id=binding['owner'])
    cfg = task.get('config') or {}
    if (not isinstance(row, dict) or row.get('user_id') != binding['owner']
            or row.get('task_id') != task.get('id') or row.get('project_root') != binding['projectPath']
            or row.get('state') == 'discarded' or row.get('workspace_path') != cfg.get('projectPath')
            or row.get('workspace_path') == binding['projectPath']
            or not any(item.get('canonicalRoot') == binding['projectPath']
                       and item.get('workspacePath') == row.get('workspace_path')
                       and item.get('workId') == task.get('id')
                       for item in execution_repositories(task))):
        raise AbortedError('File execution requires its registered owned isolated worktree')
    return row


def execution_limits(assignment):
    budget = assignment['request']['budget']
    return {'max_tokens': budget['maxTokens'], 'max_tool_calls': profile_for(assignment)['maxToolCalls'], 'deadline': budget['deadline']}


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
        config.update({KEY: deepcopy(self.binding),
            '_turnPrincipalTenantId': self.binding['tenant'] or '',
            'canonicalProjectPath': self.binding['projectPath'], 'projectPath': self.binding['projectPath'],
            'projectPaths': [], 'readOnlyPaths': [self.binding['projectPath']] if profile['mode'] == 'consult' else [],
            'model': assignment['request']['model'], 'executionLimits': execution_limits(assignment),
            'planMode': False, 'autopilot': False, 'activeFlow': '', 'humanGuidanceEnabled': False,
            'autoTranslate': False,
            'tools': {**config.get('tools', {}), 'programmaticCalling': 'off'}})
        if profile['mode'] not in ('project', 'coordinator'):
            config.update(searchEnabled=False, fetchEnabled=False, codeExecEnabled=False,
                          browserEnabled=False, desktopEnabled=False)
        return config

    def bind_execution(self, turn_id, attempt_id):
        self.checked()
        return repository_for(self.binding).bind_execution(
            self.binding['assignmentId'], turn_id, attempt_id)


def restore_dispatch_identity(config, *, task=None):
    binding = config.get(KEY)
    if not isinstance(binding, dict):
        raise PermissionError('Malformed project dispatch authorization')
    assignment = repository_for(binding).validate(binding)
    runtime_path = config.get('projectPath')
    profile = profile_for(assignment)
    if runtime_path != binding['projectPath']:
        if task is None or profile['mode'] not in ('files', 'project'):
            raise PermissionError('Project dispatch project path differs from its accepted grant')
        owned_workspace(task, binding)
    if (config.get('_turnOwnerUserId') != binding['owner']
            or config.get('_turnPrincipalTenantId', '') != (binding['tenant'] or '')
            or config.get('projectPaths') != []
            or config.get('readOnlyPaths') != ([binding['projectPath']] if profile['mode'] == 'consult' else [])
            or config.get('canonicalProjectPath') != binding['projectPath']
            or config.get('executionLimits') != execution_limits(assignment)
            or config.get('model') != assignment['request']['model']
            or config.get('autopilot') is not False or config.get('planMode') is not False
            or config.get('activeFlow') or config.get('humanGuidanceEnabled') is not False
            or config.get('autoTranslate') is not False
            or '_conversationExecutionIdentity' in config):
        raise PermissionError('Project dispatch config differs from its accepted grant')
    if profile['mode'] in ('project', 'coordinator'):
        original = assignment['request']['delegation']['policy']['config']
        for key, value in original.items():
            if key != 'tools' and config.get(key) != value:
                raise PermissionError('Project worker capability policy changed after admission')
        if config.get('tools', {}).get('programmaticCalling') != 'off':
            raise PermissionError('Project worker programmatic policy changed after admission')
    return principal_for(binding), False


def is_file_dispatch_task(task):
    """Only descriptor-confined file grants use the fixed first-party handler."""
    if not is_dispatch_task(task):
        return False
    binding = validate_task(task)
    return profile_for(repository_for(binding).read(binding['assignmentId']))['mode'] == 'files'


def is_dispatch_task(task):
    payload = task.get('_principalContext') or {}
    return (KEY in (task.get('config') or {}) or '_projectDispatchBinding' in task
            or isinstance(payload, dict) and payload.get('kind') == 'system'
            and str(payload.get('subject_id', '')).startswith('project-dispatch:'))


def validate_task(task, *, require_account=True):
    config = task.get('config') or {}
    if not is_dispatch_task(task):
        return None
    if KEY not in config:
        raise AbortedError('Project dispatch lost its accepted grant carrier')
    try:
        principal, attended = restore_dispatch_identity(config, task=task)
        assignment = repository_for(config[KEY]).read(config[KEY]['assignmentId'])
    except Exception as error:
        raise AbortedError('Project dispatch authority is no longer available') from error
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
    return config[KEY]


def bind_account(task):
    binding = validate_task(task, require_account=False)
    if binding is None:
        return

    task['_projectDispatchBinding'] = deepcopy(binding)
    from lib.agent_core.execution_budget import _ensure_execution_budget
    budget = _ensure_execution_budget(task)

    import uuid
    account_nonce = uuid.uuid4().hex
    assigned = repository_for(binding).read(binding['assignmentId'])
    shared_id = assigned['request'].get('delegation', {}).get('policy', {}).get('requestBudgetId')

    def check_shared_capacity(value):
        from .request_budget import require_capacity
        require_capacity(repository_for(binding), value, shared_id)

    def observe_reservation(event):
        from lib.agent_core.execution_budget import ModelBudgetExceeded
        from .delegation import DelegationAllowanceExhausted
        validate_task(task)
        def record(row):
            counter = row['toolReservations']
            if (counter['rootTaskId'] != event['root_task_id'] or counter['accountNonce'] != account_nonce
                    or event['reservation_ordinal'] not in (counter['count'], counter['count'] + 1)):
                raise AbortedError('Worker tool reservation differs from its physical occurrence')
            counter['count'] = event['reservation_ordinal']
        try:
            repository_for(binding).mutate(binding['assignmentId'], record, check=check_shared_capacity)
        except DelegationAllowanceExhausted as error:
            raise ModelBudgetExceeded('execution_tool_budget_exhausted') from error

    if shared_id:
        def register_counter(row):
            counter = {'rootTaskId': task['id'], 'accountNonce': account_nonce, 'count': 0}
            if row.get('toolReservations') is not None and row['toolReservations'] != counter:
                raise AbortedError('Worker tool reservations cannot be replenished')
            row['toolReservations'] = counter
        repository_for(binding).mutate(binding['assignmentId'], register_counter)
    def observe(event):

        event = {**event, 'account_nonce': account_nonce}
        repository = repository_for(binding)
        if event['phase'] == 'dispatch_intent':
            validate_task(task)
        def record(assignment):
            from .accounting import record_model_event
            if event['phase'] == 'dispatch_intent' and (assignment['grant']['revoked']
                    or assignment['grant']['revision'] != binding['revision']):
                raise AbortedError('Project dispatch budget or revocation refuses a new request')
            policy = assignment['request']['budget']
            record_model_event(assignment['modelAttempts'], event,
                max_tokens=policy['maxTokens'], max_requests=policy['maxModelRequests'])
            return assignment
        try:
            repository.mutate(binding['assignmentId'], record, **(
                {'check': check_shared_capacity} if shared_id and event['phase'] == 'dispatch_intent' else {}))
        except AbortedError:
            raise
        except Exception as error:
            raise AbortedError('Project dispatch usage could not be durably acknowledged') from error
        if event['phase'] == 'dispatch_intent':
            validate_task(task)
    # ExecutionBudget serializes observer admission/settlement. Only locally
    # live occurrences from this exact account may overlap; an intent whose
    # settlement failed remains durable and blocks further effects.
    active_tools = set()

    def observe_tool(event):
        if event['phase'] == 'intent':
            validate_task(task)
        selected = {key: event[key] for key in ('root_task_id', 'tool_ordinal', 'task_id',
                    'tool_call_id', 'tool_name', 'phase')}
        selected['account_nonce'] = account_nonce
        def record(row):
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
                if (selected['tool_name'] not in profile['tools']
                        or len(attempts) >= profile['maxToolCalls']
                        or any(item['root_task_id'] != selected['root_task_id']
                               or item['phase'] == 'intent' and not (
                                   profile['mode'] in ('project', 'coordinator') and item['account_nonce'] == account_nonce
                                   and item['tool_ordinal'] in active_tools) for item in attempts)):
                    raise AbortedError('Tool authority exhausted or prior effect remains unconfirmed')
                attempts.append(deepcopy(selected))
            elif selected['phase'] != 'not_entered':
                raise AbortedError('Cannot settle an unrecorded tool occurrence')
            return row
        try:
            repository_for(binding).mutate(binding['assignmentId'], record)
        finally:
            if event['phase'] != 'intent':
                active_tools.discard(event['tool_ordinal'])
        if event['phase'] == 'intent':
            validate_task(task)
            active_tools.add(event['tool_ordinal'])
    budget.bind_model_attempt_observer(observe, tool_observer=observe_tool,
        tool_reservation_observer=observe_reservation if shared_id else None)
    task['_projectDispatchAccountId'] = id(budget)
