"""A turn's file undo settles as undone only when its files were restored.

Redo of an undone turn rewrites the turn's files unconditionally, so an undo
that restored nothing (for example because the user edited the file after the
turn) must stay visibly applied instead of offering a redo that would destroy
the newer edit.

Only the file-history engine's typed refusals are the user's answer
(``BadRequest``). An engine fault (history unavailable, an unreadable journal,
a failed restore) is a ``FileHistoryFault``: never a ``ValueError``, and its
message, which the turn records as ``fileChanges.error``, carries no raw
exception text.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import uuid

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def turn(tmp_path, monkeypatch):
    from lib.project_mod import config as pm_config

    monkeypatch.setenv('TOFU_FILE_HISTORY', '1')
    root = tmp_path / 'project'
    root.mkdir()
    identity = {
        'root': root,
        'conversation': f'conv-undo-outcome-{uuid.uuid4().hex}',
        'task': f'task-undo-outcome-{uuid.uuid4().hex}',
    }
    with pm_config._lock:
        pm_config._roots.clear()
        pm_config._roots['project'] = pm_config._make_root_state(str(root))
        pm_config._state['path'] = str(root)
    pm_config.set_conv_roots(identity['conversation'], str(root))
    try:
        yield identity
    finally:
        with pm_config._lock:
            pm_config._roots.clear()
            pm_config._state['path'] = ''


def _assistant_round(turn, contents):
    from lib import file_history
    from lib.project_mod.write_tools import tool_write_file

    root = turn['root']
    for path, (original, written) in contents.items():
        (root / path).write_text(original)
        result = tool_write_file(str(root), path, written,
                                 conv_id=turn['conversation'], task_id=turn['task'])
        assert result['ok'], result
    assert file_history.make_snapshot(
        str(root), task_id=turn['task'], conv_id=turn['conversation'],
        rel_paths=list(contents))


def _turn_store(monkeypatch, turn, paths, *, state='applied', task=None):
    import lib.conversation_sync.command_service as module
    from lib.turn_lifecycle import LifecycleConflict

    source_state = state
    state = {'turn': {
        'conversationId': turn['conversation'],
        'turnId': 'turn-a',
        'projectionRevision': 3,
        'projection': {'content': 'done', 'fileChanges': {
            'taskId': task or turn['task'],
            'count': len(paths),
            'state': source_state,
            'files': [{'path': path, 'action': 'written', 'source': 'local'}
                      for path in paths],
        }},
    }}

    def get_turn(_conversation_id, _turn_id, *, user_id):
        return deepcopy(state['turn'])

    def update_turn(_conversation_id, _turn_id, *, projection,
                    expected_projection_revision, user_id):
        if expected_projection_revision != state['turn']['projectionRevision']:
            raise LifecycleConflict('stale_projection', 'stale', deepcopy(state['turn']))
        state['turn']['projection'] = deepcopy(projection)
        state['turn']['projectionRevision'] += 1
        return {'conversationRevision': 1, 'turn': deepcopy(state['turn'])}

    monkeypatch.setattr(module, 'get_turn', get_turn)
    monkeypatch.setattr(module, 'update_turn_projection', update_turn)
    monkeypatch.setattr(module, 'get_conversation_revision',
                        lambda _conversation_id, *, user_id: 1)
    return state


def _command(turn, state, operation, command_id):
    from lib.conversation_sync.runtime import conversation_turn_commands

    return conversation_turn_commands.mutate_turn_file_changes(
        turn['conversation'], 'turn-a', 1,
        {'commandId': command_id,
         'expectedProjectionRevision': state['turn']['projectionRevision']},
        operation=operation)


def test_refused_undo_keeps_turn_applied_and_redo_cannot_clobber_user_edit(
        turn, monkeypatch):
    notes = turn['root'] / 'notes.txt'
    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    notes.write_text('user edit after the turn\n')
    state = _turn_store(monkeypatch, turn, ['notes.txt'])

    with pytest.raises(ValueError, match='File changed after the recorded modification'):
        _command(turn, state, 'undo', 'undo-1')
    block = state['turn']['projection']['fileChanges']
    assert block['state'] == 'applied'
    assert block['error']

    with pytest.raises(ValueError, match='cannot redo'):
        _command(turn, state, 'redo', 'redo-1')
    assert notes.read_text() == 'user edit after the turn\n'


def test_partially_refused_undo_is_not_settled_as_undone(turn, monkeypatch):
    _assistant_round(turn, {
        'kept.txt': ('kept original\n', 'kept assistant\n'),
        'edited.txt': ('edited original\n', 'edited assistant\n'),
    })
    (turn['root'] / 'edited.txt').write_text('user edit after the turn\n')
    state = _turn_store(monkeypatch, turn, ['kept.txt', 'edited.txt'])

    with pytest.raises(ValueError, match='edited.txt'):
        _command(turn, state, 'undo', 'undo-1')

    assert state['turn']['projection']['fileChanges']['state'] == 'applied'
    assert (turn['root'] / 'kept.txt').read_text() == 'kept original\n'
    assert (turn['root'] / 'edited.txt').read_text() == 'user edit after the turn\n'


def test_undo_after_redo_is_refused_instead_of_claiming_a_restore(turn, monkeypatch):
    notes = turn['root'] / 'notes.txt'
    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    state = _turn_store(monkeypatch, turn, ['notes.txt'])
    _command(turn, state, 'undo', 'undo-1')
    assert notes.read_text() == 'original\n'
    _command(turn, state, 'redo', 'redo-1')
    assert notes.read_text() == 'assistant edit\n'

    with pytest.raises(ValueError, match='No recorded file changes'):
        _command(turn, state, 'undo', 'undo-2')

    assert state['turn']['projection']['fileChanges']['state'] == 'applied'
    assert notes.read_text() == 'assistant edit\n'


def _pin_project(monkeypatch, turn):
    """Resolve the turn's project without the history index or file history."""
    import lib.project_mod as project_mod

    monkeypatch.setattr(project_mod, 'resolve_base_path',
                        lambda **_identity: str(turn['root']))


def _server_fault(caught, *withheld):
    """Return the fault's message after checking it is a typed server fault."""
    error = caught.value
    # BadRequest, the only 400, is a ValueError; a server fault must not be.
    assert not isinstance(error, ValueError), repr(error)
    from lib.project_mod.modifications import FileHistoryFault

    assert isinstance(error, FileHistoryFault), repr(error)
    for text in withheld:
        assert text not in str(error)
    return str(error)


@pytest.mark.parametrize(('scenario', 'operation', 'source_state', 'message'), [
    ('changed', 'undo', 'applied',
     'Could not undo notes.txt: File changed after the recorded modification'),
    ('disabled', 'redo', 'undone', 'File-history disabled (TOFU_FILE_HISTORY=0)'),
    ('never_committed', 'redo', 'undone',
     'No snapshot found for this task (was it ever committed?)'),
])
def test_engine_refusal_is_a_bad_request_with_its_authored_message(
        turn, monkeypatch, scenario, operation, source_state, message):
    from lib.error_envelope import BadRequest

    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    task = None
    if scenario == 'changed':
        (turn['root'] / 'notes.txt').write_text('user edit after the turn\n')
    elif scenario == 'disabled':
        # Disabled history also hides the snapshot that locates the project.
        _pin_project(monkeypatch, turn)
        monkeypatch.setenv('TOFU_FILE_HISTORY', '0')
    else:
        task = f'task-never-committed-{uuid.uuid4().hex}'
    state = _turn_store(monkeypatch, turn, ['notes.txt'], state=source_state, task=task)

    with pytest.raises(BadRequest) as caught:
        _command(turn, state, operation, f'{operation}-refused')

    assert str(caught.value) == message
    block = state['turn']['projection']['fileChanges']
    assert block['state'] == source_state
    assert block['error'] == message


def test_unavailable_file_history_is_a_redacted_server_fault(turn, monkeypatch):
    import sys

    import lib

    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    state = _turn_store(monkeypatch, turn, ['notes.txt'], state='undone')
    _pin_project(monkeypatch, turn)
    monkeypatch.delattr(lib, 'file_history')
    monkeypatch.setitem(sys.modules, 'lib.file_history', None)

    with pytest.raises(Exception) as caught:
        _command(turn, state, 'redo', 'redo-unavailable')

    message = _server_fault(caught, 'sys.modules', 'lib.file_history')
    block = state['turn']['projection']['fileChanges']
    assert block['state'] == 'undone'
    assert block['error'] == message


def test_unreadable_change_journal_is_a_redacted_server_fault(turn, monkeypatch):
    from lib.project_mod import modifications as mods

    notes = turn['root'] / 'notes.txt'
    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    state = _turn_store(monkeypatch, turn, ['notes.txt'])
    index = Path(mods._get_session_dir(str(turn['root']))) / 'modifications.json'
    committed = index.read_bytes()
    index.write_text('{"modifications": [')
    try:
        with pytest.raises(Exception) as caught:
            _command(turn, state, 'undo', 'undo-journal')
    finally:
        index.write_bytes(committed)

    message = _server_fault(caught, 'Expecting', 'line 1 column')
    assert message == "Could not undo the turn's file changes: internal error (JSONDecodeError)"
    block = state['turn']['projection']['fileChanges']
    assert block['state'] == 'applied'
    assert block['error'] == message
    assert notes.read_text() == 'assistant edit\n'


def test_redo_restore_crash_is_a_redacted_server_fault(turn, monkeypatch):
    from lib.file_history import api as fh_api

    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    state = _turn_store(monkeypatch, turn, ['notes.txt'])
    _command(turn, state, 'undo', 'undo-1')
    withheld = '/srv/private/file-history/tracked.json'

    def fail(*_args, **_kwargs):
        raise OSError(28, 'No space left on device', withheld)

    monkeypatch.setattr(fh_api, 'save_tracked', fail)
    with pytest.raises(Exception) as caught:
        _command(turn, state, 'redo', 'redo-crash')

    message = _server_fault(caught, withheld, 'No space left')
    block = state['turn']['projection']['fileChanges']
    assert block['state'] == 'undone'
    assert block['error'] == message


@pytest.mark.parametrize('operation', ['undo', 'redo'])
def test_per_file_fault_reason_is_withheld(turn, monkeypatch, operation):
    from lib.project_mod import undo_filesystem

    notes = turn['root'] / 'notes.txt'
    _assistant_round(turn, {'notes.txt': ('original\n', 'assistant edit\n')})
    state = _turn_store(monkeypatch, turn, ['notes.txt'])
    if operation == 'redo':
        _command(turn, state, 'undo', 'undo-1')
    before = notes.read_text()
    source_state = state['turn']['projection']['fileChanges']['state']
    withheld = '/srv/private/project/notes.txt'

    def deny(*_args, **_kwargs):
        raise PermissionError(13, 'Permission denied', withheld)

    monkeypatch.setattr(undo_filesystem, 'write_file', deny)
    with pytest.raises(Exception) as caught:
        _command(turn, state, operation, f'{operation}-denied')

    message = _server_fault(caught, withheld, 'Permission denied')
    assert message == f'Could not {operation} notes.txt: internal error (PermissionError)'
    block = state['turn']['projection']['fileChanges']
    assert block['state'] == source_state
    assert block['error'] == message
    assert notes.read_text() == before
