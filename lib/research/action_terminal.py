"""Immutable Research action settlement facts, independent of live task memory.

persist_action_terminal is the TaskRuntime durability port, called after resource
settlement and before visible terminal status. Reads grant no replay, remote-job
completion, billing completeness, or scientific approval.
"""
from copy import deepcopy

from lib.identity import require_user_id
from lib.storage.errors import StorageError
from .action_observations import _task_prefix, _validate, _digest, _record_value
from .persistence import research_direction_hash

NAMESPACE = 'research.action_terminal.v1'


def persist_action_terminal(task, snapshot):
    from lib.storage import get_storage_client
    owner = require_user_id(task['_userId'], context='research terminal owner')
    if task.get('user_id', owner) != owner:
        raise ValueError('Research terminal task owner is inconsistent')
    _, key = _task_prefix(owner, task['id'])
    result = snapshot['result']
    error = snapshot['error'] or {}
    value = {'schema_version': 1, 'user_id': owner, 'task_id': task['id'],
        'direction_hash': research_direction_hash(task['direction']), 'lang': task['lang'],
        'action': task['action'], 'expected_revision': task['expected_revision'],
        'status': snapshot['status'],
        'committed_workspace_revision': task.get('_committedWorkspaceRevision'),
        'result_digest': _digest(result) if result is not None else None,
        'error_kind': str(error.get('kind') or error.get('code') or '')[:128],
        'finished_at': snapshot['finished_at'], 'artifact_quality': deepcopy(snapshot['artifact_quality']),
        'replay_allowed': False, 'result': deepcopy(result),
        'error': deepcopy(snapshot['error'])}
    if task.get('created_at') is not None:
        value['created_at'] = task['created_at']
    if '_researchUsageSnapshot' in task:
        value['usage_snapshot'] = deepcopy(task['_researchUsageSnapshot'])
    _validate(value, 'actionTerminal')
    client = get_storage_client(write=True)
    def exact_readback():
        stored = client.query('record.get', {'namespace': NAMESPACE, 'key': key})
        return stored is not None and _record_value(stored, 'actionTerminal') == value
    try:
        ack = client.command('record.put', {'namespace': NAMESPACE, 'key': key,
            'value': value, 'expected_version': 0}, 'research-terminal:' + _digest(value))
        if not isinstance(ack, dict) or ack.get('version') != 1:
            raise StorageError('database_internal', 'Research terminal write was not acknowledged')
    except StorageError as exc:
        if not exact_readback():
            raise exc
    if not exact_readback():
        raise StorageError('database_integrity', 'Research terminal publication was not exactly acknowledged')

    try:
        from .action_pending import retire_settled
        retire_settled(client, task_id=task['id'], user_id=owner)
    except Exception as exc:
        from lib.log import get_logger
        get_logger(__name__).warning('Research pending lookup cleanup deferred: %s', exc)


def load_action_terminal(task_id, *, user_id):
    from lib.storage import get_storage_client
    owner, key = _task_prefix(user_id, task_id)
    row = get_storage_client().query('record.get', {'namespace': NAMESPACE, 'key': key})
    if row is None:
        return {'found': False, 'terminal': None}
    value = _record_value(row, 'actionTerminal')
    if value['user_id'] != owner or value['task_id'] != task_id:
        raise StorageError('database_integrity', 'Research terminal owner/task identity is invalid')
    if 'result' in value:
        expected = _digest(value['result']) if value['result'] is not None else None
        if expected != value['result_digest']:
            raise StorageError('database_integrity', 'Research terminal result digest is invalid')
    return {'found': True, 'terminal': deepcopy(value)}


def load_public_action_task(task_id, *, user_id):
    """Owner-scoped cold task state; never adopt work or reconstruct legacy results."""
    from lib.identity import require_user_id
    owner = require_user_id(user_id, context='cold research task owner')
    try:
        _task_prefix(owner, task_id)
    except ValueError:
        return None
    retained = load_action_terminal(task_id, user_id=owner)
    if not retained['found']:
        return _load_interrupted_action_task(task_id, user_id=owner)
    value = retained['terminal']
    task = {'id':task_id, 'task_id':task_id, 'kind':'research-action',
        'status':value['status'], 'finished_at':value['finished_at'],
        'result':deepcopy(value.get('result')), 'result_available':'result' in value,
        'result_digest':value['result_digest'], 'error':deepcopy(value.get('error')),
        'artifact_quality':deepcopy(value['artifact_quality']), 'replay_allowed':False,
        'meta':{'action':value['action'],'lang':value['lang'],
                'expected_revision':value['expected_revision'],
                'committed_workspace_revision':value['committed_workspace_revision'],
                'error_kind':value['error_kind']}}
    if 'created_at' in value:
        task['created_at'] = value['created_at']
    return task


def _load_interrupted_action_task(task_id, *, user_id):
    """Expose proven executor loss without manufacturing settlement or replay."""
    from lib.storage import get_storage_client
    from .execution_owner import ActionExecutionOwners
    owners = ActionExecutionOwners.open(get_storage_client(), user_id=user_id, task_id=task_id)
    if owners is None or owners.load_basis() is None:
        return None
    state = owners.inspect_owner()
    if not state['found'] or state['process_state'] != 'dead':
        return None
    binding = owners.binding
    return {'id': task_id, 'task_id': task_id, 'kind': 'research-action',
        'status': 'interrupted', 'finished_at': None, 'result': None,
        'result_available': False, 'result_digest': None, 'error': None,
        'replay_allowed': False,
        'meta': {'lang': binding['lang'], 'expected_revision': binding['revision'],
            'interruption_reason': 'executor_process_lost',
            'settlement_recorded': False}}
