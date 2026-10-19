"""Edit admits unrelated effects but never overlaps one file transaction."""
from contextlib import contextmanager
from pathlib import Path
import threading

import pytest

from lib.project_mod import workspace_lease as leases
from lib.project_mod.write_tools import _ops

pytestmark = pytest.mark.unit


@pytest.fixture
def checkouts(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    monkeypatch.setattr(_ops, '_salvage_root', lambda: str(tmp_path / 'salvage'))
    parent = tmp_path / 'repo'
    (parent / '.git').mkdir(parents=True)
    child = parent / '.worktrees' / 'search'
    child.mkdir(parents=True)
    git_dir = parent / '.git' / 'worktrees' / 'search'
    git_dir.mkdir(parents=True)
    (git_dir / 'commondir').write_text('../..')
    (child / '.git').write_text('gitdir: ' + str(git_dir))
    for root in (parent, child):
        (root / 'a.txt').write_text('before\n')
        (root / 'b.txt').write_text('before\n')
    return parent, child


@contextmanager
def held_elsewhere(factory, target):
    ready, release = threading.Event(), threading.Event()
    errors = []

    def hold():
        try:
            with factory(target):
                ready.set()
                assert release.wait(10), 'test did not release holder'
        except BaseException as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert ready.wait(5), 'holder failed to start'
        assert not errors
        yield
    finally:
        release.set()
        thread.join(5)
        assert not thread.is_alive(), 'holder did not quiesce'
        assert not errors


def edit(root, name='a.txt'):
    return _ops.tool_edit_file(str(root), [
        {'path': name, 'operation': 'replace', 'anchor': 'before', 'content': 'after'},
    ])


def test_edit_in_nested_worktree_ignores_parent_checkout_transformation(checkouts):
    parent, child = checkouts
    with held_elsewhere(leases.workspace_write_lease, parent):
        result = edit(child)
        assert 'Applied 1/1' in result
        assert (child / 'a.txt').read_text() == 'after\n'
        assert (parent / 'a.txt').read_text() == 'before\n'


@pytest.mark.parametrize('target_index', [0, 1])
def test_edit_does_not_wait_for_git_metadata(checkouts, target_index):
    parent, child = checkouts
    with held_elsewhere(leases.workspace_git_metadata_lease, parent):
        target = checkouts[target_index]
        assert 'Applied 1/1' in edit(target)
        assert (target / 'a.txt').read_text() == 'after\n'


@pytest.mark.parametrize('api_name', ['workspace_write_lease', 'workspace_tree_write_lease'])
def test_edit_still_refuses_real_same_checkout_transformation(checkouts, api_name):
    parent, _ = checkouts
    with held_elsewhere(getattr(leases, api_name), parent):
        result = edit(parent)
        assert '[workspace-busy]' in result
        assert 'No edits applied' in result
        assert (parent / 'a.txt').read_text() == 'before\n'


def test_edit_refuses_recursive_retirement_of_its_parent(checkouts):
    parent, child = checkouts
    with held_elsewhere(leases.workspace_tree_write_lease, parent):
        assert '[workspace-busy]' in edit(child)
        assert (child / 'a.txt').read_text() == 'before\n'


def test_edit_only_contends_on_the_file_being_published(checkouts, monkeypatch):
    parent, _ = checkouts
    original = _ops._atomic_write_bytes
    entered, release = threading.Event(), threading.Event()
    first_result, errors = [], []

    def publish(target, data, **kwargs):
        if Path(target) == parent / 'a.txt':
            entered.set()
            assert release.wait(10), 'publication was not released'
        return original(target, data, **kwargs)

    monkeypatch.setattr(_ops, '_atomic_write_bytes', publish)

    def first_edit():
        try:
            first_result.append(edit(parent))
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=first_edit)
    worker.start()
    try:
        assert entered.wait(5), 'first edit did not reach publication'
        assert 'Applied 1/1' in edit(parent, 'b.txt')
        refusal = edit(parent, 'a.txt')
        assert '[workspace-busy]' in refusal
        assert (parent / 'a.txt').read_text() == 'before\n'
        assert (parent / 'b.txt').read_text() == 'after\n'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert not errors
    assert 'Applied 1/1' in first_result[0]
    assert (parent / 'a.txt').read_text() == 'after\n'
    assert leases.workspace_lease_fds() == ()


def test_controlled_retirement_retains_tree_with_active_descendant(checkouts, monkeypatch):
    from lib import integration_control

    parent, child = checkouts
    monkeypatch.setattr(integration_control, '_workspace_root', lambda: parent)

    def forbidden_git(*args, **kwargs):
        pytest.fail('retirement started Git before acquiring its affected tree')

    monkeypatch.setattr(integration_control, '_git', forbidden_git)
    with held_elsewhere(leases.workspace_file_write_lease, child / 'a.txt'):
        integration_control._remove_controlled_worktree(parent, parent)
        assert (child / 'a.txt').read_text() == 'before\n'
    assert parent.is_dir()


def test_non_git_edit_serializes_same_file_but_not_other_files(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    root = tmp_path / 'plain-workspace'
    root.mkdir()
    for name in ('a.txt', 'b.txt'):
        (root / name).write_text('before\n')
    with held_elsewhere(leases.workspace_file_write_lease, root / 'a.txt'):
        assert 'Applied 1/1' in edit(root, 'b.txt')
        assert '[workspace-busy]' in edit(root, 'a.txt')
    assert 'Applied 1/1' in edit(root, 'a.txt')
    assert (root / 'a.txt').read_text() == (root / 'b.txt').read_text() == 'after\n'
    assert leases.workspace_lease_fds() == ()


def test_non_git_edit_is_fenced_by_actual_tree_retirement(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_WORKSPACE_TOOL_WAIT_SECONDS', '0')
    monkeypatch.setattr(_ops, '_should_record_modification', lambda *a, **kw: False)
    root = tmp_path / 'plain-workspace'
    root.mkdir()
    (root / 'a.txt').write_text('before\n')
    with held_elsewhere(leases.workspace_tree_write_lease, root):
        assert '[workspace-busy]' in edit(root)
        assert (root / 'a.txt').read_text() == 'before\n'


def test_non_git_file_transaction_excludes_other_process(tmp_path):
    import subprocess
    import sys

    root = tmp_path / 'plain-workspace'
    root.mkdir()
    target = root / 'a.txt'
    target.write_text('before\n')
    code = (
        'import sys; from lib.project_mod import workspace_lease as m; '
        'cm=m.workspace_file_write_lease(sys.argv[1]); cm.__enter__(); '
        'print("ready",flush=True); sys.stdin.read()'
    )
    child = subprocess.Popen([sys.executable, '-c', code, str(target)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == 'ready'
        with pytest.raises(leases.WorkspaceBusyError):
            with leases.workspace_file_write_lease(target):
                pytest.fail('same non-Git target overlapped another process')
        with leases.workspace_file_write_lease(root / 'b.txt'):
            pass
    finally:
        child.stdin.close()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
    assert leases.workspace_lease_fds() == ()
