"""Model-facing run messages. Session lookup stays with Swarm integration.

This service checks the caller and exact target; repository owns durable CAS.
A body cannot set owner, parent, generation, provenance or tool permissions.
"""
from __future__ import annotations

from collections.abc import Callable
from lib.swarm.message_contract import NAMESPACE
from lib.swarm.run_messages import (
    RunMessageError, RunMessageRepository, binding_for, mailbox_key, validate_target,
    validate_message,
)
from lib.tasks_pkg.manager import task_user_id


def _caller(task: dict) -> tuple[int, str, str]:
    from lib.swarm.team_messages import worker_binding
    worker = worker_binding(task)
    if not isinstance(task.get('id'), str) or not task['id']:
        raise RunMessageError('message_forbidden', 'A current parent task identity is required')
    if (task.get('_transientRuntime') and worker is None) or task.get('_invocationStorageFree'):
        raise RunMessageError('durable_storage_required', 'Messaging requires durable application storage')
    try:
        owner = task_user_id(task)
    except (ValueError, PermissionError) as error:
        raise RunMessageError('message_forbidden', 'The current caller identity is invalid') from error
    return owner, str(task.get('_tenant_id') or ''), str(worker['conversationId'] if worker else task.get('convId') or '')


def _arguments(args: dict, *, sending: bool) -> tuple[str, str]:
    fields = {'agent_id', 'swarm_run_id', 'content', 'kind', 'idempotency_key'} if sending else {
        'agent_id', 'swarm_run_id', 'message_id'}
    if not isinstance(args, dict):
        raise RunMessageError('message_invalid_arguments', 'Unknown message fields')
    extras = set(args) - fields
    if extras:
        raise RunMessageError('message_invalid_arguments',
            'Unknown exact-run message fields: ' + ', '.join(sorted(extras))
            + '. Exact-run messages omit retained_agent_id, scope and intent.')
    for key in ('agent_id', 'swarm_run_id'):
        if not isinstance(args.get(key), str) or not 1 <= len(args[key]) <= 256:
            raise RunMessageError('message_invalid_identity', f'{key} is required')
    return args['agent_id'], args['swarm_run_id']


def existing_receipt(task: dict, args: dict) -> dict | None:
    """An exact durable retry is evidence lookup, not another execution."""
    import uuid
    owner, tenant, conversation_id = _caller(task)
    agent_id, run_id = _arguments(args, sending=True)
    validate_message(content=args.get('content'), kind=args.get('kind', 'reference'),
                     key=args.get('idempotency_key'))
    key = mailbox_key(owner, tenant, conversation_id, run_id, agent_id)
    from lib.swarm.team_messages import message_key
    message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key + ':' + message_key(task, args['idempotency_key'])))
    try:
        history = read(task, {'agent_id': agent_id, 'swarm_run_id': run_id,
                             'message_id': message_id})
    except RunMessageError as error:
        if error.code not in {'message_run_not_found', 'message_not_found'}:
            raise
        return None
    item = history['messages'][0]
    if item['content'] != args['content'] or item['kind'] != args.get('kind', 'reference'):
        raise RunMessageError('message_idempotency_conflict', 'The same idempotency_key has different content')
    return {'agent_id': agent_id, 'swarm_run_id': run_id, 'message_id': message_id,
            'state': item['state'], 'kind': item['kind'], 'contentChars': item['contentChars'],
            'execution_started': False}


def send(task: dict, args: dict, session, *,
         abort_check: Callable[[], bool] | None = None) -> dict:
    owner, tenant, conversation_id = _caller(task)
    agent_id, run_id = _arguments(args, sending=True)
    def caller_active():
        if (task.get('aborted')
                or (task.get('abort_event') is not None and task['abort_event'].is_set())
                or (abort_check is not None and abort_check())):
            raise RunMessageError('message_run_ended', 'The current caller is stopped; sending cannot wake it')
    caller_active()
    prior = existing_receipt(task, args)
    if prior is not None:
        return prior
    from lib.swarm.team_messages import validate_worker, coordinator_binding, message_key
    worker = validate_worker(task, run_id, session)
    if (session is None or session.task_id != run_id or session.user_id != owner
            or session.conv_id != conversation_id
            or str(session._parent_task_proxy.get('_tenant_id') or '') != tenant
            or (not conversation_id and not worker and task['id'] != run_id)):
        raise RunMessageError('message_run_not_found', 'No owned current run matches this exact handle')
    if callable(getattr(session, 'abort_check', None)) and session.abort_check():
        raise RunMessageError('message_run_ended', 'Execution cancellation authority rejected this run')
    if task.get('aborted') or session.is_terminated or session._aborted or session._restart_suspended:
        raise RunMessageError('message_run_ended', 'The run is stopped, suspended or ended; sending cannot wake it')
    with session._lock:
        spec = next((item for item in session.specs if item.id == agent_id), None)
        terminal = agent_id in session._results_by_id

        agent = getattr(session, '_agents', {}).get(agent_id)
        child = getattr(agent, '_child', None)
        if child is not None and (child.get('aborted') or
                (child.get('abort_event') is not None and child['abort_event'].is_set())):
            terminal = True
    coordinator = worker is not None and agent_id == run_id
    if coordinator:
        from lib.tasks_pkg.manager.runtime import chat_task_runtime
        parent = chat_task_runtime.get(run_id)
        if parent is None or parent.get('status') not in {'running', 'pending'} or parent.get('aborted'):
            raise RunMessageError('message_target_not_active', 'Coordinator is not running; messages cannot wake it')
    if (spec is None and not coordinator) or terminal:
        raise RunMessageError('message_target_not_active', 'Target is absent or already settled; use a new explicit assignment')
    binding = (coordinator_binding(session) if coordinator else
               binding_for(session._parent_task_proxy, spec, session.inbox_key))
    validate_target(binding)
    repository = RunMessageRepository(binding)
    def active():
        validate_target(binding)
        # Recheck the live caller after scope validation and before mailbox CAS.
        # Stable routing may pass a copied task, so preserve its original check.
        caller_active()
        validate_worker(task, run_id, session)
        if coordinator and (parent.get('aborted') or parent.get('status') not in {'running', 'pending'}):
            raise RunMessageError('message_run_ended', 'Coordinator stopped during submission')
        if ((callable(getattr(session, 'abort_check', None)) and session.abort_check())
                or session.is_terminated or session._aborted or session._restart_suspended):
            raise RunMessageError('message_run_ended', 'Target run stopped during submission')
        with session._lock:
            ended = agent_id in session._results_by_id
            live_agent = getattr(session, '_agents', {}).get(agent_id)
            live_child = getattr(live_agent, '_child', None)
            stopped = live_child is not None and (
                live_child.get('aborted')
                or (live_child.get('abort_event') is not None
                    and live_child['abort_event'].is_set()))
        if ended or stopped:
            raise RunMessageError('message_run_ended', 'Child settled or was stopped during submission')
    item = repository.submit(
        content=args.get('content'), kind=args.get('kind', 'reference'),
        key=message_key(task, args.get('idempotency_key')), sender_task_id=task['id'], before_commit=active)
    # submit returned durable evidence. A later Stop, scope revocation or read
    # failure cannot turn that commit into a rejection. Do not close the target
    # mailbox on the caller's behalf or bypass scoped read ownership; use only
    # evidence already saved by this invocation. Exact-run termination remains
    # responsible for closing its mailbox.
    saved_receipt = None
    try:
        saved_receipt = {
            'agent_id': agent_id, 'swarm_run_id': run_id, 'message_id': item['id'],
            'state': item['state'], 'kind': item['kind'], 'contentChars': len(item['content']),
            'execution_started': False,
        }
        active()
    except Exception as error:
        if saved_receipt is not None:
            return saved_receipt
        raise RunMessageError('message_submission_unknown', 'Reconcile the exact message receipt',
                              submission_state='unknown') from error
    return saved_receipt


def _uncreated_mailbox_binding(task: dict, agent_id: str, run_id: str,
                               worker: dict | None) -> dict:
    """Resolve lazy inbox identity from execution evidence, never caller text."""
    from types import SimpleNamespace
    from lib.swarm.integration import _get_session
    from lib.swarm.persistence import load_session
    from lib.swarm.types import SubTaskSpec

    owner, tenant, conversation_id = _caller(task)
    key = worker['swarmKey'] if worker else (conversation_id or task['id'])
    session = _get_session(key)
    if (session is not None and session.task_id == run_id
            and session.user_id == owner and session.conv_id == conversation_id
            and str(session._parent_task_proxy.get('_tenant_id') or '') == tenant):
        with session._lock:
            spec = next((item for item in session.specs if item.id == agent_id), None)
        parent = session._parent_task_proxy
    else:
        row = load_session(key, required=True)
        config = (row or {}).get('config') or {}
        parent_cfg = config.get('parent_cfg') or {}
        if (row is None or row.get('task_id') != run_id
                or row.get('conv_id', '') != conversation_id
                or config.get('user_id') != owner
                or str(parent_cfg.get('_tenant_id') or '') != tenant):
            raise RunMessageError('message_run_not_found', 'No owned original run matches this exact handle')
        saved_spec = next((item for item in row.get('specs') or []
                           if item.get('id') == agent_id), None)
        spec = SubTaskSpec.from_dict(saved_spec) if saved_spec is not None else None
        parent = {'id': run_id, 'convId': conversation_id, '_userId': owner,
                  '_tenant_id': tenant, 'config': parent_cfg}
    if agent_id == run_id:
        spec = SimpleNamespace(id=run_id, retained_agent_id='', retained_run_id='', retained_scope='')
    if spec is None:
        raise RunMessageError('message_run_not_found', 'No recipient in the owned original run matches this exact handle')
    return binding_for(parent, spec, key)


def read(task: dict, args: dict) -> dict:
    owner, tenant, conversation_id = _caller(task)
    agent_id, run_id = _arguments(args, sending=False)
    from lib.swarm.team_messages import validate_worker
    worker = validate_worker(task, run_id)
    if not conversation_id and not worker and task['id'] != run_id:
        raise RunMessageError('message_forbidden', 'Unbound runs are visible only to their spawning task')
    from lib.storage import get_storage_client
    row = get_storage_client().query('record.get', {
        'namespace': NAMESPACE, 'key': mailbox_key(owner, tenant, conversation_id, run_id, agent_id)})
    payload = row.get('value') if isinstance(row, dict) else None
    binding = payload.get('binding') if isinstance(payload, dict) else None
    if row is None:
        binding = _uncreated_mailbox_binding(task, agent_id, run_id, worker)
    elif not isinstance(binding, dict):
        from lib.storage.errors import StorageError
        raise StorageError('database_integrity', 'Invalid run message mailbox')
    if (binding.get('owner'), binding.get('tenant'), binding.get('conversationId'),
            binding.get('swarmRunId'), binding.get('agentId')) != (owner, tenant, conversation_id, run_id, agent_id):
        raise RunMessageError('message_forbidden', 'Mailbox ownership does not match')
    validate_target(binding, active_retained=False)
    repository = RunMessageRepository(binding)
    from lib.swarm.persistence import load_session
    execution = load_session(binding['swarmKey'], required=True)
    ended = False
    if execution is not None and execution.get('task_id') == run_id:
        ended = execution.get('status') in {'terminated', 'quarantined', 'cancelled'} or any(
            item.get('agent_id') == agent_id and item.get('status') not in {'running', 'pending', 'interrupted'}
            for item in execution.get('agents') or [])
    if agent_id == run_id:
        from lib.swarm.team_messages import validate_coordinator
        try:
            validate_coordinator(binding)
            ended = False
        except RunMessageError:
            ended = True
    value, _ = repository.load()
    if ended:
        value['closed'] = True
        for item in value['messages']:
            if item['state'] == 'submitted':
                item['state'] = 'unconfirmed'
    message_id = args.get('message_id')
    if message_id is not None and (not isinstance(message_id, str) or not 1 <= len(message_id) <= 128):
        raise RunMessageError('message_invalid_identity', 'Invalid message_id')
    items = value['messages']
    if message_id:
        items = [item for item in items if item['id'] == message_id]
        if not items:
            raise RunMessageError('message_not_found', 'No message matches this exact ID')
    return {
        'agent_id': agent_id, 'swarm_run_id': run_id, 'run_closed': value['closed'],
        'messages': [{**{key: item[key] for key in (
            'id', 'kind', 'state', 'senderTaskId', 'submittedAt', 'inputTaskId',
            'inputRound', 'inputInjectedAt', 'inputInjectedRound', 'inputConfirmedAt') if key in item},
            'content': item['content'] if message_id else item['content'][:256],
            'contentChars': len(item['content']),
            'contentTruncated': not bool(message_id) and len(item['content']) > 256,
        } for item in items],
    }
