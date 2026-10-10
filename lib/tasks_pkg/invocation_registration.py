"""Register conversation child lineage; headless child carriers stay storage-free.

A durable headless parent has no conversation archive. Its transient children
borrow the ordinary budget and evidence port; the parent retains domain evidence.
This exclusion does not change the parent's durability or conversation ownership.
"""


def storage_free_parent(parent: dict) -> bool:
    """An explicitly empty conversation ID is the normal headless identity."""
    return bool(parent.get('_transientRuntime') or parent.get('_invocationStorageFree')
                or parent.get('convId') == '')


def register_child_invocation(child: dict, parent: dict, *, discard_on_failure=True) -> None:
    if storage_free_parent(parent):
        return
    try:
        from lib.storage import get_storage_client
        from lib.tasks_pkg.manager import task_user_id
        identity = get_storage_client(write=True).command(
            'invocation.register_child', {
                'user_id': task_user_id(parent),
                'conversation_id': parent['convId'],
                'parent_task_id': parent['id'],
                'child_task_id': child['id'],
            }, f'invocation-register:{child["id"]}')
        child['_archiveScope'] = identity['archive_scope']
    except BaseException:
        from lib.tasks_pkg.manager import discard_task
        if discard_on_failure:discard_task(child['id'])
        raise
