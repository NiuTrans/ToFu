"""My Day goal state survives a storage restart on the real SQL authority.

A process restart reopens the same database: item IDs, manual status,
timestamps and provenance read back byte-identical, and a regeneration after
the restart neither duplicates items nor touches the owner's state.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit
OWNER = 41


def _authority(root):
    from lib.resource_control.adapters import bind_storage
    from lib.storage import StorageRuntime, StorageSupervisor
    runtime = StorageRuntime(StorageSupervisor(project_root=root, backend='sqlite'), auto_restart=False)
    runtime.start()

    def client(*, write=False):
        handle = runtime.client(write=write)
        bind_storage(handle)
        return handle
    return runtime, client


def test_goal_items_survive_a_storage_restart_and_a_later_regeneration(tmp_path):
    from lib.myday.goals import GoalStore, goal_view, merge_suggestions

    runtime, client = _authority(tmp_path)
    try:
        goals = GoalStore(OWNER, client=client)
        goal = goals.create('Ship My Day goals')
        owner_item = goals.add_item(goal['id'], 'Write the specs')['items'][-1]
        goals.update(goal['id'], lambda value: merge_suggestions(
            value, ['Build the board', 'Verify in a browser'], now_ms=goal['createdAt']))
        suggestion = next(item for item in goals.read(goal['id'])['items'] if item['origin'] == 'agent')
        goals.update_item(goal['id'], owner_item['id'], status='completed')
        goals.update_item(goal['id'], suggestion['id'], status='in_progress')
        before = goals.read(goal['id'])
    finally:
        runtime.stop()

    runtime, client = _authority(tmp_path)  # a restarted process opens the same database
    try:
        goals = GoalStore(OWNER, client=client)
        after = goals.read(goal['id'])
        assert after == before
        assert [row['id'] for row in goals.list()] == [goal['id']]
        regenerated = goals.record_decomposition(goal['id'], ['Build the board', 'Verify in a browser',
                                                             'Write the specs'])
        rows = {item['text']: item for item in regenerated['items']}
        assert len(regenerated['items']) == 3
        assert rows['Write the specs'] == before['items'][0]
        assert rows['Build the board']['status'] == 'in_progress' and rows['Build the board']['manual']
        assert rows['Build the board']['proposal'] == 'accepted'
        assert goal_view(regenerated)['progress']['percent'] == 50
        from lib.myday.goals import GoalNotFound
        with pytest.raises(GoalNotFound):
            GoalStore(OWNER + 1, client=client).read(goal['id'])
    finally:
        runtime.stop()
