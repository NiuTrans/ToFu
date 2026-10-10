"""apply_diffs atomic=true: all-or-nothing batch semantics.

Default (atomic=false) failure isolation is unchanged; atomic mode pre-flights
every edit and writes NOTHING when any edit fails, reporting would-apply lines
so the model can fix and re-send the batch.
"""
import pytest

from lib.project_mod.write_tools import _ops
from lib.project_mod import modifications as mods
from lib.project_mod import tools as project_tools

pytestmark = pytest.mark.unit


def _base(tmp_path, monkeypatch):
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    base = tmp_path / 'project'
    base.mkdir()
    return str(base)


def test_atomic_all_pass_applies_normally(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    (tmp_path / 'project' / 'a.txt').write_text('A\nC\n')
    result = _ops.tool_apply_diffs(base, [
        {'path': 'a.txt', 'search': 'A', 'replace': 'A\nB'},
        {'path': 'a.txt', 'search': 'C', 'replace': 'B\nC'},
    ], atomic=True)
    assert result.startswith('Applied 2/2'), result
    assert (tmp_path / 'project' / 'a.txt').read_text() == 'A\nB\nB\nC\n'


def test_atomic_one_failure_writes_nothing(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    target = tmp_path / 'project' / 'a.txt'
    target.write_text('A\nC\n')
    result = _ops.tool_apply_diffs(base, [
        {'path': 'a.txt', 'search': 'A', 'replace': 'A\nB'},
        {'path': 'a.txt', 'search': 'DOES NOT EXIST', 'replace': 'x'},
    ], atomic=True)
    assert result.startswith('Atomic batch rejected: 1 of 2 edits failed'), result
    assert 'NO files were modified' in result
    assert '[1] OK a.txt' in result and 'would apply' in result
    assert '[2] FAIL a.txt' in result
    assert target.read_text() == 'A\nC\n'  # untouched
    assert mods.get_modifications(base) == []  # no journal entry either


def test_atomic_failure_in_one_file_protects_other_files(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    (tmp_path / 'project' / 'a.txt').write_text('A\n')
    (tmp_path / 'project' / 'b.txt').write_text('B\n')
    result = _ops.tool_apply_diffs(base, [
        {'path': 'a.txt', 'search': 'A', 'replace': 'A2'},
        {'path': 'b.txt', 'search': 'MISSING', 'replace': 'x'},
    ], atomic=True)
    assert result.startswith('Atomic batch rejected'), result
    assert (tmp_path / 'project' / 'a.txt').read_text() == 'A\n'
    assert (tmp_path / 'project' / 'b.txt').read_text() == 'B\n'


def test_atomic_missing_file_blocks_batch(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    (tmp_path / 'project' / 'a.txt').write_text('A\n')
    result = _ops.tool_apply_diffs(base, [
        {'path': 'a.txt', 'search': 'A', 'replace': 'A2'},
        {'path': 'gone.txt', 'search': 'x', 'replace': 'y'},
    ], atomic=True)
    assert result.startswith('Atomic batch rejected'), result
    assert (tmp_path / 'project' / 'a.txt').read_text() == 'A\n'


def test_default_partial_semantics_unchanged(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    target = tmp_path / 'project' / 'a.txt'
    target.write_text('A\nC\n')
    result = _ops.tool_apply_diffs(base, [
        {'path': 'a.txt', 'search': 'A', 'replace': 'A\nB'},
        {'path': 'a.txt', 'search': 'DOES NOT EXIST', 'replace': 'x'},
    ])
    assert result.startswith('Applied 1/2 edits (1 failed)'), result
    assert target.read_text() == 'A\nB\nC\n'  # the good edit still landed


def test_atomic_success_remains_one_undo_transition_per_file(tmp_path, monkeypatch):
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution', lambda t, root, rel, **kw: (root, rel))
    base = _base(tmp_path, monkeypatch)
    target = tmp_path / 'project' / 'a.txt'
    target.write_text('A\nC\n')
    result = _ops.tool_apply_diffs(base, [
        {'path': 'a.txt', 'search': 'A', 'replace': 'A\nB'},
        {'path': 'a.txt', 'search': 'C', 'replace': 'B\nC'},
    ], atomic=True, task_id='task')
    assert result.startswith('Applied 2/2'), result
    undone = mods.undo_task_modifications(base, 'task')
    assert undone['failed'] == 0 and undone['undone'] == 1, undone
    assert target.read_text() == 'A\nC\n'


def test_atomic_flag_passes_through_dispatch(tmp_path, monkeypatch):
    base = _base(tmp_path, monkeypatch)
    target = tmp_path / 'project' / 'a.txt'
    target.write_text('A\n')
    out = project_tools._exec_apply_diffs(
        {'edits': [
            {'path': 'a.txt', 'search': 'A', 'replace': 'A2'},
            {'path': 'a.txt', 'search': 'NOPE', 'replace': 'x'},
        ], 'atomic': True},
        base, None, None, {})
    assert out.startswith('Atomic batch rejected'), out
    assert target.read_text() == 'A\n'
