"""One physical batch replacement must have one verifiable undo transition."""
import pytest
from lib.project_mod.write_tools import _ops
from lib.project_mod import modifications as mods

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('entry', ['edit_file', 'apply_diffs'])
@pytest.mark.parametrize('history_enabled', [False, True])
def test_batch_roundtrip_has_one_physical_undo_record(tmp_path, monkeypatch, entry, history_enabled):
    import lib.file_history as history
    if not history_enabled:
        monkeypatch.setattr(history, 'is_enabled', lambda: False)
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    base = tmp_path / 'project'
    base.mkdir()
    target = base / 'file.txt'
    original = b'A\nC\n'
    target.write_bytes(original)
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, root, rel, **kw: (root, rel))
    if entry == 'edit_file':
        result = _ops.tool_edit_file(str(base), [
            {'path':'file.txt', 'operation':'replace', 'anchor':'A', 'content':'A\nB'},
            {'path':'file.txt', 'operation':'replace', 'anchor':'B', 'content':'B\nD'},
        ], task_id='task')
    else:
        result = _ops.tool_apply_diffs(str(base), [
            {'path':'file.txt', 'search':'A', 'replace':'A\nB'},
            {'path':'file.txt', 'search':'B', 'replace':'B\nD'},
        ], task_id='task')
    assert result.startswith('Applied 2/2'), result
    assert target.read_bytes() == b'A\nB\nD\nC\n'
    undone = mods.undo_task_modifications(str(base), 'task')
    assert undone['failed'] == 0, undone
    assert undone['undone'] == 1, undone
    assert target.read_bytes() == original
    assert mods.get_modifications(str(base)) == []


def test_group_inverse_budget_refuses_before_file_or_journal_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(_ops, 'recovery_file_bytes', lambda: 32)
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    base = tmp_path / 'project'
    base.mkdir()
    target = base / 'file.txt'
    target.write_bytes(b'A\nC\n')
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, root, rel, **kw: (root, rel))
    result = _ops.tool_edit_file(str(base), [
        {'path':'file.txt', 'operation':'replace', 'anchor':'A', 'content':'x' * 20},
        {'path':'file.txt', 'operation':'replace', 'anchor':'C', 'content':'y' * 20},
    ], task_id='task')
    assert 'undo-budget-exceeded' in result
    assert target.read_bytes() == b'A\nC\n'
    assert mods.get_modifications(str(base)) == []


def test_atomic_group_undo_refuses_a_later_user_edit(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    base = tmp_path / 'project'
    base.mkdir()
    target = base / 'file.txt'
    target.write_bytes(b'A C')
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda target, root, rel, **kw: (root, rel))
    _ops.tool_apply_diffs(str(base), [
        {'path':'file.txt', 'search':'A', 'replace':'B'},
        {'path':'file.txt', 'search':'C', 'replace':'D'},
    ], task_id='task')
    target.write_bytes(b'user edit')
    result = mods.undo_task_modifications(str(base), 'task')
    assert result['undone'] == 0 and result['failed'] == 1
    assert target.read_bytes() == b'user edit'
    assert len(mods.get_modifications(str(base))) == 1
