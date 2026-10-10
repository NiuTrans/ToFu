"""Whole-operation Git supervision, recovery, resource bounds and real upstream I/O."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

# GitPython ships only with the excluded overleaf-MCP environment; skip the
# whole module elsewhere instead of breaking suite collection.
Repo = pytest.importorskip('git', reason='overleaf-MCP environment not installed').Repo
from jsonschema.exceptions import ValidationError
import psutil

from tests.support.overleaf_git import worker_command

pytestmark = pytest.mark.unit
PACKAGE = Path(__file__).resolve().parents[2] / 'packages/tofu-overleaf-mcp'
PROJECT = 'a' * 24


@pytest.fixture
def operations(tmp_path, monkeypatch):
    pytest.importorskip('overleaf_mcp.server')
    monkeypatch.syspath_prepend(str(PACKAGE))
    monkeypatch.setenv('OVERLEAF_GIT_TOKEN', 'isolated-fixture-token')
    from tofu_overleaf_mcp.git_operations import GitOperations
    remote = Repo.init(tmp_path / 'remote.git', bare=True, initial_branch='main')
    source = Repo.init(tmp_path / 'original', initial_branch='main')
    with source.config_writer() as config:
        config.set_value('user', 'name', 'Fixture')
        config.set_value('user', 'email', 'fixture@example.test')
    (Path(source.working_tree_dir) / 'main.tex').write_text('Original paper.')
    source.index.add(['main.tex'])
    source.index.commit('Initial paper')
    source.create_remote('origin', remote.git_dir).push('main:main').raise_if_error()
    runner = GitOperations(command=worker_command(tmp_path, remote.git_dir), temporary_root=tmp_path / 'jobs')
    try:
        yield runner, remote
    finally:
        remote.close()
        source.close()


def write_arguments(content='Updated paper.'):
    return {'project_id': PROJECT, 'file_path': 'main.tex', 'content': content,
            'tofu_write_receipt': {'owner_user_id': 7, 'request_id': 'b' * 32}}


def test_real_clone_write_push_then_fresh_read_is_consistent_and_cleans_workspaces(operations):
    runner, remote = operations
    async def run():
        result = await runner.invoke('rewrite_file', write_arguments())
        assert result['source_revision'] == remote.head.commit.hexsha
        read = await runner.invoke('read_file', {'project_id': PROJECT, 'file_path': 'main.tex'})
        assert 'Updated paper.' in read
    asyncio.run(run())
    assert runner.active is None and not list(runner.temporary_root.iterdir())
    assert 0 < runner.last_usage['peak_memory_bytes'] <= runner.budget.memory_bytes
    assert runner.last_usage['peak_workspace_bytes'] <= runner.budget.workspace_bytes


def test_rejected_commit_cannot_poison_or_publish_with_the_next_request(operations):
    runner, remote = operations
    original = remote.head.commit.hexsha
    hook = Path(remote.git_dir) / 'hooks/pre-receive'
    if os.name == 'nt':
        pytest.skip('The rejection fixture uses a POSIX Git hook')
    hook.write_text('#!/bin/sh\nexit 1\n')
    hook.chmod(0o700)
    async def run():
        with pytest.raises(ValueError, match='git_operation_unconfirmed'):
            await runner.invoke('rewrite_file', write_arguments('Rejected content.'))
        assert remote.head.commit.hexsha == original
        hook.unlink()
        result = await runner.invoke('rewrite_file', write_arguments('Independent next change.'))
        assert result['source_revision'] == remote.head.commit.hexsha
        assert remote.head.commit.parents[0].hexsha == original
        assert (remote.head.commit.tree / 'main.tex').data_stream.read() == b'Independent next change.'
    asyncio.run(run())
    assert not list(runner.temporary_root.iterdir())


def test_old_upstream_cache_and_upload_sources_are_not_reclaimed(operations, tmp_path, monkeypatch):
    runner, remote = operations
    old_cache = tmp_path / 'historical-cache' / PROJECT
    old_cache.mkdir(parents=True)
    unpublished = old_cache / 'main.tex'
    unpublished.write_text('Unpublished user work')
    monkeypatch.setenv('OVERLEAF_TEMP_DIR', str(old_cache.parent))
    source = tmp_path / 'figure.bin'
    source.write_bytes(b'\x00\xffuploaded figure')
    args = {'project_id': PROJECT, 'file_path': 'figure.bin', 'source_path': str(source)}
    result = asyncio.run(runner.invoke('upload_file', args))
    assert result['source_revision'] == remote.head.commit.hexsha
    assert (remote.head.commit.tree / 'figure.bin').data_stream.read() == source.read_bytes()
    assert unpublished.read_text() == 'Unpublished user work'
    assert not list(runner.temporary_root.iterdir())


def test_concurrent_remote_edit_between_section_read_and_rewrite_is_preserved(operations, tmp_path):
    runner, remote = operations
    collaborator = Repo.clone_from(remote.git_dir, tmp_path / 'collaborator')
    with collaborator.config_writer() as config:
        config.set_value('user', 'name', 'Collaborator')
        config.set_value('user', 'email', 'fixture@example.test')
    (Path(collaborator.working_tree_dir) / 'main.tex').write_text('\\section{Intro}\nOriginal paper.\n')
    collaborator.index.add(['main.tex'])
    collaborator.index.commit('Paper section')
    collaborator.remotes.origin.push('main:main').raise_if_error()
    bootstrap = tmp_path / 'concurrent_worker.py'
    bootstrap.write_text('''import sys
from pathlib import Path
from git import Repo
from tofu_overleaf_mcp import git_worker
remote=sys.argv.pop(1)
collaborator=sys.argv.pop(1)
def configure(upstream):
    client=upstream.git_client
    client._git_url=lambda project: remote
    client._project_tag=lambda project: project.project_id
    client._PUSH_RETRIES=0
    client._PULL_TTL=0
    upstream._auto_setup_guard=lambda name: None
    read=client.read_file
    def concurrent_read(*args):
        text=read(*args)
        with Repo(collaborator) as repo:
            (Path(repo.working_tree_dir)/'main.tex').write_text(text+'\\nConcurrent user addition.\\n')
            repo.index.add(['main.tex'])
            repo.index.commit('Concurrent change')
            repo.remotes.origin.push('main:main').raise_if_error()
        return text
    client.read_file=concurrent_read
git_worker.configure_upstream=configure
git_worker.main()
''')
    runner.command = (sys.executable, str(bootstrap), remote.git_dir, collaborator.working_tree_dir)
    try:
        with pytest.raises(ValueError, match='git_operation_unconfirmed'):
            asyncio.run(runner.invoke('update_section', {'project_id': PROJECT, 'file_path': 'main.tex',
                'section_title': 'Intro', 'new_content': 'Agent rewrite.'}))
        content = (remote.head.commit.tree / 'main.tex').data_stream.read()
        assert b'Concurrent user addition.' in content and b'Original paper.' in content
        assert b'Agent rewrite.' not in content
    finally:
        collaborator.close()


@pytest.mark.parametrize('fault', ['project', 'nested', 'size', 'owner'])
def test_invalid_input_never_starts_a_worker_or_retains_a_workspace(operations, fault):
    runner, _ = operations
    arguments = {'project_id': PROJECT}
    if fault == 'project':
        arguments['project_id'] = '../other'
    elif fault == 'nested':
        arguments['nested'] = {'huge': []}
    elif fault == 'size':
        arguments['content'] = 'x' * (16 * 1024**2 + 1)
    else:
        arguments['tofu_write_receipt'] = {'owner_user_id': 0, 'request_id': 'b' * 32}
    with pytest.raises((ValueError, ValidationError)):
        asyncio.run(runner.invoke('rewrite_file', arguments))
    assert runner.active is None and not runner.temporary_root.exists()


def test_git_credentials_are_origin_scoped_and_never_enter_remote_urls(operations, monkeypatch):
    from tofu_overleaf_mcp.git_worker import configure_upstream
    from overleaf_mcp.config import OVERLEAF_GIT_HOST
    client = SimpleNamespace(_project_tag=None)
    upstream = SimpleNamespace(git_client=client)
    monkeypatch.setenv('GIT_CONFIG_COUNT', '0')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'http.extraHeader')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', '')
    monkeypatch.setenv('GIT_CONFIG_NOSYSTEM', '1')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', os.devnull)
    configure_upstream(upstream)
    project = SimpleNamespace(project_id=PROJECT, git_token='test-token-with-sensitive-value')
    url = client._git_url(project)
    assert project.git_token not in url and url == f'https://{OVERLEAF_GIT_HOST}/{PROJECT}'
    client._git_url(project)
    assert os.environ['GIT_CONFIG_COUNT'] == '1'
    # Ask native Git to resolve the configured URL scope; no network is used.
    match = subprocess.run(['git', 'config', '--get-urlmatch', 'http.extraHeader', url],
                           capture_output=True, text=True, check=True)
    assert match.stdout.startswith('Authorization: Basic ')
    foreign = subprocess.run(['git', 'config', '--get-urlmatch', 'http.extraHeader', 'https://different.invalid/project'],
                             capture_output=True, text=True)
    assert foreign.returncode == 1 and not foreign.stdout


def test_low_resource_probe_is_lean_and_overrides_cannot_exceed_contract(monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    from tofu_overleaf_mcp import git_resources
    monkeypatch.setattr(git_resources.psutil, 'virtual_memory', lambda: (_ for _ in ()).throw(OSError()))
    monkeypatch.setattr(git_resources.shutil, 'disk_usage', lambda _: (_ for _ in ()).throw(OSError()))
    budget = git_resources.probe_budget({})
    assert budget.memory_bytes == 128 * 1024**2 and budget.workspace_bytes == 64 * 1024**2
    assert budget.cpu_count == 1
    for env in ({'TOFU_OVERLEAF_GIT_MEMORY_MIB': '513'}, {'TOFU_OVERLEAF_GIT_WORKSPACE_MIB': '257'},
                {'TOFU_OVERLEAF_GIT_MEMORY_MIB': 'no'}):
        with pytest.raises(ValueError, match='git_budget_invalid'):
            git_resources.probe_budget(env)


@pytest.mark.parametrize('ending', ['timeout', 'cancel', 'shutdown'])
def test_hung_native_child_retires_before_admission_is_released(operations, tmp_path, ending):
    runner, _ = operations
    child_pid = tmp_path / 'native.pid'
    bootstrap = tmp_path / 'hung_worker.py'
    bootstrap.write_text('''import json, subprocess, sys, time
from pathlib import Path
root=Path(sys.argv[-1])
while not (root/'admitted').exists(): time.sleep(0.01)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
Path(sys.argv[1]).write_text(str(child.pid))
time.sleep(60)
''')
    runner.command = (sys.executable, str(bootstrap), str(child_pid))
    runner.seconds = 1.5 if ending == 'timeout' else 10
    async def run():
        task = asyncio.create_task(runner.invoke('read_file', {'project_id': PROJECT, 'file_path': 'main.tex'}))
        deadline = time.monotonic() + 5
        while not child_pid.exists():
            assert time.monotonic() < deadline
            await asyncio.sleep(0.05)
        child = psutil.Process(int(child_pid.read_text()))
        with pytest.raises(ValueError, match='git_busy'):
            await runner.invoke('read_file', {'project_id': PROJECT})
        if ending == 'cancel':
            task.cancel()
        elif ending == 'shutdown':
            await runner.aclose()
        with pytest.raises(TimeoutError if ending == 'timeout' else asyncio.CancelledError):
            await task
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        assert runner.active is None and not list(runner.temporary_root.iterdir())
    asyncio.run(run())


def test_oversized_workspace_is_stopped_and_reclaimed(operations, tmp_path):
    runner, _ = operations
    bootstrap = tmp_path / 'disk_worker.py'
    bootstrap.write_text('''import sys,time
from pathlib import Path
root=Path(sys.argv[-1])
while not (root/'admitted').exists(): time.sleep(0.01)
(root/'large').write_bytes(b'x'*(3*1024*1024))
time.sleep(30)
''')
    runner.command = (sys.executable, str(bootstrap))
    runner.budget = replace(runner.budget, workspace_bytes=1024**2)
    with pytest.raises(ValueError, match='git_workspace_limit'):
        asyncio.run(runner.invoke('read_file', {'project_id': PROJECT}))
    assert not list(runner.temporary_root.iterdir())


def test_crash_reclamation_preserves_live_foreign_and_unmarked_work(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(PACKAGE))
    from tofu_overleaf_mcp.git_workspace import create_workspace, identity, private_root
    base = private_root(tmp_path / 'owned')
    stale = base / 'job-stale'
    stale.mkdir()
    (stale / 'owner.json').write_text(json.dumps({'schema': 1, 'parent': {'pid': 99999999, 'created': 1}}))
    (stale / 'reconstructible').write_text('cache')
    live = base / 'job-live'
    live.mkdir()
    (live / 'owner.json').write_text(json.dumps({'schema': 1, 'parent': identity(os.getpid())}))
    foreign = base / 'user-files'
    foreign.mkdir()
    (foreign / 'main.tex').write_text('Durable user paper')
    uncertain = base / 'job-uncertain'
    uncertain.mkdir()
    (uncertain / 'owner.json').write_text(json.dumps({'schema': 1, 'parent': {'pid': True, 'created': 0}}))
    created = create_workspace(base)
    assert created.is_dir() and live.is_dir() and uncertain.is_dir() and not stale.exists()
    assert (foreign / 'main.tex').read_text() == 'Durable user paper'


def test_native_worker_limits_are_installed_before_tool_dispatch(operations, tmp_path):
    if os.name == 'nt':
        pytest.skip('POSIX allocation/CPU/file limits; Windows uses a Job Object')
    runner, _ = operations
    script = tmp_path / 'limits_worker.py'
    script.write_text('''import json,os,resource
from tofu_overleaf_mcp import git_worker
async def dispatch(request):
    return json.dumps({'address':resource.getrlimit(resource.RLIMIT_AS)[0],
        'cpu':resource.getrlimit(resource.RLIMIT_CPU)[0],
        'file':resource.getrlimit(resource.RLIMIT_FSIZE)[0],
        'cores':len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None})
git_worker.dispatch=dispatch
git_worker.main()
''')
    runner.command = (sys.executable, str(script))
    result = json.loads(asyncio.run(runner.invoke('read_file', {'project_id': PROJECT})))
    assert result['address'] == 2 * runner.budget.memory_bytes
    assert result['file'] == runner.budget.workspace_bytes and result['cpu'] == runner.seconds
    if hasattr(os, 'sched_getaffinity'):
        assert result['cores'] == 1


@pytest.mark.parametrize('fault', ['memory', 'response'])
def test_oversized_worker_memory_and_results_fail_explicitly_and_clean_up(operations, tmp_path, fault):
    runner, _ = operations
    script = tmp_path / 'bounded_worker.py'
    body = ("return 'x'*(3*1024*1024)" if fault == 'response' else
            "data=[]\n    while True:\n        data.append(bytearray(4*1024*1024))\n        await asyncio.sleep(0.01)")
    script.write_text('import asyncio\nfrom tofu_overleaf_mcp import git_worker\n'
                      + 'async def dispatch(request):\n    ' + body
                      + '\ngit_worker.dispatch=dispatch\ngit_worker.main()\n')
    runner.command = (sys.executable, str(script))
    runner.budget = replace(runner.budget, memory_bytes=128 * 1024**2)
    with pytest.raises(ValueError, match='git_' + fault + '_limit'):
        asyncio.run(runner.invoke('read_file', {'project_id': PROJECT}))
    assert runner.active is None and not list(runner.temporary_root.iterdir())


def test_cleanup_failure_does_not_erase_acknowledgement_and_next_request_reclaims(operations, monkeypatch):
    runner, remote = operations
    from tofu_overleaf_mcp import git_operations
    original = git_operations.shutil.rmtree
    def fail_once(path):
        monkeypatch.setattr(git_operations.shutil, 'rmtree', original)
        raise PermissionError('injected cleanup failure')
    monkeypatch.setattr(git_operations.shutil, 'rmtree', fail_once)
    result = asyncio.run(runner.invoke('rewrite_file', write_arguments()))
    assert result['source_revision'] == remote.head.commit.hexsha
    assert runner.last_usage['cleanup_pending']
    assert len(list(runner.temporary_root.iterdir())) == 1
    assert 'Updated paper.' in asyncio.run(runner.invoke('read_file', {'project_id': PROJECT, 'file_path': 'main.tex'}))
    assert not list(runner.temporary_root.iterdir())


def test_transport_crash_stops_worker_and_native_child_then_reclaims(tmp_path, monkeypatch):
    if sys.platform != 'linux':
        pytest.skip('Linux subreaper fixture owns and reaps orphaned grandchildren')
    pytest.importorskip('overleaf_mcp.server')
    monkeypatch.syspath_prepend(str(PACKAGE))
    from tofu_overleaf_mcp.git_workspace import create_workspace
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    assert libc.prctl(37, ctypes.byref(previous), 0, 0, 0) == 0
    assert libc.prctl(36, 1, 0, 0, 0) == 0
    marker = tmp_path / 'children.json'
    jobs = tmp_path / 'jobs'
    worker = tmp_path / 'orphan_worker.py'
    worker.write_text('''import asyncio,json,os,subprocess,sys
from pathlib import Path
from tofu_overleaf_mcp import git_worker
async def dispatch(request):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    Path(request['arguments']['marker']).write_text(json.dumps([os.getpid(),child.pid]))
    await asyncio.sleep(60)
git_worker.dispatch=dispatch
git_worker.main()
''')
    driver = tmp_path / 'transport.py'
    driver.write_text('import asyncio,sys\nfrom tofu_overleaf_mcp.git_operations import GitOperations\n'
        + f'runner=GitOperations(command=[sys.executable,{str(worker)!r}],temporary_root={str(jobs)!r})\n'
        + f'asyncio.run(runner.invoke("read_file",{{"project_id":{PROJECT!r},"marker":{str(marker)!r}}}))\n')
    process = subprocess.Popen([sys.executable, str(driver)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    children = []
    try:
        deadline = time.monotonic() + 10
        while not marker.exists():
            assert process.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        children = json.loads(marker.read_text())
        process.kill()
        process.wait(timeout=5)
        for pid in children:
            psutil.Process(pid).wait(timeout=5)
        created = create_workspace(jobs)
        assert list(jobs.iterdir()) == [created]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for pid in children:
            try:
                child = psutil.Process(pid)
                child.kill()
                child.wait(timeout=5)
            except psutil.NoSuchProcess:
                pass
        assert libc.prctl(36, previous.value, 0, 0, 0) == 0
