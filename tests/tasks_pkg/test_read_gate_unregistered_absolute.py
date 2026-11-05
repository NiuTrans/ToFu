"""Absolute reads outside every registered root are read-gate evidence.

Task worktrees live in /tmp, outside the conversation's registered roots. An
absolute read there must certify the same canonical path that a later edit
resolves to, without first registering the directory as a root. Only the
descriptor-pinned bytes the model was shown count: unread files, failed reads,
``..`` detours and symlinked paths certify nothing.
"""
from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.delenv('TOFU_APPLY_DIFF_READ_GATE', raising=False)
    monkeypatch.delenv('TOFU_WRITE_FRESHNESS_GATE', raising=False)
    from lib import write_freshness
    from lib.tasks_pkg.handlers import _read_gate
    import lib.tasks_pkg.handlers.project as handler
    write_freshness._reset_for_tests()
    _read_gate._reset_satisfied_cache_for_tests()

    def finalize(task, rn, round_entry, results, **kwargs):
        round_entry['results'] = results
        round_entry['status'] = kwargs.get('status', 'done')

    monkeypatch.setattr(handler, '_finalize_tool_round', finalize)
    yield
    write_freshness._reset_for_tests()


@pytest.fixture
def tree(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'main.py').write_text('MAIN = 1\n')
    worktree = tmp_path / 'worktree'
    (worktree / 'sub').mkdir(parents=True)
    (worktree / 'a.py').write_text('A = 1\n')
    (worktree / 'b.py').write_text('B = 1\n')
    return project, worktree


def _task(task_id='gate-task'):
    return {'id': task_id, 'convId': 'gate-conv', 'messages': [], 'toolRounds': []}


def _call(task, project, name, args):
    """One pipeline occurrence: its round is published before the handler runs."""
    from lib.tasks_pkg.handlers.project import _handle_project_tool
    number = len(task['toolRounds']) + 1
    call_id = f'call-{number}'
    round_entry = {'roundNum': number, 'toolCallId': call_id, 'toolName': name,
                   'toolArgs': json.dumps(args), 'status': 'searching'}
    task['toolRounds'].append(round_entry)
    _, content, _ = _handle_project_tool(
        task, {}, name, call_id, args, number, round_entry, None, str(project), True)
    return content, round_entry


def _gate(task, project, path):
    from lib.tasks_pkg.handlers._read_gate import check_read_before_edit, partition_batch_edits
    single = check_read_before_edit(
        task, 'apply_diff', {'path': str(path), 'search': 'x', 'replace': 'y'}, str(project))
    skipped, _ = partition_batch_edits(
        task, 'edit_file',
        {'path': str(path), 'edits': [{'operation': 'replace', 'anchor': 'x', 'content': 'y'}]},
        str(project))
    assert (single is None) == (skipped == []), (single, skipped)
    return single


def test_absolute_read_outside_roots_satisfies_edit_of_same_path(tree):
    project, worktree = tree
    target = worktree / 'a.py'
    task = _task()
    content, round_entry = _call(task, project, 'read_files', {'path': str(target)})
    assert 'A = 1' in content
    assert round_entry['_readObservedPaths'] == [str(target)]
    assert _gate(task, project, target) is None

    content, _ = _call(task, project, 'edit_file', {
        'path': str(target),
        'edits': [{'operation': 'replace', 'anchor': 'A = 1', 'content': 'A = 2'}]})
    assert 'must read each target file first' not in content, content
    assert target.read_text() == 'A = 2\n'


def test_each_item_of_an_absolute_batch_read_is_certified(tree):
    project, worktree = tree
    task = _task()
    _call(task, project, 'read_files', {'reads': [
        {'path': str(worktree / 'a.py')},
        {'path': str(worktree / 'b.py'), 'start_line': 1, 'end_line': 1}]})
    assert _gate(task, project, worktree / 'a.py') is None
    assert _gate(task, project, worktree / 'b.py') is None


def test_freshness_token_certifies_after_round_history_is_gone(tree):
    project, worktree = tree
    target = worktree / 'a.py'
    _call(_task(), project, 'read_files', {'path': str(target)})
    # A later task occurrence with the same execution key has no rounds and no
    # messages; only the compaction-proof token remains.
    assert _gate(_task(), project, target) is None
    target.write_text('A = 3\n')
    assert 'must read each target file first' in _gate(_task(), project, target)


def test_registering_the_tree_after_the_read_keeps_one_canonical_path(tree):
    from lib.project_mod.config import build_root_policy, execution_root_scope
    project, worktree = tree
    target = worktree / 'a.py'
    task = _task()
    _call(task, project, 'read_files', {'path': str(target)})
    with execution_root_scope(task['id'], build_root_policy(str(project), [str(worktree)])):
        for path in (str(target), 'worktree:a.py'):
            assert _gate(task, project, path) is None
            assert _gate(_task(), project, path) is None


def test_unread_sibling_in_unregistered_tree_is_still_refused(tree):
    project, worktree = tree
    task = _task()
    _call(task, project, 'read_files', {'path': str(worktree / 'a.py')})
    refusal = _gate(task, project, worktree / 'b.py')
    assert refusal is not None and 'must read each target file first' in refusal


def test_dotdot_detour_cannot_certify_a_different_file(tree):
    project, worktree = tree
    task = _task()
    _call(task, project, 'read_files', {'path': f'{worktree}/sub/../a.py'})
    assert _gate(task, project, worktree / 'a.py') is None
    refusal = _gate(task, project, f'{worktree}/sub/../b.py')
    assert refusal is not None and 'must read each target file first' in refusal


@pytest.mark.parametrize('alias', ['file', 'directory'])
def test_symlinked_read_certifies_neither_alias_nor_target(tree, tmp_path, alias):
    project, worktree = tree
    target = worktree / 'a.py'
    if alias == 'file':
        read_path = worktree / 'alias.py'
        read_path.symlink_to(target)
    else:
        (tmp_path / 'alias-dir').symlink_to(worktree, target_is_directory=True)
        read_path = tmp_path / 'alias-dir' / 'a.py'
    task = _task()
    content, round_entry = _call(task, project, 'read_files', {'path': str(read_path)})
    assert 'A = 1' in content
    assert round_entry['_readObservedPaths'] == []
    for path in (target, read_path):
        refusal = _gate(task, project, path)
        assert refusal is not None and 'must read each target file first' in refusal


def test_failed_absolute_read_certifies_nothing(tree):
    project, worktree = tree
    binary = worktree / 'blob.py'
    binary.write_bytes(b'\x00' * 4096)
    task = _task()
    content, round_entry = _call(task, project, 'read_files', {'path': str(binary)})
    assert 'appears to be binary' in content
    assert round_entry['_readObservedPaths'] == []
    assert 'must read each target file first' in _gate(task, project, binary)
    assert os.path.isfile(binary)


def test_non_regular_absolute_path_is_never_opened(tree, monkeypatch):
    # Opening a device node can have side effects (a tty becomes the server's
    # controlling terminal), so evidence reads keep read_local_file's
    # regular-file precondition.
    project, worktree = tree
    fifo = worktree / 'pipe.py'
    os.mkfifo(fifo)
    opened = []
    real_open = os.open

    def spy(path, *args, **kwargs):
        opened.append(os.fsdecode(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', spy)
    task = _task()
    content, round_entry = _call(task, project, 'read_files', {'path': str(fifo)})
    assert 'File not found' in content
    assert round_entry['_readObservedPaths'] == []
    assert fifo.name not in opened and str(fifo) not in opened
