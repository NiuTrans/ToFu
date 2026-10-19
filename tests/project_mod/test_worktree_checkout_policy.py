"""Fresh checkout defaults scale without changing Git configuration or content."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from lib.project_mod import worktree_checkout_policy as policy
from lib.project_mod.worktree_process import run_worktree_add

pytestmark = pytest.mark.unit


@pytest.fixture
def repo(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
               GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
               GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
    root = tmp_path / 'repo'
    root.mkdir()
    git(root, env, 'init', '-q')
    return root, env


def git(root, env, *args):
    cp = subprocess.run([policy.git_executable(), *args], cwd=root, env=env,
                        capture_output=True, text=True, timeout=20, check=True)
    return cp.stdout.strip()


def resources(monkeypatch, cpu=63, capacity=8192, available=8192):
    monkeypatch.setattr(policy, 'probe_system_resources', lambda: SimpleNamespace(
        effective_cpu_count=cpu, effective_memory_capacity_mb=capacity,
        effective_memory_available_mb=available))


@pytest.mark.parametrize('cpu,capacity,available,expected', [
    (63,8192,8192,63), (128,65536,65536,128), (16,8192,256,2),
    (16,128,8192,1), (1,8192,8192,1), (16,None,8192,1),
    (16,8192,None,1), (16,8192,0,1), (0,8192,8192,1),
])
def test_capacity_defaults(repo, monkeypatch, cpu, capacity, available, expected):
    root, env = repo
    resources(monkeypatch, cpu, capacity, available)
    assert policy.checkout_worker_config(root, env=env) == ['-c', f'checkout.workers={expected}']
    assert subprocess.run([policy.git_executable(), 'config', '--get', 'checkout.workers'],
                          cwd=root, env=env, capture_output=True).returncode == 1


@pytest.mark.parametrize('value', ['1', '8', '0', '-1', '', 'invalid'])
def test_explicit_git_policy_is_not_overridden(repo, monkeypatch, value):
    root, env = repo
    git(root, env, 'config', 'checkout.workers', value)
    monkeypatch.setattr(policy, 'probe_system_resources', lambda: pytest.fail('unneeded probe'))
    assert policy.checkout_worker_config(root, env=env) == []


def test_global_and_worktree_specific_config(repo, tmp_path, monkeypatch):
    root, env = repo
    resources(monkeypatch)
    global_config = tmp_path / 'gitconfig'
    global_config.write_text('[checkout]\nworkers = 3\n')
    assert policy.checkout_worker_config(root, env={**env, 'GIT_CONFIG_GLOBAL': str(global_config)}) == []
    (root / 'a').write_text('a')
    git(root, env, 'add', '.')
    git(root, env, 'commit', '-qm', 'base')
    git(root, env, 'config', 'extensions.worktreeConfig', 'true')
    destination = tmp_path / 'work'
    git(root, env, 'worktree', 'add', '--no-checkout', '--detach', str(destination), 'HEAD')
    git(destination, env, 'config', '--worktree', 'checkout.workers', '2')
    assert policy.checkout_worker_config(destination, env=env) == []
    assert policy.checkout_worker_config(root, env=env) == ['-c', 'checkout.workers=63']


def test_probe_failure_is_lean(repo, monkeypatch):
    root, env = repo
    def fail():
        raise OSError('unavailable')
    monkeypatch.setattr(policy, 'probe_system_resources', fail)
    assert policy.checkout_worker_config(root, env=env) == ['-c', 'checkout.workers=1']


@pytest.mark.parametrize('failure', ['timeout', 'error'])
def test_config_failure_does_not_override(monkeypatch, failure):
    monkeypatch.setattr(policy, 'git_executable', lambda: 'git')
    def run(*args, **kwargs):
        assert kwargs['timeout'] == 2.0
        if failure == 'timeout':
            raise subprocess.TimeoutExpired('git', 2)
        return SimpleNamespace(returncode=128)
    monkeypatch.setattr(policy.subprocess, 'run', run)
    assert policy.checkout_worker_config(Path('.'), env={}) == []


@pytest.mark.parametrize('file_count,worker_count', [(120, 4), (2, 0)])
def test_real_parallel_checkout_preserves_content_modes_and_hooks(
        repo, tmp_path, monkeypatch, file_count, worker_count):
    root, env = repo
    resources(monkeypatch, cpu=4)
    for index in range(file_count):
        (root / f'f{index}').write_text(f'content {index}\n')
    (root / 'executable').write_text('#!/bin/sh\nexit 0\n')
    (root / 'executable').chmod(0o755)
    (root / 'link').symlink_to('f0')
    git(root, env, 'add', '.')
    git(root, env, 'commit', '-qm', 'base')
    marker = tmp_path / 'hook-ran'
    hook = root / '.git' / 'hooks' / 'post-checkout'
    hook.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    hook.chmod(0o755)
    destination = tmp_path / 'work'
    git(root, env, '-c', 'core.hooksPath=' + os.devnull,
        'worktree', 'add', '--no-checkout', '--detach', str(destination), 'HEAD')
    trace = tmp_path / 'trace.jsonl'
    checkout_env = {**env, 'GIT_TRACE2_EVENT': str(trace)}
    result = run_worktree_add([
        policy.git_executable(), '-c', 'core.hooksPath=' + os.devnull,
        *policy.checkout_worker_config(destination, env=checkout_env),
        'checkout', '--force', '--detach', '--progress', 'HEAD',
    ], cwd=destination, env=checkout_env)
    assert result.returncode == 0
    assert not marker.exists()
    assert git(destination, env, 'status', '--porcelain') == ''
    assert git(destination, env, 'write-tree') == git(root, env, 'rev-parse', 'HEAD^{tree}')
    assert (destination / 'f1').read_text() == 'content 1\n'
    assert (destination / 'executable').stat().st_mode & 0o111
    assert (destination / 'link').is_symlink()
    events = [json.loads(line) for line in trace.read_text().splitlines()]
    children = [event for event in events if event.get('event') == 'child_start'
                and any('checkout--worker' in arg for arg in event.get('argv', []))]
    assert len(children) == worker_count
