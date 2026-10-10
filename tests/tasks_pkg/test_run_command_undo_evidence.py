"""Missing pre-image bytes never prove a path did not exist."""
import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('action', ['modified', 'deleted', None])
def test_missing_preimage_never_authorizes_undo_deletion(tmp_path, action):
    target = tmp_path / 'existing'
    target.write_text('keep this current file')
    assert mods._record_modification(str(tmp_path), 'run_command', 'existing',
                                    action=action, task_id='task')
    records = mods.get_modifications(str(tmp_path))
    undone, failed = mods._undo_modifications_list(str(tmp_path), records)
    assert target.read_text() == 'keep this current file'
    assert undone == []
    assert len(failed) == 1
    assert records[0]['existed'] is True


def test_explicit_creation_remains_undoable(tmp_path):
    target = tmp_path / 'new'
    target.write_text('new')
    mods._record_modification(str(tmp_path), 'run_command', 'new', action='created')
    undone, failed = mods._undo_modifications_list(str(tmp_path), mods.get_modifications(str(tmp_path)))
    assert not target.exists()
    assert len(undone) == 1 and not failed


def test_empty_preimage_is_restored(tmp_path):
    target = tmp_path / 'existing'
    target.write_text('new content')
    mods._record_modification(str(tmp_path), 'run_command', 'existing',
                              original_content=b'', action='modified')
    undone, failed = mods._undo_modifications_list(str(tmp_path), mods.get_modifications(str(tmp_path)))
    assert target.read_bytes() == b''
    assert len(undone) == 1 and not failed


def test_ambiguous_legacy_record_cannot_delete_current_file(tmp_path):
    target = tmp_path / 'existing'
    target.write_text('keep')
    undone, failed = mods._undo_modifications_list(str(tmp_path), [{
        'type': 'run_command', 'path': 'existing', 'existed': False}])
    assert target.read_text() == 'keep'
    assert not undone and len(failed) == 1


def test_real_large_file_command_cannot_be_undone_as_creation(tmp_path):
    from lib.project_mod import tools
    target = tmp_path / 'existing'
    target.write_bytes(b'x' * (101 * 1024))
    result = tools.execute_tool('run_command', {'command': 'echo after > existing'},
                                str(tmp_path), task_id='large-command')
    assert '[exit code: 0]' in result
    records = [r for r in mods.get_modifications(str(tmp_path)) if r['path'] == 'existing']
    assert len(records) == 1
    undone, failed = mods._undo_modifications_list(str(tmp_path), records)
    assert target.read_text() == 'after\n'
    assert not undone and len(failed) == 1
