"""Amazon Q todo_list commands translate to the same flat todo snapshots."""

import copy

import pytest

from lib.tools.todo import TODO_MAX_ITEMS, apply_todo_operation, apply_todo_write_to_task

pytestmark = pytest.mark.unit


def _create(state, tasks):
    return apply_todo_operation(state, {'command': 'create', 'tasks': tasks})


def _contents(out):
    return [row['content'] for row in out['todos']]


def test_create_builds_flat_rows_without_ids_and_repeats_idempotently():
    out = _create(None, ['scan repo', 'patch', 'test'])
    assert not out['rejected'] and not out['no_op']
    assert out['todos'] == [
        {'content': text, 'status': 'pending'} for text in ['scan repo', 'patch', 'test']]
    assert out['state']['version'] == 3
    assert 'operation' not in out
    repeat = _create(out['state'], ['scan repo', 'patch', 'test'])
    assert not repeat['rejected'] and repeat['no_op']
    assert repeat['state'] == out['state']


@pytest.mark.parametrize('description', [None, 'pivot after scan'])
def test_create_replaces_unfinished_work_without_revision_gate(description):
    state = _create(None, ['old plan'])['state']
    args = {'command': 'create', 'tasks': ['new plan']}
    if description is not None:
        args['todo_list_description'] = description
    out = apply_todo_operation(state, args)
    assert not out['rejected']
    assert _contents(out) == ['new plan']
    assert not {'stack', 'history'} & set(out['state'])
    if description is not None:
        assert out['state']['explanation'] == description


@pytest.mark.parametrize('tasks', [None, [], 'not a list', [None, {}, 42]])
def test_create_without_usable_tasks_rejects_without_clearing_state(tasks):
    state = _create(None, ['keep me'])['state']
    before = copy.deepcopy(state)
    out = apply_todo_operation(state, {'command': 'create', 'tasks': tasks})
    assert out['rejected'] is True
    assert out['reason']
    assert out['state'] == before == state


def test_create_over_max_items_hits_canonical_guard():
    out = _create(None, [f'task {i}' for i in range(TODO_MAX_ITEMS + 1)])
    assert out['rejected'] is True
    assert 'at most' in out['reason']


def test_create_tolerates_dict_tasks_and_ignores_unusable_entries():
    out = _create(None, ['plain', {'description': 'from dict'}, {'nope': 1}, 42])
    assert not out['rejected']
    assert _contents(out) == ['plain', 'from dict']


def test_complete_marks_zero_based_indices_and_ignores_out_of_range():
    state = _create(None, ['a', 'b', 'c'])['state']
    before = copy.deepcopy(state)
    out = apply_todo_operation(state, {
        'command': 'complete', 'completed_indices': [0, '2', 9, -1]})
    assert not out['rejected']
    assert [row['status'] for row in out['todos']] == ['completed', 'pending', 'completed']
    assert state == before


def test_complete_without_active_list_rejects():
    out = apply_todo_operation(None, {'command': 'complete', 'completed_indices': [0]})
    assert out['rejected'] is True
    assert 'no active checklist' in out['reason']


def test_complete_all_then_add_preserves_completed_rows_and_reopens_work():
    state = _create(None, ['done'])['state']
    out = apply_todo_operation(state, {'command': 'complete', 'completed_indices': [0]})
    assert not out['rejected']
    assert out['todos'] == [{'content': 'done', 'status': 'completed'}]
    out = apply_todo_operation(out['state'], {'command': 'add', 'new_tasks': ['more']})
    assert not out['rejected']
    assert out['todos'] == [
        {'content': 'done', 'status': 'completed'},
        {'content': 'more', 'status': 'pending'}]


def test_add_inserts_in_sequence_without_generating_ids():
    state = _create(None, ['a', 'b'])['state']
    out = apply_todo_operation(state, {
        'command': 'add', 'new_tasks': ['x', 'y'], 'insert_indices': [0, 2]})
    assert not out['rejected']
    assert _contents(out) == ['x', 'a', 'y', 'b']
    assert all(set(row) == {'content', 'status'} for row in out['todos'])


def test_add_appends_without_positions_and_can_create_initial_list():
    out = apply_todo_operation(None, {'command': 'add', 'new_tasks': ['first']})
    assert not out['rejected']
    out = apply_todo_operation(out['state'], {'command': 'add', 'new_tasks': ['second']})
    assert not out['rejected']
    assert _contents(out) == ['first', 'second']


def test_add_over_limit_leaves_existing_state_intact():
    state = _create(None, [str(i) for i in range(TODO_MAX_ITEMS)])['state']
    before = copy.deepcopy(state)
    out = apply_todo_operation(state, {'command': 'add', 'new_tasks': ['overflow']})
    assert out['rejected']
    assert out['state'] == before == state


def test_remove_deletes_selected_rows_and_can_clear_all():
    state = _create(None, ['a', 'b', 'c'])['state']
    out = apply_todo_operation(state, {'command': 'remove', 'remove_indices': [0, '2', -1, 9]})
    assert not out['rejected']
    assert _contents(out) == ['b']
    out = apply_todo_operation(out['state'], {'command': 'remove', 'remove_indices': [0]})
    assert not out['rejected']
    assert out['todos'] == out['state']['todos'] == []


def test_remove_without_matching_index_rejects_and_keeps_snapshot():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(state, {'command': 'remove', 'remove_indices': [7]})
    assert out['rejected']
    assert out['state'] == state


@pytest.mark.parametrize('command', ['load', 'lookup'])
@pytest.mark.parametrize('initial_tasks', [None, ['a', 'b']])
def test_read_commands_never_increment_update_count_or_mutate_state(command, initial_tasks):
    state = _create(None, initial_tasks)['state'] if initial_tasks else None
    before = copy.deepcopy(state)
    out = apply_todo_operation(state, {'command': command})
    assert not out['rejected'] and out['no_op']
    assert out['todos'] == (state['todos'] if state else [])
    assert out['state']['update_count'] == (state['update_count'] if state else 0)
    assert out['note'].startswith(f'todo_list {command}:')
    assert state == before


def test_unknown_command_fails_closed_instead_of_clearing_state():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(state, {'command': 'restart'})
    assert out['rejected']
    assert out['reason']
    assert out['state'] == state


def test_explicit_todos_snapshot_wins_over_command():
    state = _create(None, ['a'])['state']
    out = apply_todo_operation(state, {
        'command': 'complete',
        'todos': [{'content': 'native snapshot', 'status': 'completed'}]})
    assert not out['rejected']
    assert out['todos'] == [{'content': 'native snapshot', 'status': 'completed'}]


def test_task_round_trip_updates_mirror_receipt_and_read_only_counter():
    task = {'_todos': [], '_todoState': None}
    todos, text, out = apply_todo_write_to_task(task, {'command': 'create', 'tasks': ['a', 'b']})
    assert task['_todos'] == todos == out['state']['todos']
    assert '0/2 completed' in text
    todos, text, out = apply_todo_write_to_task(task, {'command': 'complete', 'completed_indices': [0]})
    assert task['_todos'][0]['status'] == 'completed'
    assert '1/2 completed' in text
    before = copy.deepcopy({key: task[key] for key in ('_todos', '_todoState')})
    _, text, out = apply_todo_write_to_task(task, {'command': 'load'})
    assert out['no_op']
    assert text.startswith('todo_list load:')
    assert task['_todos'] == before['_todos']
    assert task['_todoState'] == before['_todoState']
    _, _, out = apply_todo_write_to_task(task, {'todos': [None]})
    assert out['rejected']
    assert task['_todos'] == before['_todos']
    assert task['_todoState'] == before['_todoState']
