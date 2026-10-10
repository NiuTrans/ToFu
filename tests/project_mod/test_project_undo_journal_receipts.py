"""Undo only consumes successful receipts from its original selection."""
import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


def _undo(base, scope):
    if scope == 'task':
        return mods.undo_task_modifications(base, 'task')
    if scope == 'conversation':
        return mods.undo_conv_modifications(base, 'conv')
    return mods.undo_all_modifications(base)


@pytest.mark.parametrize('scope', ['task', 'conversation', 'all'])
def test_failed_undo_retains_its_journal_record(tmp_path, scope):
    (tmp_path / 'existing').write_text('keep')
    mods._record_modification(str(tmp_path), 'run_command', 'existing',
                             task_id='task', conv_id='conv', action='modified')
    result = _undo(str(tmp_path), scope)
    assert result['failed'] == 1
    assert len(mods.get_modifications(str(tmp_path))) == 1
    assert (tmp_path / 'existing').read_text() == 'keep'


@pytest.mark.parametrize('scope', ['task', 'conversation', 'all'])
def test_concurrent_append_is_not_consumed_by_undo(tmp_path, monkeypatch, scope):
    (tmp_path / 'created').write_text('created')
    mods._record_modification(str(tmp_path), 'write_file', 'created',
                             task_id='task', conv_id='conv')
    from lib.project_mod import undo_filesystem
    remove = undo_filesystem.remove_file
    def remove_and_append(base, path, **kwargs):
        result = remove(base, path, **kwargs)
        if str(path) == str(tmp_path / 'created'):
            (tmp_path / 'newer').write_text('newer')
            mods._record_modification(str(tmp_path), 'write_file', 'newer',
                                     task_id='task', conv_id='conv')
        return result
    monkeypatch.setattr(undo_filesystem, 'remove_file', remove_and_append)
    result = _undo(str(tmp_path), scope)
    assert result['undone'] == 1 and not result['failed']
    assert [r['path'] for r in mods.get_modifications(str(tmp_path))] == ['newer']
    assert (tmp_path / 'newer').read_text() == 'newer'


def test_failed_latest_change_blocks_older_undo_of_same_path(tmp_path):
    (tmp_path / 'file').write_text('external')
    mods._record_modification(str(tmp_path), 'write_file', 'file', original_content='old', task_id='task')
    mods._record_modification(str(tmp_path), 'apply_diff', 'file',
                             reverse_patch={'search':'missing','replace':'previous'}, task_id='task')
    result = mods.undo_task_modifications(str(tmp_path), 'task')
    assert result['failed'] == 2 and not result['undone']
    assert (tmp_path / 'file').read_text() == 'external'
    assert len(mods.get_modifications(str(tmp_path))) == 2
