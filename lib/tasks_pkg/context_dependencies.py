"""Bind admitted working-fact versions to the common model request lifecycle.

Preparation persists before dispatch. Final-wire observation is content-only;
verified provider return confirms that same binding. Neither composer reads nor
Inspector snapshots write facts. Uncertain confirmations remain durable unknowns.
"""
from copy import deepcopy


def _scope(task):
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.manager import task_user_id
    try:
        principal = PrincipalContext.from_payload(task['_principalContext'])
        matching_owner = principal.require_owner(context='working fact consumer') == task_user_id(task)
    except (KeyError, TypeError, ValueError) as error:
        raise PermissionError('Working fact consumer ownership differs') from error
    if not matching_owner:
        raise PermissionError('Working fact consumer ownership differs')
    return {'principal': principal, 'conversation_id': task['convId'], 'task_id': task['id'],
            'turn_id': task.get('_turnId') or '', 'attempt_id': task.get('_attemptId') or ''}


def prepare(task, messages):
    """Only the actual, fully admitted composer block can declare dependencies."""
    task.pop('_workingFactDependencyPending', None)
    if task.get('_transientRuntime') or task.get('aborted'):
        return
    rows = [row for row in task.get('_contextManifest', []) if row.get('id') == 'working_facts'
            and row.get('injected') and (row.get('appended') or row.get('reused'))
            and 'truncated' not in str(row.get('reason') or '')]
    if not rows:
        return
    from lib.tasks_pkg.context_composer import verified_block_hash
    existing = task.get('_managedBlockHashes') or {}
    existing_map = (existing.get('map')
                    if existing.get('_messages_identity') == id(messages)
                    and existing.get('_messages_length') == len(messages)
                    else None)
    actual = verified_block_hash(messages, 'working_facts',
                                 existing_hashes=existing_map)
    row = next((row for row in reversed(rows) if row.get('hash') == actual), None)
    if row is None or not row.get('provenance', {}).get('facts'):
        return
    from lib.memory.working_facts import consumers
    from lib.memory.working_facts.repository import digest
    scope = _scope(task)
    provenance = row['provenance']
    if (provenance.get('conversationId'), provenance.get('taskId')) != (scope['conversation_id'], scope['task_id']):
        raise PermissionError('Working fact context ownership differs')
    facts = [{'id': value['id'], 'revision': value['revision']} for value in provenance['facts']]
    binding = digest([scope['principal'].to_payload(), scope['conversation_id'], scope['task_id'],
                      scope['turn_id'], scope['attempt_id'], sorted(facts, key=lambda value: value['id'])])
    if task.get('_workingFactDependencyConfirmed') == binding:
        return
    consumers.prepare(**scope, facts=facts)
    task['_workingFactDependencyPending'] = {'scope': scope, 'facts': deepcopy(facts),
        'binding': binding, 'hash': actual, 'wireMatched': False}


def observe_wire(task, messages):
    pending = task.get('_workingFactDependencyPending')
    if pending is not None:
        if messages is not None and _scope(task) != pending['scope']:
            raise PermissionError('Working fact consumer ownership changed')
        from lib.tasks_pkg.context_composer import verified_block_hash
        pending['wireMatched'] = verified_block_hash(messages, 'working_facts', wire=True) == pending['hash']


def confirm(task):
    """Caller supplies the existing verified provider-completion boundary."""
    pending = task.get('_workingFactDependencyPending')
    if (pending is None or not pending['wireMatched'] or task.get('aborted')
            or (task.get('abort_event') and task['abort_event'].is_set())):
        return
    if _scope(task) != pending['scope']:
        raise PermissionError('Working fact consumer ownership changed')
    from lib.memory.working_facts import consumers
    from lib.memory.working_facts.repository import FactConflict
    from lib.storage import StorageError
    try:
        consumers.confirm(pending['scope']['conversation_id'], principal=pending['scope']['principal'],
                          task_id=pending['scope']['task_id'], facts=pending['facts'])
    except (StorageError, FactConflict):
        # Prepared evidence still exists. Never turn a missing acknowledgement
        # into a confirmed dependency, nor rerun a successful provider call.
        return
    task['_workingFactDependencyConfirmed'] = pending['binding']
    task.pop('_workingFactDependencyPending', None)
