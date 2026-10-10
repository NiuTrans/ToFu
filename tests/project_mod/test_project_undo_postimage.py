"""Undo preserves edits made after the recorded task, even with reused mtimes."""
import os
import pytest

from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('kind', ['write_file', 'run_command'])
@pytest.mark.parametrize('action', ['created', 'modified', 'deleted'])
def test_newer_external_content_blocks_old_undo(tmp_path, kind, action):
    target = tmp_path / 'file'
    original = None if action == 'created' else 'before'
    if action != 'deleted':
        target.write_text('task')
    mods._record_modification(str(tmp_path), kind, 'file',
                             original_content=original, action=action, task_id='t')
    before = target.stat() if target.exists() else None
    target.write_text('user')
    if before is not None:
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    result = mods.undo_task_modifications(str(tmp_path), 't')
    assert result['undone'] == 0 and result['failed'] == 1
    assert target.read_text() == 'user'
    assert len(mods.get_modifications(str(tmp_path))) == 1


def test_multiple_changes_to_same_file_undo_in_reverse_content_order(tmp_path):
    target = tmp_path / 'file'
    target.write_text('one')
    mods._record_modification(str(tmp_path), 'write_file', 'file', original_content='zero', task_id='t')
    target.write_text('two')
    mods._record_modification(str(tmp_path), 'write_file', 'file', original_content='one', task_id='t')
    result = mods.undo_task_modifications(str(tmp_path), 't')
    assert result['undone'] == 2 and not result['failed']
    assert target.read_text() == 'zero'
    assert not mods.get_modifications(str(tmp_path))


def test_legacy_whole_file_restore_without_postimage_fails_closed(tmp_path):
    (tmp_path / 'file').write_text('user')
    undone, failed = mods._undo_modifications_list(str(tmp_path), [{
        'type':'write_file', 'path':'file', 'existed':True, 'originalContent':'old'}])
    assert not undone and len(failed) == 1
    assert (tmp_path / 'file').read_text() == 'user'


@pytest.mark.parametrize('operation', ['restore', 'delete'])
def test_external_edit_after_initial_verification_is_rechecked_at_mutation(tmp_path, monkeypatch, operation):
    from lib.project_mod import undo_filesystem
    target = tmp_path / 'file'
    target.write_text('task')
    mods._record_modification(str(tmp_path), 'write_file', 'file',
                             original_content='before' if operation == 'restore' else None,
                             task_id='t')
    name = 'write_file' if operation == 'restore' else 'remove_file'
    real_operation = getattr(undo_filesystem, name)
    def change_then_apply(base, path, *args, **kwargs):
        old = target.stat()
        target.write_text('user')
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
        return real_operation(base, path, *args, **kwargs)
    monkeypatch.setattr(undo_filesystem, name, change_then_apply)
    result = mods.undo_task_modifications(str(tmp_path), 't')
    assert result['failed'] == 1 and not result['undone']
    assert target.read_text() == 'user'
    assert len(mods.get_modifications(str(tmp_path))) == 1


def test_postimage_batch_hashing_has_one_shared_byte_budget(tmp_path):
    from lib.project_mod import undo_filesystem
    remaining = [10]
    target = tmp_path / 'file'
    target.write_bytes(b'12345')
    first = undo_filesystem.fingerprint_file(str(tmp_path), str(target), byte_budget=remaining)
    assert first['kind'] == 'content'
    assert remaining == [4]
    second = undo_filesystem.fingerprint_file(str(tmp_path), str(target), byte_budget=remaining)
    assert second == {'kind': 'unavailable'}
    assert remaining == [4]


def test_persisted_postimage_schema_rejects_boolean_sizes_and_unknown_fields(tmp_path):
    from lib.project_mod import undo_filesystem
    target = tmp_path / 'file'
    target.write_text('x')
    receipt = undo_filesystem.fingerprint_file(str(tmp_path), str(target))
    assert undo_filesystem.valid_postimage(receipt)
    assert not undo_filesystem.valid_postimage({**receipt, 'bytes': True})
    assert not undo_filesystem.valid_postimage({**receipt, 'unexpected': 'field'})
    assert undo_filesystem.valid_postimage({'kind': 'unavailable'})
