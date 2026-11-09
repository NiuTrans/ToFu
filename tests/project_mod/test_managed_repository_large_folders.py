"""Preparing a large plain folder made mostly of other projects (2026-11-09, autoharness).

autoharness held 1,397,250 files and no project manifest at its root; 98% of
them sat inside 21,782 nested projects (copied repositories, package caches)
whose source-like text the bulk-data rule never shares, so preparation walked,
listed and tried to version all of them. These specifications pin the
structural fix: a pruned discovery walk that never enters nested projects,
repositories or environments, the owner's typed question when Tofu cannot tell
whether nested projects belong to the project, a recorded decision, resumable
discovery, a typed stall, and a read-only preview.
"""
import json
import os
import subprocess

import pytest

from lib.project_mod import managed_repository as mr
from lib.project_mod.managed_repository import (
    ContentPolicy, ManagedRepositoryUnavailable, link_shared_paths, prepare_managed_repository,
)

pytestmark = pytest.mark.unit

SMALL = ContentPolicy(large_file_bytes=1 << 20, bulk_directory_files=5)
OWN_FILES = {'AGENTS.md', 'check_status.py', '.gitignore', 'docs/plan.md', 'harness/__init__.py',
             'harness/run.py', 'reference/README.md', 'work/receipt.json', 'work/run1/REPORT.md'}


def git(root, *args, check=True):
    return subprocess.run(['git', '-C', str(root), *args], check=check,
                          capture_output=True, text=True, timeout=60)


def write(path, data=b'x\n'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def snapshot(root):
    """Every entry with its content, so an addition, change or deletion shows."""
    state = {}
    for current, directories, files in os.walk(root):
        for name in directories + files:
            path = os.path.join(current, name)
            relative = os.path.relpath(path, root)
            if os.path.islink(path):
                state[relative] = ('link', os.readlink(path))
            elif os.path.isdir(path):
                state[relative] = ('dir',)
            else:
                with open(path, 'rb') as stream:
                    state[relative] = ('file', stream.read())
    return state


def added_by_preparation(before, after):
    """Entries preparation added: only its repository is expected."""
    assert all(after.get(path) == value for path, value in before.items()), 'a user file changed or vanished'
    return {path.split(os.sep, 1)[0] for path in set(after) - set(before)}


@pytest.fixture
def other_projects(tmp_path):
    """autoharness in miniature: own files, copies of other projects, caches and data."""
    root = (tmp_path / 'autoharness').resolve()
    write(root / 'AGENTS.md', b'# autoharness guide\n')
    write(root / 'check_status.py', b'print(1)\n')
    write(root / '.gitignore', b'JOURNAL.md\n.tofu*\noutputs/\n')
    write(root / 'JOURNAL.md', b'journal\n')
    write(root / 'docs' / 'plan.md', b'# plan\n')
    write(root / 'harness' / '__init__.py', b'')
    write(root / 'harness' / 'run.py', b'pass\n')
    write(root / '.venv' / 'pyvenv.cfg', b'home = /usr/bin\n')
    write(root / '.venv' / 'lib' / 'site.py', b'pass\n')
    write(root / '.tofu' / 'memories' / 'm.md', b'm\n')
    write(root / 'agentix' / 'pyproject.toml', b'[project]\nname = "agentix"\n')
    write(root / 'agentix' / 'src' / 'a.py', b'pass\n')
    git(root / 'agentix', 'init', '-q')
    git(root / 'agentix', 'add', '-A')
    git(root / 'agentix', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid',
        'commit', '-q', '-m', 'upstream')
    write(root / 'reference' / 'README.md', b'read-only snapshots\n')
    write(root / 'reference' / 'agentix' / 'pyproject.toml', b'[project]\nname = "agentix"\n')
    for index in range(200):
        write(root / 'reference' / 'agentix' / 'src' / f'm{index}.py', b'pass\n')
    write(root / 'work' / 'receipt.json', b'{}\n')
    write(root / 'work' / 'run1' / 'REPORT.md', b'# run 1\n')
    write(root / 'work' / 'run1' / 'agentix' / 'pyproject.toml', b'[project]\nname = "agentix"\n')
    for index in range(300):
        write(root / 'work' / 'run1' / 'agentix' / 'src' / 'agentix' / f'm{index}.py', b'pass\n')
    for package in ('pkg-a@1', 'pkg-b@1'):
        write(root / 'work' / 'run1' / 'cache' / package / 'package.json', b'{}\n')
        for index in range(200):
            write(root / 'work' / 'run1' / 'cache' / package / 'lib' / f'm{index}.js', b'1;\n')
    for index in range(6):  # bulky data: shared by the existing rule
        write(root / 'work' / 'run1' / 'frames' / f'{index}.bin', b'\0' * 8)
    write(root / 'work' / 'run1' / 'node_modules' / 'x' / 'index.js', b'1;\n')
    write(root / 'outputs' / 'proj' / 'package.json', b'{}\n')  # hidden by the folder's own ignore file
    write(root / 'outputs' / 'proj' / 'a.js', b'1;\n')
    return root


INSIDE_BOUNDARIES = ('agentix/', 'reference/agentix/', 'work/run1/agentix/',
                     'work/run1/cache/pkg-a@1/', 'work/run1/cache/pkg-b@1/',
                     'work/run1/node_modules', '.venv', 'outputs')


@pytest.fixture
def listed(monkeypatch):
    """Directories Python listed during preparation, folder-relative."""
    seen, real = [], os.scandir

    def spy(path='.'):
        if not isinstance(path, int):  # shutil.rmtree lists open descriptors
            seen.append(os.fspath(path))
        return real(path)

    monkeypatch.setattr(os, 'scandir', spy)
    return seen


def relative_listings(seen, root):
    return [os.path.relpath(path, root) for path in seen
            if os.path.abspath(path) == str(root) or os.path.abspath(path).startswith(str(root) + os.sep)]


def tracked(root):
    return set(git(root, 'ls-files').stdout.splitlines())


def manifest(root):
    return json.loads((root / '.git' / mr.MANIFEST_NAME).read_text())


def test_a_folder_of_other_projects_is_refused_with_the_owners_question_and_left_untouched(
        other_projects, listed):
    root = other_projects
    before = snapshot(root)
    listed.clear()  # the snapshot's own walk is not preparation's
    with pytest.raises(ManagedRepositoryUnavailable) as caught:
        prepare_managed_repository(str(root), policy=SMALL)
    walked = relative_listings(listed, root)
    refusal = caught.value
    assert refusal.code == 'ambiguous_project_root'
    assert refusal.details['candidates'] == {'reference': 1, 'work': 3}
    assert refusal.details['recommended'] == 'shared' and set(refusal.details['options']) == {
        'shared', 'versioned'}
    assert 'work/ (3 inside)' in refusal.reason and 'reference/ (1 inside)' in refusal.reason
    assert 'git ' not in refusal.remedy and refusal.hint().startswith('解决办法')
    # Nothing is written into the folder before the owner decides, nothing is removed.
    assert snapshot(root) == before
    # Discovery stopped at every boundary: the copies' contents were never listed.
    assert walked, 'discovery lists the folder itself'
    for path in walked:
        assert not any(path.startswith(boundary) for boundary in INSIDE_BOUNDARIES), path


def test_the_owner_choosing_sharing_versions_the_folder_and_shares_its_projects(
        other_projects, listed, tmp_path):
    root = other_projects
    before = snapshot(root)
    listed.clear()  # the snapshot's own walk is not preparation's
    result = prepare_managed_repository(str(root), policy=SMALL, nested_projects='shared')
    walked = relative_listings(listed, root)
    assert result.state == 'created' and result.guidance == ''  # its own AGENTS.md is kept
    assert tracked(root) == OWN_FILES
    shared = manifest(root)['shared']
    assert set(shared) == set(result.shared) == {
        'agentix', 'reference/agentix', 'work/run1/agentix', 'work/run1/cache', 'work/run1/frames',
        'JOURNAL.md', 'outputs'}
    assert manifest(root)['nestedProjects'] == 'shared'
    assert git(root, 'status', '--porcelain').stdout == ''
    # Only Tofu's standard preparation (its repository) entered the folder; nothing changed or vanished.
    assert added_by_preparation(before, snapshot(root)) == {'.git'}
    for path in walked:
        assert not any(path.startswith(boundary) for boundary in INSIDE_BOUNDARIES), path
    # A worker checkout reads every shared project in place.
    checkout = tmp_path / 'worker'
    git(root, 'worktree', 'add', '-q', '--detach', str(checkout), 'HEAD')
    linked = link_shared_paths(root, checkout)
    assert {'work/run1/cache', 'reference/agentix', 'agentix'} <= set(linked)
    assert (checkout / 'work' / 'run1' / 'cache' / 'pkg-a@1' / 'lib' / 'm0.js').read_bytes() == b'1;\n'
    assert git(checkout, 'status', '--porcelain').stdout == ''


def test_the_owner_choosing_versioning_keeps_nested_projects_in_the_baseline(other_projects):
    root = other_projects
    result = prepare_managed_repository(str(root), policy=SMALL, nested_projects='versioned')
    versioned = tracked(root)
    assert result.state == 'created' and OWN_FILES < versioned
    assert 'reference/agentix/src/m0.py' in versioned
    assert 'work/run1/cache/pkg-a@1/lib/m0.js' in versioned
    assert 'agentix' in result.shared, 'another repository is never versioned'
    assert manifest(root)['nestedProjects'] == 'versioned'
    assert git(root, 'status', '--porcelain').stdout == ''


def test_a_declared_project_root_with_bulky_data_is_prepared_without_a_question(tmp_path):
    """A project manifest at the root makes the root the project: its nested
    packages are part of it, and bulky data is still shared, never versioned."""
    root = (tmp_path / 'service').resolve()
    write(root / 'pyproject.toml', b'[project]\nname = "service"\n')
    for index in range(300):
        write(root / 'src' / 'service' / f'm{index}.py', b'pass\n')
    write(root / 'packages' / 'client' / 'package.json', b'{}\n')
    write(root / 'packages' / 'client' / 'index.js', b'1;\n')
    for shard in range(30):  # many files in a bulky data directory
        for index in range(100):
            write(root / 'datasets' / 'frames' / f's{shard}' / f'{index}.bin', b'\0' * 8)
    before = snapshot(root)
    result = prepare_managed_repository(str(root), policy=ContentPolicy())
    assert result.state == 'created'
    versioned = tracked(root)
    assert 'packages/client/index.js' in versioned and 'src/service/m0.py' in versioned
    assert not any(path.startswith('datasets/') for path in versioned)
    assert 'datasets/frames' in result.shared
    assert 'nestedProjects' not in manifest(root), 'nothing needed deciding'
    assert added_by_preparation(before, snapshot(root)) == {'.git', 'AGENTS.md'}
    assert git(root, 'status', '--porcelain').stdout == ''


def test_an_interrupted_discovery_resumes_where_it_stopped(other_projects, listed):
    root = other_projects

    class StopAfter:
        """The owner's Stop, pressed once a few directories below the root were listed."""
        def __init__(self, directories):
            self.directories = directories

        def is_set(self):
            return len(set(relative_listings(listed, root)) - {'.'}) >= self.directories

    with pytest.raises(mr.PreparationCancelled):
        prepare_managed_repository(str(root), policy=SMALL, cancel=StopAfter(3))
    first = set(relative_listings(listed, root)) - {'.'}
    assert len(first) >= 3 and not (root / '.git').exists()
    listed.clear()
    result = prepare_managed_repository(str(root), policy=SMALL, nested_projects='shared')
    assert result.state == 'created'
    second = set(relative_listings(listed, root)) - {'.'}  # every preparation reads the root
    assert second and not first & second, 'a resumed discovery lists no directory twice'


def test_refresh_shares_a_new_nested_project_once_the_owner_chose_sharing(other_projects):
    root = other_projects
    prepare_managed_repository(str(root), policy=SMALL, nested_projects='shared')
    write(root / 'work' / 'run2' / 'copy' / 'pyproject.toml', b'[project]\nname = "copy"\n')
    write(root / 'work' / 'run2' / 'copy' / 'src' / 'a.py', b'pass\n')
    write(root / 'work' / 'run2' / 'notes.md', b'notes\n')
    refreshed = prepare_managed_repository(str(root), policy=SMALL)
    assert refreshed.state == 'refreshed' and refreshed.added_shared == ('work/run2/copy',)
    assert git(root, 'status', '--porcelain').stdout.splitlines() == ['?? work/run2/']


def test_a_recorded_decision_is_never_overridden(other_projects):
    root = other_projects
    prepare_managed_repository(str(root), policy=SMALL, nested_projects='shared')
    with pytest.raises(ManagedRepositoryUnavailable) as caught:
        prepare_managed_repository(str(root), policy=SMALL, nested_projects='versioned')
    assert caught.value.code == 'decision_recorded'
    assert manifest(root)['nestedProjects'] == 'shared'
    with pytest.raises(ValueError):
        prepare_managed_repository(str(root), policy=SMALL, nested_projects='maybe')


@pytest.mark.parametrize('declared', [False, True])
def test_an_unresponsive_volume_ends_preparation_with_a_typed_stall(other_projects, tmp_path,
                                                                    monkeypatch, declared):
    """A Git that stops executing for one whole stall window is retired and named,
    whether it streams discovery's ignore checks or runs one batch check."""
    import signal
    import threading
    from tests.support.volume_git import volume_git
    root = other_projects
    if declared:
        write(root / 'pyproject.toml', b'[project]\nname = "autoharness"\n')
    before = snapshot(root)
    monkeypatch.setattr(mr, 'GIT_STALL_SECONDS', 1.0)
    script, marker = volume_git(tmp_path, 'hung', 'check-ignore')
    monkeypatch.setattr(mr, 'git_executable', lambda: str(script))
    outcome = []

    # A declared root skips discovery; its first ignore check is the batch one.
    decision = {} if declared else {'nested_projects': 'shared'}

    def prepare():
        try:
            outcome.append(prepare_managed_repository(str(root), policy=SMALL, **decision))
        except BaseException as error:  # the assertion below names it
            outcome.append(error)

    worker = threading.Thread(target=prepare, daemon=True)
    worker.start()
    worker.join(30)
    try:
        assert not worker.is_alive(), 'a hung check must end after one stall window'
    finally:
        if worker.is_alive() and marker.exists():
            os.kill(int(marker.read_text()), signal.SIGKILL)
            worker.join(30)
    assert isinstance(outcome[0], ManagedRepositoryUnavailable), outcome
    assert outcome[0].code == 'volume_stalled' and 'executed for 1s' in str(outcome[0])
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)
    assert snapshot(root) == before


def test_the_owner_may_share_whole_folders_of_run_artifacts(other_projects, listed):
    """A project folder whose run/reference trees are read-only data: the owner names
    them once and they are shared whole (not versioned), including what is not a
    nested project — distributions, wheels, bundles and reports. The rest of the
    folder is versioned and new files there keep being ordinary project files."""
    root = other_projects
    write(root / 'work' / 'node-v22' / 'bin' / 'node', b'\x7fELF')
    write(root / 'work' / 'runtime' / 'python', b'\x7fELF')
    before = snapshot(root)
    listed.clear()  # only preparation's own listings count
    result = prepare_managed_repository(str(root), policy=SMALL,
                                        nested_projects='shared', shared_folders=['work', 'reference'])
    walked = relative_listings(listed, root)  # before snapshot()'s own walk below
    versioned = tracked(root)
    assert versioned == {'AGENTS.md', 'check_status.py', '.gitignore', 'docs/plan.md',
                         'harness/__init__.py', 'harness/run.py'}
    assert {'work', 'reference', 'agentix', 'JOURNAL.md', 'outputs'} <= set(result.shared)
    assert not any(path.startswith('work/') or path.startswith('reference/')
                   for path in versioned)
    assert added_by_preparation(before, snapshot(root)) == {'.git'}
    # Only the two named folders were shared whole; discovery never entered them.
    for path in walked:
        assert not path.startswith(('work/', 'reference/', 'agentix/')), path
    # Sharing whole folders is recorded and never silently undone.
    recorded = prepare_managed_repository(str(root), policy=SMALL)
    assert recorded.state == 'unchanged' and {'work', 'reference'} <= set(recorded.shared)
    # A folder that is not named stays versioned; new project files are ordinary input.
    write(root / 'docs' / 'new.md', b'new\n')
    assert 'docs/new.md' in git(root, 'status', '--porcelain').stdout


def test_preview_reports_preparation_without_writing_into_the_folder(other_projects, tmp_path):
    root = other_projects
    before = snapshot(root)
    ambiguous = mr.preview_preparation(str(root), scratch=tmp_path / 'scratch', policy=SMALL)
    assert ambiguous.refusal is not None and ambiguous.refusal.code == 'ambiguous_project_root'
    assert ambiguous.projects == ('reference/agentix', 'work/run1/agentix',
                                  'work/run1/cache/pkg-a@1', 'work/run1/cache/pkg-b@1')
    shared = mr.preview_preparation(str(root), scratch=tmp_path / 'scratch', policy=SMALL,
                                    nested_projects='shared')
    assert shared.refusal is None and set(shared.versioned) == OWN_FILES
    assert shared.versioned_bytes == sum((root / path).stat().st_size for path in OWN_FILES)
    assert set(shared.shared) == {'agentix', 'reference/agentix', 'work/run1/agentix',
                                  'work/run1/cache', 'work/run1/frames', 'JOURNAL.md', 'outputs'}
    assert shared.seconds['discovery'] >= 0 and shared.seconds['classification'] >= 0
    assert snapshot(root) == before
    # The preview predicts exactly what preparation then does.
    result = prepare_managed_repository(str(root), policy=SMALL, nested_projects='shared')
    assert tracked(root) == set(shared.versioned) and set(result.shared) == set(shared.shared)
