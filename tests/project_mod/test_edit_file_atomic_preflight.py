"""Unified edit safety: complete preflight, truthful receipts and admitted writes."""
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from lib.project_mod import modifications as mods
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def project(tmp_path, monkeypatch):
    import lib.file_history as history

    monkeypatch.setattr(history, 'is_enabled', lambda: False)
    monkeypatch.setattr(mods, 'SESSIONS_DIR', str(tmp_path / 'journals'))
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: True)
    monkeypatch.setattr(_ops, '_mod_attribution',
                        lambda target, base, rel, **kw: (str(Path(target).parent), Path(target).name))
    root = tmp_path / 'project'
    root.mkdir()
    return root


def edit(path, anchor, content, operation='replace'):
    return {'path': str(path), 'operation': operation, 'anchor': anchor, 'content': content}


@pytest.mark.parametrize('mode', ['default', True, False])
def test_absent_semantic_anchors_in_fourteen_mixed_edits_are_all_or_nothing(
        project, monkeypatch, mode):
    originals = {'a.txt': b'old\r\n', 'b.txt': b'ROOT\n', 'c.txt': b'omega\n'}
    for name, data in originals.items():
        (project / name).write_bytes(data)
        (project / name).chmod(0o751)
    edits = [
        edit('a.txt', 'old', 'step1'),
        edit('b.txt', 'ROOT', 'new_one', 'insert_after'),
        edit('a.txt', 'semantic branch that does not exist', 'wrong'),
        edit('c.txt', 'omega', 'before', 'insert_before'),
        edit('a.txt', 'step1', 'step2'),
        edit('b.txt', 'conceptually similar but absent message', 'wrong'),
        edit('c.txt', 'before', 'ready'),
        edit('a.txt', 'step2', 'extra', 'insert_after'),
        edit('c.txt', 'absent semantic anchor', 'wrong', 'insert_after'),
        edit('b.txt', 'new_one', 'new_two'),
        edit('a.txt', 'extra', 'final'),
        edit('b.txt', 'ROOT', 'start', 'insert_before'),
        edit('c.txt', 'omega', 'done'),
        edit('a.txt', 'different missing semantic branch', 'wrong'),
    ]
    writes = []
    writer = _ops._atomic_write_text

    def record_write(target, content, **kwargs):
        writes.append(target)
        return writer(target, content, **kwargs)

    monkeypatch.setattr(_ops, '_atomic_write_text', record_write)
    kwargs = {} if mode == 'default' else {'atomic': mode}
    result = _ops.tool_edit_file(str(project), edits, task_id='batch', **kwargs)
    receipts = [line for line in result.splitlines() if re.match(r'^\[\d+\] (OK|FAIL|SKIP)', line)]
    assert [int(line.split(']')[0][1:]) for line in receipts] == list(range(1, 15))
    assert sum('] FAIL ' in line for line in receipts) == 4
    if mode is False:
        assert result.startswith('Applied 10/14 edits (4 failed)'), result
        assert len(writes) == 3
        assert len(mods.get_modifications(str(project))) == 3
        assert (project / 'a.txt').read_bytes() == b'step2\nfinal\n'
        assert (project / 'b.txt').read_bytes() == b'start\nROOT\nnew_two\n'
        assert (project / 'c.txt').read_bytes() == b'ready\ndone\n'
        undone = mods.undo_task_modifications(str(project), 'batch')
        assert undone['failed'] == 0 and undone['undone'] == 3
    else:
        assert result.startswith('Applied 0/14 edits (4 failed)'), result
        assert 'no files written; 10 skipped' in result
        assert '] OK ' not in result
        assert writes == []
        assert mods.get_modifications(str(project)) == []
        assert not list(project.glob('.tofu_atomic_*'))
        assert not list((project.parent / 'journals').glob('**/write-intents-v1/*.json'))
    for name, data in originals.items():
        assert (project / name).read_bytes() == data
        assert (project / name).stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize('bad', [
    None,
    {'path': 'a.txt', 'operation': [], 'anchor': 'A', 'content': 'bad'},
    {'path': 'a.txt', 'operation': 'replace', 'anchor': 'A'},
    {'path': 'a.txt', 'operation': 'replace', 'anchor': 1, 'content': 'bad'},
    edit('missing.txt', 'A', 'bad'),
    edit('unknown-root:missing.txt', 'A', 'bad'),
    edit('b.txt', 'duplicate', 'bad'),
    edit('a.txt', 'A', 'A', 'insert_after'),
])
def test_any_late_invalid_operation_prevents_earlier_file_write(project, bad):
    (project / 'a.txt').write_text('A\n')
    (project / 'b.txt').write_text('duplicate\nduplicate\n')
    result = _ops.tool_edit_file(str(project), [edit('a.txt', 'A', 'changed'), bad])
    assert result.startswith('Applied 0/2 edits (1 failed)'), result
    assert '[1] SKIP' in result and '[2] FAIL' in result
    assert '] OK ' not in result
    assert (project / 'a.txt').read_text() == 'A\n'
    assert (project / 'b.txt').read_text() == 'duplicate\nduplicate\n'
    assert mods.get_modifications(str(project)) == []


def test_read_error_on_later_file_does_not_publish_earlier_file(project, monkeypatch):
    for name in ('a.txt', 'b.txt'):
        (project / name).write_text('A\n')
    reader = _ops._read_original_text

    def read(target):
        if Path(target).name == 'b.txt':
            raise PermissionError('injected later read failure')
        return reader(target)

    monkeypatch.setattr(_ops, '_read_original_text', read)
    result = _ops.tool_edit_file(str(project), [edit('a.txt', 'A', 'B'), edit('b.txt', 'A', 'B')])
    assert '[1] SKIP' in result and '[2] FAIL' in result
    assert '[read-failed]' in result
    assert all((project / name).read_text() == 'A\n' for name in ('a.txt', 'b.txt'))
    assert mods.get_modifications(str(project)) == []


def test_late_invalid_entry_does_not_hide_dependent_preflight_or_original_indexes(project):
    target = project / 'a.txt'
    target.write_text('STATE_000\n')
    edits = [edit('a.txt', f'STATE_{n:03d}', f'STATE_{n + 1:03d}') for n in range(40)]
    edits += [None, edit('a.txt', 'STATE_040', 'FINAL')]
    result = _ops.tool_edit_file(str(project), edits)
    assert result.startswith('Applied 0/42 edits (1 failed)'), result
    assert '[40] SKIP' in result and '[41] FAIL' in result and '[42] SKIP' in result
    assert target.read_text() == 'STATE_000\n'
    assert mods.get_modifications(str(project)) == []


def test_same_physical_file_aliases_share_sequential_buffer_and_one_undo(project):
    target = project / 'a.txt'
    target.write_bytes(b'A\r\n')
    target.chmod(0o751)
    alias = project / 'alias.txt'
    alias.symlink_to(target.name)
    result = _ops.tool_edit_file(str(project), [
        edit('a.txt', 'A', 'B'), edit('alias.txt', 'B', 'C'),
        edit('a.txt', 'C', 'D', 'insert_after'),
    ], task_id='aliases')
    assert result.startswith('Applied 3/3 edits'), result
    assert target.read_bytes() == b'C\nD\n'
    assert alias.is_symlink()
    assert len(mods.get_modifications(str(project))) == 1
    undone = mods.undo_task_modifications(str(project), 'aliases')
    assert undone['failed'] == 0 and undone['undone'] == 1
    assert target.read_bytes() == b'A\r\n'
    assert target.stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize('kind', ['busy', 'corrupt'])
def test_all_destination_journals_are_admitted_before_first_write(project, tmp_path, monkeypatch, kind):
    from lib.project_mod import session_lock as locks

    other = tmp_path / 'other'
    other.mkdir()
    first, last = project / 'a.txt', other / 'b.txt'
    first.write_text('A\n')
    last.write_text('A\n')
    edits = [edit(first, 'A', 'B'), edit(last, 'A', 'B')]
    session = mods._get_session_dir(str(other))
    monkeypatch.setattr(locks, '_wait_seconds', lambda: 0)
    if kind == 'corrupt':
        Path(session, 'modifications.json').write_text('{corrupt')
        result = _ops.tool_edit_file(str(project), edits)
        assert '[write-failed]' in result, result
        assert Path(session, 'modifications.json').read_text() == '{corrupt'
    else:
        with locks.locked_session(session), ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(_ops.tool_edit_file, str(project), edits).result(timeout=5)
        assert '[journal-busy]' in result
    assert result.startswith('Applied 0/2 edits (1 failed)'), result
    assert '[1] SKIP' in result and '[2] FAIL' in result
    assert first.read_text() == last.read_text() == 'A\n'
    assert mods.get_modifications(str(project)) == []


@pytest.mark.parametrize('value', [None, 0, '', 'false'])
def test_only_explicit_false_enables_partial_application(project, value):
    target = project / 'a.txt'
    target.write_text('A\n')
    result = _ops.tool_edit_file(str(project), [edit('a.txt', 'A', 'B'), None], atomic=value)
    assert result.startswith('Applied 0/2'), result
    assert target.read_text() == 'A\n'


@pytest.mark.parametrize(('before', 'anchor', 'content', 'operation', 'expected'), [
    ('A', 'A', 'B', 'insert_after', 'A\nB\n'),
    ('A', 'A', 'B\n', 'insert_after', 'A\nB\n'),
    ('prefix A', 'A', 'B', 'insert_after', 'prefix A\nB\n'),
    ('A\n', 'A', 'B', 'insert_after', 'A\nB\n'),
    ('A\n', 'A\n', 'B', 'insert_after', 'A\nB\n'),
    ('A\nC', 'A', 'B', 'insert_after', 'A\nB\nC'),
    ('A\nC', 'C', 'B', 'insert_before', 'A\nB\nC'),
    ('A', 'A', 'B', 'insert_before', 'B\nA'),
    ('A', 'A', 'A\nB', 'insert_after', 'A\nB\n'),
])
def test_eof_insert_separates_lines_without_extra_blank_lines(
        project, before, anchor, content, operation, expected):
    target = project / 'a.txt'
    target.write_text(before)
    result = _ops.tool_edit_file(str(project), [edit('a.txt', anchor, content, operation)], task_id='eof')
    assert result.startswith('Applied 1/1 edits'), result
    assert target.read_text() == expected
    undone = mods.undo_task_modifications(str(project), 'eof')
    assert undone['failed'] == 0
    assert target.read_text() == before


def test_late_write_path_refusal_skips_other_files_without_success_advisories(project, monkeypatch):
    for name in ('a.txt', 'b.txt'):
        (project / name).write_text('A\n')
    resolver = _ops._resolve_write_path
    advisories = []

    def resolve(base, rel, **kwargs):
        if rel == 'b.txt':
            raise ValueError('Root b is read-only')
        return resolver(base, rel, **kwargs)

    monkeypatch.setattr(_ops, '_resolve_write_path', resolve)
    monkeypatch.setattr(_ops, '_post_edit_guard', lambda *a, **kw: advisories.append(a))
    result = _ops.tool_edit_file(str(project), [edit('a.txt', 'A', 'B'), edit('b.txt', 'A', 'B')])
    assert result.startswith('Applied 0/2 edits (1 failed)'), result
    assert '[1] SKIP' in result and '[2] FAIL' in result
    assert '[path-invalid]' in result
    assert advisories == []
    assert all((project / name).read_text() == 'A\n' for name in ('a.txt', 'b.txt'))
    assert mods.get_modifications(str(project)) == []


def test_io_failure_after_successful_preflight_does_not_claim_rollback(project, monkeypatch):
    for name in ('a.txt', 'b.txt'):
        (project / name).write_text('A\n')
    writer = _ops._atomic_write_text

    def write(target, content, **kwargs):
        if Path(target).name == 'b.txt':
            raise OSError('injected late I/O failure')
        return writer(target, content, **kwargs)

    monkeypatch.setattr(_ops, '_atomic_write_text', write)
    result = _ops.tool_edit_file(str(project), [edit('a.txt', 'A', 'B'), edit('b.txt', 'A', 'B')])
    assert result.startswith('Applied 1/2 edits (1 failed)'), result
    assert '[1] OK' in result and '[2] FAIL' in result
    assert 'no files written' not in result and 'rollback' not in result
    assert (project / 'a.txt').read_text() == 'B\n'
    assert (project / 'b.txt').read_text() == 'A\n'
    records = mods.get_modifications(str(project))
    assert len(records) == 1 and records[0]['path'] == 'a.txt'


@pytest.mark.parametrize('atomic', [True, False])
def test_project_symlink_escape_cannot_become_untracked_scratch(project, monkeypatch, atomic):
    from lib.project_mod import config
    from lib.project_mod.write_tools import _paths

    monkeypatch.setattr(_ops, '_should_record_modification', _paths._should_record_modification)
    monkeypatch.setattr(_ops, '_mod_attribution', _paths._mod_attribution)
    monkeypatch.setattr(config, 'get_conv_roots', lambda _cid: {'project': {'path': str(project)}})
    outside = project.parent / 'outside.txt'
    outside.write_text('original\n')
    (project / 'escape.txt').symlink_to(outside)
    result = _ops.tool_edit_file(str(project), [edit('escape.txt', 'original', 'changed')],
                                 conv_id='symlink-escape', atomic=atomic)
    assert 'Applied 0/1' in result
    assert 'outside' in result or 'root' in result
    assert outside.read_bytes() == b'original\n'
    assert mods.get_modifications(str(project)) == []
    assert not list((project.parent / 'journals').glob('**/write-intents-v1/*.json'))


@pytest.mark.parametrize('atomic', [True, False])
def test_symlinked_registered_root_keeps_tracking_and_undo(project, monkeypatch, atomic):
    from lib.project_mod import config
    from lib.project_mod.write_tools import _paths

    alias = project.parent / 'registered-alias'
    alias.symlink_to(project, target_is_directory=True)
    monkeypatch.setattr(_ops, '_should_record_modification', _paths._should_record_modification)
    monkeypatch.setattr(_ops, '_mod_attribution', _paths._mod_attribution)
    monkeypatch.setattr(config, 'get_conv_roots', lambda _cid: {'project': {'path': str(alias)}})
    (project / 'a.txt').write_text('original\n')
    result = _ops.tool_edit_file(str(alias), [edit('a.txt', 'original', 'changed')],
                                 conv_id='symlink-root', task_id='alias-edit', atomic=atomic)
    assert 'Applied 1/1' in result, result
    assert (project / 'a.txt').read_text() == 'changed\n'
    assert len(mods.get_modifications(str(alias))) == 1
    undone = mods.undo_task_modifications(str(alias), 'alias-edit')
    assert undone['failed'] == 0 and undone['undone'] == 1
    assert (project / 'a.txt').read_text() == 'original\n'


@pytest.mark.parametrize('atomic', [True, False])
@pytest.mark.parametrize('bad_kind', ['utf8', 'intent-path'])
def test_deterministic_publication_failures_are_preflighted(project, atomic, bad_kind):
    second = 'b.txt' if bad_kind == 'utf8' else 'back\\slash.txt'
    (project / 'a.txt').write_text('first\n')
    (project / second).write_text('second\n')
    replacement = '\ud800' if bad_kind == 'utf8' else 'changed'
    result = _ops.tool_edit_file(str(project), [
        edit('a.txt', 'first', 'changed'), edit(second, 'second', replacement),
    ], task_id='invalid-publication', atomic=atomic)
    assert '[2] FAIL' in result
    assert (project / second).read_bytes() == b'second\n'
    if atomic:
        assert 'Applied 0/2' in result and '[1] SKIP' in result
        assert (project / 'a.txt').read_bytes() == b'first\n'
        assert mods.get_modifications(str(project)) == []
    else:
        assert 'Applied 1/2' in result and '[1] OK' in result
        assert (project / 'a.txt').read_bytes() == b'changed\n'
        assert len(mods.get_modifications(str(project))) == 1
    assert not list(project.glob('.tofu_atomic_*'))
    assert not list((project.parent / 'journals').glob('**/write-intents-v1/*.json'))
