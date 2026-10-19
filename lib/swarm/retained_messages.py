"""Persistent-colleague messaging over the retained identity and ordinary Swarm.

Entry points send/read pin one message to one assignment in the retained record.
Identity CAS owns routing/deduplication; run mailboxes own input receipts; Swarm
owns all execution. No idle executor, new storage namespace or wake loop exists.
An uncertain activation is evidence lookup, never permission to spawn again.
"""
from __future__ import annotations

import copy
import hashlib
import uuid

from lib.swarm import message_service
from lib.swarm.retained import _reconcile, _repository
from lib.swarm.retained_launch import apply_launch_outcome, classify_launch, launch_lookup
from lib.swarm.retained_repository import (
    RetainedAgentError,
)
from lib.swarm.run_messages import RunMessageError, validate_message


def _target(task, args, project_path, *, sending):
    fields = {'retained_agent_id', 'scope', 'idempotency_key'}
    if sending:
        fields |= {'content', 'kind', 'intent'}
    if not isinstance(args, dict):
        raise RunMessageError('message_invalid_arguments', 'Message arguments must be an object')
    extras = set(args) - fields
    if extras:
        if extras & {'agent_id', 'swarm_run_id'}:
            raise RunMessageError('message_invalid_arguments',
                'Choose a stable colleague OR an exact run, not both. '
                'Temporary spawn_agents children use agent_id and swarm_run_id; '
                'omit retained_agent_id, scope and intent.')
        raise RunMessageError('message_invalid_arguments',
            'Unknown stable-message fields: ' + ', '.join(sorted(extras)))
    from lib.swarm.team_messages import worker_binding
    if worker_binding(task) is not None:
        raise RunMessageError('message_forbidden', 'Workers cannot activate or inspect retained colleagues')
    message_service._caller(task)
    agent_id = args.get('retained_agent_id')
    key = args.get('idempotency_key')
    if not isinstance(agent_id, str) or not 1 <= len(agent_id) <= 256:
        raise RunMessageError('message_invalid_identity', 'retained_agent_id is required')
    if not isinstance(key, str) or not 1 <= len(key) <= 256:
        raise RunMessageError('message_invalid_identity', 'idempotency_key is required for a stable message')

    try:
        key.encode('utf-8')
    except UnicodeEncodeError as error:
        raise RunMessageError('message_invalid_identity', 'idempotency_key must be valid UTF-8') from error
    if 'scope' in args and args['scope'] not in ('project', 'conversation'):
        raise RunMessageError('message_invalid_arguments', 'scope must be project or conversation')
    try:
        repository, scope = _repository(task, project_path=project_path,
                                       requested_scope=args.get('scope', ''))
        found = repository.get(scope, agent_id)
    except RetainedAgentError as error:
        raise RunMessageError('message_identity_unavailable', str(error)) from error
    if found is None:
        raise RunMessageError('message_identity_unavailable', 'No owned colleague exists in the current scope')
    return repository, scope, agent_id, hashlib.sha256(key.encode('utf-8')).hexdigest(), found[0]


def _scope_name(scope):
    return 'project' if scope.startswith('p:') else 'conversation'


def _caller_active(task, abort_check=None):
    if (task.get('aborted') or (task.get('abort_event') is not None and task['abort_event'].is_set())
            or (abort_check is not None and abort_check())):
        raise RunMessageError('message_caller_stopped', 'The current caller is stopped; no work was authorized')


def _validate_conversation(task, project_path):
    from lib.conversations.repository import ConversationRepository
    from lib.conversations.project_identity import normalize_project_path
    owner, _, conv_id = message_service._caller(task)
    if not conv_id:
        raise RunMessageError('message_scope_unavailable', 'Stable messaging requires an owned conversation')
    conv = ConversationRepository().get(conv_id, user_id=owner, include_messages=False)
    settings = conv.metadata.get('settings') if conv is not None else None
    if (not isinstance(settings, dict) or settings.get('archived') or settings.get('readOnly')
            or normalize_project_path(settings.get('projectPath') or '') != normalize_project_path(project_path)):
        raise RunMessageError('message_scope_unavailable', 'Current conversation/project scope is unavailable')


def _owned_session(repository, assignment, agent_id, scope):
    from lib.swarm.persistence import load_session
    row = load_session(assignment['swarm_key'], required=True)
    if not isinstance(row, dict):
        return None
    config = row.get('config')
    if not isinstance(config, dict):
        return None
    parent_cfg = config.get('parent_cfg') or {}
    specs, children = row.get('specs'), row.get('agents')
    if (not isinstance(parent_cfg, dict) or not isinstance(specs, list)
            or not isinstance(children, list)
            or not assignment.get('swarm_run_id') or not assignment.get('conversation_id')
            or str(config.get('user_id')) != str(repository.user_id)
            or str(parent_cfg.get('_tenant_id') or '') != repository.tenant_id
            or row.get('task_id') != assignment['swarm_run_id']
            or row.get('conv_id') != assignment['conversation_id']):
        return None
    matching = [spec for spec in specs
                if isinstance(spec, dict) and spec.get('id') == assignment['agent_id']]
    actual = [child for child in children
              if isinstance(child, dict) and child.get('agent_id') == assignment['agent_id']]
    if (len(matching) != 1 or len(actual) != 1
            or matching[0].get('retained_agent_id') != agent_id
            or matching[0].get('retained_scope') != scope
            or matching[0].get('retained_run_id') != assignment['agent_id']):
        return None
    return row


def _receipt(agent_id, scope, operation, *, state, launch=None, mailbox=None):
    assignment = {key: operation['assignment'][key] for key in (
        'agent_id', 'swarm_run_id', 'conversation_id')}
    if launch is not None:
        assignment['launch'] = launch
    if mailbox is not None:
        assignment['message_id'] = mailbox['message_id']
    return {'retained_agent_id': agent_id, 'scope': _scope_name(scope),
            'message_id': operation['message_id'], 'state': state,
            'kind': operation['request']['kind'],
            'contentChars': len(operation['request']['content']),
            'execution_started': state == 'turn_started', 'assignment': assignment}


def _unknown():
    raise RunMessageError('message_activation_unknown',
        'This message is pinned to its original assignment; execution/submission is not yet proved. '
        'Inspect get_agent_messages with the same stable identity and idempotency_key. Do not launch a replacement.',
        submission_state='unknown')


def _observe(task, repository, scope, agent_id, operation, *, raise_failed=True):
    """Read-only outcome resolution; never activates work or updates a receipt."""
    if operation.get('outcome') is not None:
        outcome = copy.deepcopy(operation['outcome'])
        if raise_failed and outcome.get('status') == 'error':
            raise RunMessageError(outcome['code'], outcome.get('message') or 'Message was rejected',
                                  submission_state=outcome.get('submission_state', 'unknown'))
        if operation['mode'] != 'active_run' or outcome.get('status') == 'error':
            return outcome
        # Busy receipt states can advance to confirmed/unconfirmed. Read the
        # pinned original mailbox rather than freezing an obsolete submission.

    assignment = operation['assignment']
    if operation['mode'] == 'active_run':
        request = {'agent_id': assignment['agent_id'], 'swarm_run_id': assignment['swarm_run_id'],
                   'content': operation['request']['content'], 'kind': operation['request']['kind'],
                   'idempotency_key': operation['message_id']}
        original_task = {**task, 'convId': assignment['conversation_id']}
        try:
            mailbox = message_service.existing_receipt(original_task, request)
        except RunMessageError:
            # Current target scope can be revoked after submission. The owned
            # stable record still proves the original outcome, never rejection.
            if operation.get('outcome') is not None:
                return copy.deepcopy(operation['outcome'])
            _unknown()
        if mailbox is not None:
            return _receipt(agent_id, scope, operation, state=mailbox['state'], mailbox=mailbox)
    else:
        value = repository.get(scope, agent_id)
        current = value[0] if value else {}
        last = current.get('last_assignment')
        witnesses = []
        if (current.get('state') == 'busy'
                and current.get('generation') == operation['generation']):
            witnesses.append({'agent_id': current.get('run_id'),
                'swarm_run_id': current.get('run_session_id'),
                'conversation_id': current.get('run_conversation_id'),
                'swarm_key': current.get('swarm_key')})
        if isinstance(last, dict):
            witnesses.append(last)
        # A launch exception may lose even the returned lookup handle. The
        # child factory's actual binding or immutable completion can identify a
        # reused older session, but never a different assignment/scope/generation.
        bound = next((w for w in witnesses if all(w.get(key) == assignment.get(key)
            for key in ('agent_id', 'conversation_id', 'swarm_key'))
            and isinstance(w.get('swarm_run_id'), str) and w['swarm_run_id'].strip()), None)
        lookup = operation.get('launch_lookup') or {}
        session_id = bound['swarm_run_id'] if bound else lookup.get('swarm_run_id')
        if session_id:
            operation = copy.deepcopy(operation)
            operation['assignment']['swarm_run_id'] = session_id
            assignment = operation['assignment']
        proved = (isinstance(last, dict) and all(last.get(key) == assignment.get(key)
            for key in ('agent_id', 'swarm_run_id', 'conversation_id', 'swarm_key')))
        if not proved:
            row = _owned_session(repository, assignment, agent_id, scope)
            specs = (row or {}).get('specs') or []
            proved = any(spec.get('id') == assignment['agent_id']
                         and spec.get('retained_agent_id') == agent_id
                         and spec.get('retained_scope') == scope
                         and spec.get('retained_run_id') == assignment['agent_id']
                         for spec in specs if isinstance(spec, dict))
            # A session spec is launch intent, not execution acceptance. Require
            # an actual child checkpoint carrying the same exact identity too.
            proved = proved and any(isinstance(child, dict)
                and child.get('agent_id') == assignment['agent_id']
                for child in (row or {}).get('agents') or [])
        if proved:
            return _receipt(agent_id, scope, operation, state='turn_started')
    _unknown()


def _save_outcome(repository, scope, agent_id, op_key, outcome, *, release=False):

    def save(value):
        dispatches = copy.deepcopy(value.get('message_dispatches') or {})
        op = dispatches[op_key]
        if op.get('outcome') is None:
            op['outcome'] = copy.deepcopy(outcome)
        value['message_dispatches'] = dispatches
        if (release and value.get('state') == 'busy'
                and value.get('run_id') == op['assignment']['agent_id']
                and value.get('generation') == op['generation']):
            value.update(state='idle', run_id='', swarm_key='', run_session_id='', run_conversation_id='',
                         run_launch_state='', run_launch_lookup={})
        return value
    return repository.change(scope, agent_id, save)['message_dispatches'][op_key]['outcome']


def send(task: dict, args: dict, *, cfg: dict, all_tools: list, project_path: str,
         on_event=None, abort_check=None) -> dict:
    repository, scope, agent_id, op_key, value = _target(task, args, project_path, sending=True)
    content, kind, intent = args.get('content'), args.get('kind', 'reference'), args.get('intent', 'message')
    validate_message(content=content, kind=kind, key=args['idempotency_key'])
    if not isinstance(intent, str) or intent not in ('message', 'resume'):
        raise RunMessageError('message_invalid_arguments', 'intent must be message or resume')
    _caller_active(task, abort_check)
    _validate_conversation(task, project_path)
    request = {'content': content, 'kind': kind, 'intent': intent}
    existing = (value.get('message_dispatches') or {}).get(op_key)
    if existing is not None:
        if existing['request'] != request:
            raise RunMessageError('message_idempotency_conflict', 'The same idempotency_key has different content, kind or intent')
        return _observe(task, repository, scope, agent_id, existing)
    # Repair only positively settled assignments through the existing owner.
    value = _reconcile(repository, scope, value)
    from lib.swarm.integration import get_active_session, swarm_key_for
    swarm_key = swarm_key_for(task)
    session = get_active_session(swarm_key)
    session_id = session.task_id if session is not None and not session.is_terminated else task['id']
    claim_id = uuid.uuid4().hex
    run_id = uuid.uuid4().hex[:16]
    def reserve(current):
        _caller_active(task, abort_check)
        dispatches = copy.deepcopy(current.get('message_dispatches') or {})
        previous = dispatches.get(op_key)
        if previous is not None:
            if previous['request'] != request:
                raise RunMessageError('message_idempotency_conflict', 'The same idempotency_key has different content, kind or intent')
            return current
        if current.get('state') == 'archived':
            raise RunMessageError('message_identity_archived', 'Restore this colleague before messaging')
        if current.get('state') not in ('idle', 'busy'):
            raise RunMessageError('message_identity_unavailable', 'Unknown colleague state; inspect it first')
        if current['state'] == 'idle':
            if current.get('last_status') in ('cancelled', 'interrupted') and intent != 'resume':
                raise RunMessageError('message_resume_required', 'Explicit intent=resume is required for a fresh turn after cancellation')
            assignment = {'agent_id': run_id, 'swarm_run_id': session_id,
                          'conversation_id': task['convId'], 'swarm_key': swarm_key}
            mode = 'fresh_turn'
            current.update(state='busy', run_id=run_id, swarm_key=swarm_key,
                           run_session_id='', run_conversation_id=task['convId'],
                           run_launch_state='unknown', run_launch_lookup=launch_lookup(
                               run_id=run_id, swarm_key=swarm_key, session_id=session_id))
        else:
            assignment = {'agent_id': current.get('run_id'),
                          'swarm_run_id': current.get('run_session_id') or session_id,
                          'conversation_id': current.get('run_conversation_id') or task['convId'],
                          'swarm_key': current.get('swarm_key')}
            if assignment['conversation_id'] != task['convId'] and not scope.startswith('p:'):
                raise RunMessageError('message_assignment_busy', 'This colleague is working outside the current conversation scope')
            row = _owned_session(repository, assignment, agent_id, scope)
            if (current.get('run_launch_state') == 'unknown'
                    or row is None or row.get('status') != 'running' or not any(
                    isinstance(child, dict) and child.get('agent_id') == assignment['agent_id']
                    and child.get('status') in ('pending', 'running')
                    for child in row.get('agents') or [])):
                raise RunMessageError('message_assignment_unavailable',
                    'The busy assignment is suspended, activating or unknown; inspect it or use existing Continue/reset controls')
            mode = 'active_run'
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, scope + ':' + agent_id + ':' + op_key))
        dispatches[op_key] = {'message_id': message_id, 'request': request, 'assignment': assignment,
                             'generation': current['generation'], 'mode': mode, 'claim_id': claim_id,
                             'outcome': None}
        current['message_dispatches'] = dispatches
        return current
    try:
        reserved = repository.change(scope, agent_id, reserve)

    except RetainedAgentError as error:
        raise RunMessageError('message_identity_unavailable', str(error)) from error
    operation = reserved['message_dispatches'][op_key]
    if operation['claim_id'] != claim_id:
        return _observe(task, repository, scope, agent_id, operation)
    # The durable claim precedes side effects. Only this invocation may execute
    # it; a crash/lost reply leaves an inspectable claim, never a replay lease.
    try:
        _caller_active(task, abort_check)
        _validate_conversation(task, project_path)
        if operation['mode'] == 'active_run':
            assignment = operation['assignment']
            mailbox = message_service.send({**task, 'convId': assignment['conversation_id']}, {
                'agent_id': assignment['agent_id'], 'swarm_run_id': assignment['swarm_run_id'],
                'content': content, 'kind': kind, 'idempotency_key': operation['message_id'],
            }, get_active_session(assignment['swarm_key']), abort_check=lambda: (
                _caller_active(task, abort_check) or False))
            outcome = _receipt(agent_id, scope, operation, state=mailbox['state'], mailbox=mailbox)
            return _save_outcome(repository, scope, agent_id, op_key, outcome)
        from lib.swarm.integration import execute_swarm_tool
        try:
            launched = execute_swarm_tool('spawn_agents', {'agents': [{
                'id': operation['assignment']['agent_id'], 'role': reserved['role'],
                'objective': content, 'max_retries': 0,
            }]}, task, cfg=cfg, all_tools=all_tools, project_path=project_path,
                on_event=on_event, abort_check=abort_check,
                retained_binding={'scope': scope, 'agent_id': agent_id,
                                  'run_id': operation['assignment']['agent_id']})
        except Exception as error:
            # Dispatch may already have admitted a worker. Retain the claim;
            # callers reconcile exact evidence instead of replaying execution.
            raise RunMessageError('message_activation_unknown',
                                  'Ordinary launch did not return an authoritative receipt',
                                  submission_state='unknown') from error
        assignment = operation['assignment']
        launch = classify_launch(launched, run_id=assignment['agent_id'],
            swarm_key=assignment['swarm_key'], conversation_id=assignment['conversation_id'])
        handle = launch.handle
        lookup = launch_lookup(run_id=assignment['agent_id'], swarm_key=assignment['swarm_key'],
                               session_id=assignment['swarm_run_id'], handle=handle)
        operation = copy.deepcopy(operation)
        operation['launch_lookup'] = lookup
        if launch.state == 'accepted':
            operation['assignment']['swarm_run_id'] = handle['swarm_run_id']

        def record_launch(current):
            dispatches = copy.deepcopy(current.get('message_dispatches') or {})
            dispatches[op_key]['launch_lookup'] = lookup
            if launch.state == 'accepted':
                dispatches[op_key]['assignment'] = operation['assignment']
            current['message_dispatches'] = dispatches
            if launch.state == 'rejected':
                # Commit the rejection receipt and release together below.
                return current
            return apply_launch_outcome(current, run_id=assignment['agent_id'],
                generation=operation['generation'], outcome=launch)

        repository.change(scope, agent_id, record_launch)
        if launch.state == 'accepted':
            # Persist launch identities, not repeated objectives or human prose.
            # The full ordinary handle remains owned by the Swarm checkpoint.
            compact_launch = {key: handle[key] for key in ('status', 'swarm_key', 'swarm_run_id')
                              if key in handle}
            compact_launch['agents'] = [{key: child[key] for key in (
                'id', 'role', 'task_id', 'conversation_id', 'swarm_run_id', 'required_for_completion')
                if key in child} for child in handle.get('agents') or []
                if child.get('id') == operation['assignment']['agent_id']]
            outcome = _receipt(agent_id, scope, operation, state='turn_started', launch=compact_launch)
            return _save_outcome(repository, scope, agent_id, op_key, outcome)
        # Only declared pre-admission errors permit release. Generic exceptions
        # or malformed launch replies may follow worker entry and stay unknown.
        if launch.state == 'rejected':
            outcome = {**handle, 'submission_state': 'rejected'}
            _save_outcome(repository, scope, agent_id, op_key, outcome, release=True)
            raise RunMessageError(handle['code'], handle.get('message') or 'Launch rejected')
        _unknown()
    except RunMessageError as error:
        if error.submission_state != 'rejected':
            raise
        outcome = {'status': 'error', 'code': error.code, 'retryable': False,
                   'message': str(error), 'submission_state': 'rejected',
                   'next_action': 'Inspect the original message; use a new key only for a new authorized message.'}
        _save_outcome(repository, scope, agent_id, op_key, outcome,
                      release=operation['mode'] == 'fresh_turn')
        raise

    except Exception as error:
        # The routing claim exists and submission/launch may already be durable.
        # A later persistence or projection failure cannot certify rejection.
        raise RunMessageError('message_submission_unknown',
            'Could not persist the final message outcome; inspect the same key and pinned assignment',
            submission_state='unknown') from error


def read(task: dict, args: dict, *, project_path: str) -> dict:
    repository, scope, agent_id, op_key, value = _target(task, args, project_path, sending=False)

    _validate_conversation(task, project_path)
    operation = (value.get('message_dispatches') or {}).get(op_key)
    if operation is None:
        raise RunMessageError('message_not_found', 'No message matches this stable identity and idempotency_key')
    try:
        outcome = _observe(task, repository, scope, agent_id, operation, raise_failed=False)
    except RunMessageError as error:
        if error.submission_state != 'unknown':
            raise
        outcome = {'status': 'error', 'code': error.code, 'message': str(error),
                   'submission_state': 'unknown', 'launch_lookup': operation.get('launch_lookup') or {},
                   'assignment': {
                       key: operation['assignment'][key] for key in (
                           'agent_id', 'swarm_run_id', 'conversation_id')}}
    return {**outcome, 'retained_agent_id': agent_id, 'message_id': operation['message_id'],
            'content': operation['request']['content'], 'kind': operation['request']['kind'],
            'intent': operation['request']['intent']}
