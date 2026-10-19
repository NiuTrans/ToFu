"""Real installed discovery children, Git parity, and joined failure ownership."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest
from tests.repo_map.test_native_discovery import (
    git_fixture as git_fixture, oracle, write,
    test_real_git_layouts_and_excludes as assert_layouts,
    test_raw_names_fresh_edits_and_configuration as assert_fresh_names,
)

pytestmark = [pytest.mark.unit, pytest.mark.skipif(sys.platform != 'linux',
             reason='process transport requires Linux sealed execution')]


@pytest.fixture
def process_discovery():
    return pytest.importorskip('tofu_repo_kernel').load_discovery_process()


def test_process_matches_real_git_layouts(process_discovery, git_fixture, tmp_path):
    assert_layouts(process_discovery, git_fixture, tmp_path)


def test_process_preserves_raw_names_and_fresh_configuration(process_discovery, git_fixture, tmp_path):
    assert_fresh_names(process_discovery, git_fixture, tmp_path)


@pytest.mark.parametrize('relative', [False, True])
def test_process_owns_relative_git_environment(process_discovery, git_fixture, tmp_path, monkeypatch, relative):
    root=tmp_path/'root';root.mkdir();metadata=tmp_path/'metadata'
    git_fixture(root,'init','-q','--separate-git-dir',str(metadata));write(root,'one.py')
    monkeypatch.setenv('GIT_DIR','../metadata' if relative else str(metadata))
    monkeypatch.setenv('GIT_WORK_TREE','.' if relative else str(root))
    before=os.getcwd()
    assert process_discovery.list_files(root) == oracle(git_fixture,root) == [b'one.py']
    assert os.getcwd() == before


def test_process_concurrent_roots_are_isolated(process_discovery, git_fixture, tmp_path):
    roots=[]
    for i in range(8):
        root=tmp_path/str(i);root.mkdir();git_fixture(root,'init','-q');write(root,f'{i}.py');roots.append(root)
    def run(i):
        assert process_discovery.list_files(roots[i%8]) == [f'{i%8}.py'.encode()]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run,range(64)))


def assert_joined(process):
    assert process.returncode is not None
    assert process.stdout.closed and process.stderr.closed
    if os.name=='posix':
        with pytest.raises(ChildProcessError): os.waitpid(process.pid,os.WNOHANG)


@pytest.mark.skipif(os.name!='posix',reason='owned FIFO filesystem fault requires POSIX')
@pytest.mark.parametrize('failure', ['timeout','cancel','caller_error','repeated_interrupt'])
def test_blocked_filesystem_child_is_killed_and_joined(process_discovery, git_fixture, tmp_path, monkeypatch, failure):
    from tofu_repo_kernel import _process_discovery as module
    git_fixture(tmp_path,'init','-q');config=tmp_path/'.git/config';config.unlink();os.mkfifo(config)
    original=module.subprocess.Popen;children=[];cancel=threading.Event();timers=[]
    primary=LookupError('injected caller failure');counts={'kill':0,'wait':0}
    def start(*args,**kwargs):
        process=original(*args,**kwargs);children.append(process)
        assert 'cwd' not in kwargs
        if failure=='cancel':
            timer=threading.Timer(.05,cancel.set);timer.start();timers.append(timer)
        if failure in ('caller_error','repeated_interrupt'):
            def communicate(*args,**kwargs): raise primary
            process.communicate=communicate
        if failure=='repeated_interrupt':
            kill,wait=process.kill,process.wait
            def interrupted_kill():
                counts['kill']+=1
                if counts['kill']<=2: raise KeyboardInterrupt()
                return kill()
            def interrupted_wait(*args,**kwargs):
                counts['wait']+=1
                if counts['wait']<=2: raise KeyboardInterrupt()
                return wait(*args,**kwargs)
            process.kill=interrupted_kill;process.wait=interrupted_wait
        return process
    monkeypatch.setattr(module.subprocess,'Popen',start)
    expected=module.DiscoveryTimeout if failure=='timeout' else module.DiscoveryCancelled if failure=='cancel' else LookupError
    try:
        with pytest.raises(expected) as caught:
            process_discovery.list_files(tmp_path,timeout=.15 if failure=='timeout' else 5,
                                         cancellation=cancel if failure=='cancel' else None)
        if failure in ('caller_error','repeated_interrupt'): assert caught.value is primary
    finally:
        for timer in timers:timer.cancel();timer.join()
    assert len(children)==1
    assert_joined(children[0])
    if failure in ('timeout','cancel'):
        assert caught.value.returncode == children[0].returncode
        assert caught.value.cancelled == (failure=='cancel')
        assert caught.value.timed_out == (failure=='timeout')
    if failure=='repeated_interrupt':
        assert counts['kill']>=3 and counts['wait']>=3
        assert any('KeyboardInterrupt' in note for note in caught.value.__notes__)


def test_cancelled_and_expired_requests_never_launch(process_discovery, tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    def forbidden(*args,**kwargs):raise AssertionError('request launched')
    monkeypatch.setattr(module.subprocess,'Popen',forbidden)
    with pytest.raises(module.DiscoveryTimeout):process_discovery.list_files(tmp_path,timeout=0)
    cancel=threading.Event();cancel.set()
    with pytest.raises(module.DiscoveryCancelled):process_discovery.list_files(tmp_path,cancellation=cancel)
    with pytest.raises(module.DiscoveryCancelled) as caught:
        process_discovery.list_files(tmp_path,timeout=0,cancellation=cancel)
    assert caught.value.timed_out and caught.value.cancelled and caught.value.returncode is None


@pytest.mark.skipif(os.name!='posix',reason='owned shell fault executable requires POSIX')
@pytest.mark.parametrize('frame,code', [(b'partial\0',2),(b'unterminated',0),(b'\0name\0',0),(b'name\0\0',0)])
def test_failed_or_invalid_output_is_withheld_and_joined(tmp_path, monkeypatch, frame, code):
    from tofu_repo_kernel import _process_discovery as module
    executable=tmp_path/'fixture';escaped=''.join('\\%03o'%byte for byte in frame)
    executable.write_text("#!/bin/sh\nprintf '"+escaped+"'\nexit "+str(code)+"\n");executable.chmod(0o700)
    discovery=module.ProcessDiscovery(executable,hashlib.sha256(executable.read_bytes()).hexdigest())
    original=module.subprocess.Popen;children=[]
    def start(*args,**kwargs):
        process=original(*args,**kwargs);children.append(process);return process
    monkeypatch.setattr(module.subprocess,'Popen',start)
    with pytest.raises(module.DiscoveryError if code else ValueError):discovery.list_files(tmp_path)
    assert_joined(children[0])


@pytest.mark.skipif(os.name!='posix',reason='owned executable permissions require POSIX')
def test_replaced_artifact_cannot_change_admitted_execution(tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    executable=tmp_path/'fixture';executable.write_text('#!/bin/sh\nexit 0\n');executable.chmod(0o700)
    with module.ProcessDiscovery(executable,hashlib.sha256(executable.read_bytes()).hexdigest()) as discovery:
        executable.write_text('#!/bin/sh\nexit 1\n')
        assert discovery.list_files(tmp_path) == []
        original=module.subprocess.Popen
        def replace_at_launch(*args,**kwargs):
            executable.unlink();executable.write_text('#!/bin/sh\nexit 2\n');executable.chmod(0o700)
            return original(*args,**kwargs)
        monkeypatch.setattr(module.subprocess,'Popen',replace_at_launch)
        assert discovery.list_files(tmp_path) == []
        with pytest.raises(OSError): os.pwrite(discovery._image.fileno(), b'bad', 0)
        with pytest.raises(OSError): os.ftruncate(discovery._image.fileno(), 0)
    with pytest.raises(ValueError,match='closed'): discovery.list_files(tmp_path)


def test_closed_image_does_not_invalidate_acquired_launch(tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    executable=tmp_path/'fixture';executable.write_text('#!/bin/sh\nexit 0\n');executable.chmod(0o700)
    with module.ProcessDiscovery(executable,hashlib.sha256(executable.read_bytes()).hexdigest()) as discovery:
        original=module.subprocess.Popen;children=[]
        def close_after_launch(*args,**kwargs):
            discovery.close();child=original(*args,**kwargs);children.append(child);return child
        monkeypatch.setattr(module.subprocess,'Popen',close_after_launch)
        assert discovery.list_files(tmp_path) == []
    assert_joined(children[0])


def test_failed_image_acquisition_retires_descriptors(tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    executable=tmp_path/'fixture';executable.write_bytes(b'bad')
    original=module.create_memfd;descriptors=[]
    def create():
        descriptor=original();descriptors.append(descriptor);return descriptor
    monkeypatch.setattr(module,'create_memfd',create)
    with pytest.raises(ValueError,match='digest'):
        module.ProcessDiscovery(executable,'0'*64)
    executable.unlink();os.mkfifo(executable)
    with pytest.raises(ValueError,match='regular file'):
        module.ProcessDiscovery(executable,'0'*64)
    for descriptor in descriptors:
        with pytest.raises(OSError): os.fstat(descriptor)


def test_process_relative_index_and_excludes_are_fresh(process_discovery, git_fixture, tmp_path, monkeypatch):
    root=tmp_path/'root';root.mkdir();git_fixture(root,'init','-q');write(root,'tracked.py')
    monkeypatch.setenv('GIT_INDEX_FILE','../alternate.index')
    git_fixture(root,'add','tracked.py');(root/'tracked.py').unlink()
    write(root,'ignored.py');write(root,'visible.py');write(root,'ignore-rules','ignored.py\n')
    git_fixture(root,'config','core.excludesFile','ignore-rules')
    assert process_discovery.list_files(root)==oracle(git_fixture,root)
    write(root,'ignore-rules','visible.py\n')
    assert process_discovery.list_files(root)==oracle(git_fixture,root)


@pytest.mark.skipif(os.name!='posix',reason='owned executable requires POSIX')
def test_completed_child_keeps_deadline_and_cancellation_facts(tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    executable=tmp_path/'fixture';executable.write_text('#!/bin/sh\nexit 3\n');executable.chmod(0o700)
    discovery=module.ProcessDiscovery(executable,hashlib.sha256(executable.read_bytes()).hexdigest())
    original=module.subprocess.Popen;children=[];cancel=threading.Event()
    def start(*args,**kwargs):
        child=original(*args,**kwargs);children.append(child);communicate=child.communicate
        def complete(*args,**kwargs):
            result=communicate(*args,**kwargs);cancel.set();return result
        child.communicate=complete;return child
    monkeypatch.setattr(module.subprocess,'Popen',start)
    with pytest.raises(module.DiscoveryCancelled) as caught:
        discovery.list_files(tmp_path,cancellation=cancel)
    assert caught.value.timed_out and caught.value.cancelled and caught.value.returncode==3
    assert_joined(children[0])



def test_child_keeps_git_config_without_inheriting_credentials(process_discovery, git_fixture, tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    git_fixture(tmp_path,'init','-q');write(tmp_path,'ignored.py');write(tmp_path,'visible.py')
    write(tmp_path,'ignore-rules','ignored.py\n')
    monkeypatch.setenv('GIT_CONFIG_COUNT','1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0','core.excludesFile')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0','ignore-rules')
    private_names=('TOFU_TEST_API_KEY','TEST_SECRET','TEST_TOKEN','TEST_PASSWORD',
                   'GIT_PRIVATE_TOKEN','UNRELATED_SESSION_COOKIE')
    for name in private_names: monkeypatch.setenv(name,'synthetic-not-a-real-credential')
    expected=oracle(git_fixture,tmp_path)
    original=module.subprocess.Popen;seen=[]
    def start(*args,**kwargs):
        environment=kwargs['env'];seen.append(True)
        assert not set(private_names).intersection(environment)
        assert environment['GIT_CONFIG_KEY_0']=='core.excludesFile'
        assert environment['GIT_CONFIG_VALUE_0']=='ignore-rules'
        assert 'LD_PRELOAD' not in environment and 'PYTHONPATH' not in environment
        return original(*args,**kwargs)
    monkeypatch.setattr(module.subprocess,'Popen',start)
    assert process_discovery.list_files(tmp_path)==expected
    assert seen==[True] and b'ignored.py' not in expected


def test_launch_failure_retires_duplicated_image(process_discovery, tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    descriptors=[]
    def fail(*args,**kwargs):
        descriptors.extend(kwargs['pass_fds']);raise OSError('injected launch failure')
    monkeypatch.setattr(module.subprocess,'Popen',fail)
    with pytest.raises(OSError,match='injected launch failure'):
        process_discovery.list_files(tmp_path)
    assert len(descriptors)==1
    with pytest.raises(OSError):os.fstat(descriptors[0])
    assert os.fstat(process_discovery._image.fileno()).st_size>0


def test_release_failure_retires_acquired_descriptor(process_discovery, tmp_path, monkeypatch):
    from tofu_repo_kernel import _process_discovery as module
    original_lock=process_discovery._lock;duplicate=module.os.dup;descriptors=[]
    primary=LookupError('injected release failure')
    class FailingRelease:
        def __enter__(self): return original_lock.__enter__()
        def __exit__(self,*args):
            original_lock.__exit__(*args);raise primary
    def observe(fd):
        copied=duplicate(fd);descriptors.append(copied);return copied
    monkeypatch.setattr(module.os,'dup',observe)
    monkeypatch.setattr(process_discovery,'_lock',FailingRelease())
    with pytest.raises(LookupError) as caught:process_discovery.list_files(tmp_path)
    assert caught.value is primary and len(descriptors)==1
    with pytest.raises(OSError):os.fstat(descriptors[0])
