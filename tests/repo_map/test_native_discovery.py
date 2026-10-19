"""Installed native discovery against real Git, including failure and ownership.

Tests own all repositories/configuration; no production backend is selected.
"""
import ctypes
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def discovery():
    return pytest.importorskip('tofu_repo_kernel').load_discovery()


@pytest.fixture
def git_fixture(tmp_path, monkeypatch):
    for name in os.environ:
        if name.startswith('GIT_'):
            monkeypatch.delenv(name)
    config = tmp_path / 'global-config'
    config.write_text('')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(config))
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    def git(root, *args, input=None):
        return subprocess.run(['git', '-C', str(root), *args], input=input,
                              capture_output=True, check=True, timeout=20)
    return git


def write(root, name, text='fixture\n'):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def oracle(git, root):
    return sorted(set(filter(None, git(root, 'ls-files', '-z', '--cached',
                                      '--others', '--exclude-standard').stdout.split(b'\0'))))


def test_real_git_layouts_and_excludes(discovery, git_fixture, tmp_path):
    git, base = git_fixture, tmp_path
    def repo(name, extra=()):
        root=base/name;root.mkdir();git(root,'init','-q',*extra);return root
    def compare(name, root):
        assert discovery.list_files(root) == oracle(git, root), name
    root=repo('ordinary')
    write(root,'.gitignore','*.tmp\n!keep.tmp\nignored/\n')
    for name in ('tracked.py','keep.tmp','drop.tmp','.hidden.py','sub/new.py','ignored/new.py'):
        write(root,name)
    git(root,'add','tracked.py');compare('tracked_untracked_ignores',root)
    (root/'tracked.py').unlink();compare('deleted_tracked',root)
    compare('subdirectory',root/'sub')
    write(root,'special[1]/new.py');compare('literal_subdirectory',root/'special[1]')
    write(root,'sub/.gitignore','*.py\n');compare('nested_ignore',root)
    git(root,'init','-q',str(root/'nested'));write(root,'nested/inside.py');compare('nested_repository',root)
    outside=base/'outside';outside.mkdir();write(outside,'external.py')
    (root/'linked-directory').symlink_to(outside,target_is_directory=True)
    (root/'linked-file.py').symlink_to(outside/'external.py');(root/'dangling').symlink_to(outside/'absent')
    compare('symlinks',root)
    alias=base/'alias';alias.symlink_to(root/'sub',target_is_directory=True);compare('symlink_root',alias)
    split=repo('split');write(split,'tracked.py');git(split,'add','tracked.py');git(split,'update-index','--split-index');compare('split_index',split)
    sha=repo('sha256',('--object-format=sha256',));write(sha,'tracked.py');git(sha,'add','tracked.py');compare('sha256_index',sha)
    linked_base=repo('linked-base');write(linked_base,'tracked.py');git(linked_base,'add','tracked.py')
    git(linked_base,'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
    linked=base/'linked';git(linked_base,'worktree','add','-q','--detach',str(linked));write(linked,'fresh.py');compare('linked_worktree',linked)
    separate=repo('separate',('--separate-git-dir',str(base/'external.git')));write(separate,'fresh.py');compare('separate_metadata',separate)
    excluded=repo('all-ignored');write(excluded,'.gitignore','*\n');write(excluded,'private.py');compare('authoritative_empty',excluded)
    sparse=repo('sparse');write(sparse,'a/a.py');write(sparse,'b/b.py');git(sparse,'add','a','b')
    git(sparse,'-c','user.name=fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture')
    git(sparse,'sparse-checkout','init','--cone','--sparse-index');git(sparse,'sparse-checkout','set','a');compare('sparse_index',sparse)


def test_raw_names_fresh_edits_and_configuration(discovery, git_fixture, tmp_path):
    git = git_fixture
    root = tmp_path / 'repository'; root.mkdir(); git(root, 'init', '-q')
    for name in (b'line\nname.py', b'nonutf8-\xff.py', 'unicode-代码.py'.encode()):
        fd = os.open(os.fsencode(root) + b'/' + name, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
    write(root, 'tracked.secret'); git(root, 'add', 'tracked.secret')
    write(root, '.gitignore', '*.secret\n'); write(root, 'hidden.secret')
    write(root, 'local.py'); write(root, '.git/info/exclude', 'local.py\n')
    assert discovery.list_files(root) == oracle(git, root)
    write(root, 'fresh.py'); (root / 'tracked.secret').unlink()
    assert discovery.list_files(root) == oracle(git, root)
    excludes = tmp_path / 'excludes'; excludes.write_text('fresh.py\n')
    git(root, 'config', 'core.excludesFile', str(excludes))
    assert discovery.list_files(root) == oracle(git, root)
    excludes.write_text('unicode-*\n')
    assert discovery.list_files(root) == oracle(git, root)


def test_concurrent_roots_and_deadlines_are_isolated(discovery, git_fixture, tmp_path):
    from tofu_repo_kernel._discovery import DiscoveryError
    git = git_fixture
    roots=[]
    for index in range(8):
        root=tmp_path/str(index);root.mkdir();git(root,'init','-q')
        write(root,f'only-{index}.py'); roots.append(root)
    before = os.getcwd()
    def run(index):
        root=roots[index % len(roots)]
        if index % 5 == 0:
            with pytest.raises(DiscoveryError) as error:
                discovery.list_files(root, timeout=0)
            assert error.value.status == 3
        else:
            assert discovery.list_files(root) == [f'only-{index % len(roots)}.py'.encode()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(200)))
    assert os.getcwd() == before
    for root in roots:
        assert discovery.list_files(root) == oracle(git, root)


def test_corrupt_index_withholds_partial_output_and_recovers(discovery, git_fixture, tmp_path):
    from tofu_repo_kernel._discovery import DiscoveryError
    git=git_fixture; git(tmp_path,'init','-q'); write(tmp_path,'tracked.py');git(tmp_path,'add','tracked.py')
    index=tmp_path/'.git/index'; saved=index.read_bytes(); index.write_bytes(b'broken')
    try:
        with pytest.raises(DiscoveryError) as error: discovery.list_files(tmp_path)
        assert error.value.status == 2
        output, length = ctypes.c_void_p(1), ctypes.c_size_t(123)
        path=os.fsencode(tmp_path)
        assert discovery.function(path,len(path),10**9,ctypes.byref(output),ctypes.byref(length)) == 2
        assert output.value is None and length.value == 0
    finally:
        index.write_bytes(saved)
    assert discovery.list_files(tmp_path) == oracle(git,tmp_path)
    assert index.read_bytes() == saved


def test_native_buffer_is_freed_after_python_copy_failure(discovery, git_fixture, tmp_path, monkeypatch):
    from tofu_repo_kernel import _discovery
    git_fixture(tmp_path,'init','-q'); write(tmp_path,'only.py')
    expected_size = sum(len(path) + 1 for path in oracle(git_fixture, tmp_path))
    frees=[]; original=discovery.free
    def free(output,length):
        frees.append(length.value); original(output,length)
    def fail(*args): raise MemoryError('injected result copy')
    monkeypatch.setattr(discovery,'free',free)
    monkeypatch.setattr(_discovery.ctypes,'string_at',fail)
    with pytest.raises(MemoryError,match='injected'): discovery.list_files(tmp_path)
    assert frees == [expected_size]


def test_invalid_inputs_and_artifact_are_rejected(discovery, tmp_path):
    from tofu_repo_kernel._discovery import Discovery
    for root in ('', 'bad\0root'):
        with pytest.raises(ValueError): discovery.list_files(root)
    for timeout in (-1,float('inf'),float('nan'),2**64,2**2048,True,'1'):
        with pytest.raises(ValueError): discovery.list_files(tmp_path,timeout=timeout)
    artifact=tmp_path/'corrupt.so';artifact.write_bytes(b'not a library')
    with pytest.raises(ValueError,match='digest'): Discovery(artifact,'0'*64)
    output,length=ctypes.c_void_p(1),ctypes.c_size_t(9)
    assert discovery.function(None,0,1,ctypes.byref(output),ctypes.byref(length)) == 1
    assert output.value is None and length.value == 0


def test_successful_empty_git_result_is_authoritative(git_fixture, tmp_path):
    from lib.repo_map.outline import list_files
    git_fixture(tmp_path, 'init', '-q')
    write(tmp_path, '.gitignore', '*\n')
    write(tmp_path, 'private.py', 'secret = 1\n')
    assert list_files(str(tmp_path)) == []


@pytest.mark.parametrize('relative', [False, True])
def test_explicit_git_metadata_environment(discovery, git_fixture, tmp_path, monkeypatch, relative):
    git=git_fixture
    root=tmp_path/'work';root.mkdir()
    metadata=tmp_path/'metadata.git'
    git(root,'init','-q','--separate-git-dir',str(metadata));write(root,'local.py')
    monkeypatch.setenv('GIT_DIR', '../metadata.git' if relative else str(metadata))
    monkeypatch.setenv('GIT_WORK_TREE', '.' if relative else str(root))
    if relative:
        from tofu_repo_kernel._discovery import DiscoveryError
        with pytest.raises(DiscoveryError) as error:
            discovery.list_files(root)
        assert error.value.status == 5
        assert oracle(git,root) == [b'local.py']
    else:
        assert discovery.list_files(root) == oracle(git,root)


@pytest.mark.parametrize('variable', ['GIT_COMMON_DIR', 'GIT_ALTERNATE_OBJECT_DIRECTORIES'])
def test_unsupported_environment_is_explicit(discovery, git_fixture, tmp_path, monkeypatch, variable):
    from tofu_repo_kernel._discovery import DiscoveryError
    git_fixture(tmp_path,'init','-q'); write(tmp_path,'file.py')
    monkeypatch.setenv(variable,str(tmp_path/'.git'))
    with pytest.raises(DiscoveryError) as error: discovery.list_files(tmp_path)
    assert error.value.status == 5


def test_alternate_index_is_fresh_and_read_only(discovery, git_fixture, tmp_path, monkeypatch):
    git=git_fixture
    root=tmp_path/'work';root.mkdir();git(root,'init','-q');write(root,'tracked.py')
    index=tmp_path/'alternate.index';monkeypatch.setenv('GIT_INDEX_FILE',str(index))
    git(root,'add','tracked.py'); (root/'tracked.py').unlink()
    before=index.read_bytes()
    assert discovery.list_files(root) == oracle(git,root) == [b'tracked.py']
    assert index.read_bytes() == before


def test_successful_empty_ripgrep_result_is_authoritative(tmp_path):
    import shutil
    from lib.repo_map.outline import list_files
    if shutil.which('rg') is None:
        pytest.skip('installed ripgrep required for the real ignore-rule oracle')
    write(tmp_path, '.ignore', '*\n')
    write(tmp_path, 'private.py', 'secret = 1\n')
    result = subprocess.run(['rg', '--files', '-0', '--no-messages'], cwd=tmp_path,
                            capture_output=True, check=False, timeout=10)
    assert result.returncode == 1 and result.stdout == b''
    assert list_files(str(tmp_path)) == []
