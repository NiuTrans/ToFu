"""Register conversation child lineage; headless child carriers stay storage-free.

A durable headless parent has no conversation archive. Its transient children
borrow the ordinary budget and evidence port; the parent retains domain evidence.
This exclusion does not change the parent's durability or conversation ownership.
"""

INVOCATION_COMPLETION_FIELD = 'flow_invocation_completion'


def storage_free_parent(parent: dict) -> bool:
    """An explicitly empty conversation ID is the normal headless identity."""
    from lib.tasks_pkg.invocation import is_child_invocation
    if is_child_invocation(parent):
        # Child runtime carriers are transient even when their registered
        # conversation lineage is durable. Descendants inherit that explicit
        # storage policy; they must not lose it because of the carrier flag.
        return parent.get('_invocationStorageFree') is not False or parent.get('convId') == ''
    return bool(parent.get('_transientRuntime') or parent.get('_invocationStorageFree')
                or parent.get('convId') == '')


def register_child_invocation(child: dict, parent: dict, *, discard_on_failure=True) -> None:
    if storage_free_parent(parent):
        return
    try:
        from lib.storage import get_storage_client
        from lib.tasks_pkg.manager import task_user_id
        from lib.tasks_pkg.persistence_wait import wait_for_persistence
        payload = {
            'user_id': task_user_id(parent),
            'conversation_id': parent['convId'],
            'parent_task_id': parent['id'],
            'child_task_id': child['id'],
        }
        identity = wait_for_persistence(child, lambda: get_storage_client(write=True).command(
            'invocation.register_child', payload, f'invocation-register:{child["id"]}'),
            label='invocation registration')
        child['_archiveScope'] = identity['archive_scope']
        import threading
        child['_invocationEffectLock'] = threading.RLock()
    except BaseException:
        from lib.tasks_pkg.manager import discard_task
        if discard_on_failure:discard_task(child['id'])
        raise


def registered_invocation_id(child: dict) -> str | None:
    """Return the selected authority's exact child locator before Flow admission.

    A transient carrier can have durable registered lineage. A storage-free
    child has no recovery locator; neither a scope string nor a mutable task ID
    proves registration by itself.
    """
    if storage_free_parent(child):
        return None
    from lib.storage import get_storage_client
    from lib.tasks_pkg.manager import task_user_id
    from lib.tasks_pkg.persistence_wait import wait_for_persistence
    owner = task_user_id(child)
    scope = child.get('_archiveScope')
    if not scope:
        raise ValueError('Flow child registration was not acknowledged')
    identity = wait_for_persistence(child, lambda: get_storage_client().query(
        'invocation.resolve_child', {'user_id': owner, 'archive_scope': scope}),
        label='Flow child registration lookup')
    expected = dict(user_id=owner, conversation_id=child['convId'],
        parent_task_id=child['_parentTaskId'], child_task_id=child['id'], archive_scope=scope)
    if identity != expected:
        raise ValueError('Flow child registration identity changed')
    return identity['child_task_id']


def owned_child_record(client, *, user_id, conversation_id, invocation_id):
    from lib.conversation_sync.service import ConversationSyncNotFound
    expected = dict(user_id=user_id, conversation_id=conversation_id,
                    child_task_id=invocation_id)
    scope = f'invocation:{user_id}:{invocation_id}'
    record = client.query('record.get', {
        'namespace': 'task_results', 'key': invocation_id, 'user_id': user_id})
    value = (record or {}).get('value') or {}
    # Registration is the only authority allowed to introduce this identity.
    # Use that exact persisted birth, not the syntactic archive scope. Reading
    # resolve_child as well would hydrate the cumulative record a second time.
    identity = value.get('child_invocation') or {}
    if (any(identity.get(key) != item for key, item in expected.items())
            or identity.get('archive_scope') != scope or not identity.get('parent_task_id')
            or value.get('task_id') != invocation_id
            or value.get('user_id') != user_id or value.get('conv_id') != conversation_id):
        raise ConversationSyncNotFound('Child invocation not found')
    if not client.query('conversation.get', {
            'conv_id': conversation_id, 'user_id': user_id, 'derive_messages': False}):
        raise ConversationSyncNotFound('Child invocation not found')
    return record
