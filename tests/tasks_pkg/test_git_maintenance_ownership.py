"""No model- or harness-run Git command starts automatic maintenance.

Automatic ``gc --auto`` either escapes the command's process group with its
inherited workspace leases (detached) or runs unbounded inside the command
(foreground). Maintenance is the explicit scripts/git_maintenance.py step.
"""
import hashlib
import os
import subprocess

import pytest

from lib import git_checkpoint_policy, git_runtime
from lib.project_mod import workspace_lease
from lib.project_mod import run_command as commands

pytestmark = pytest.mark.unit
IDENTITY = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
            'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def git(repo, *args, env):
    return subprocess.run([git_runtime.git_executable(), '-C', str(repo), *args], env=env,
                          text=True, capture_output=True, timeout=60, check=False)


def read_git_setting(environment, name, *options):
    result = subprocess.run([git_runtime.git_executable(), *options, 'config', '--get', name],
                            env=environment, text=True, capture_output=True, check=True)
    return result.stdout.strip()


@pytest.fixture
def repository_due_for_gc(tmp_path):
    """A repository whose next commit starts automatic maintenance.

    Mirrors the shared checkout: repository-level ``gc.autoDetach=false`` and
    more loose objects in objects/17 than ``gc.auto`` allows. The pre-auto-gc
    hook records that Git decided to collect, then vetoes the work itself.
    """
    home = tmp_path / 'home'
    home.mkdir()
    repo = tmp_path / 'repo'
    plain = {key: value for key, value in os.environ.items() if not key.upper().startswith('GIT_')}
    plain.update(IDENTITY, HOME=str(home), GIT_CONFIG_NOSYSTEM='1')
    assert git(tmp_path, 'init', '-q', str(repo), env=plain).returncode == 0
    for name, value in (('gc.auto', '1'), ('gc.autoDetach', 'false')):
        assert git(repo, 'config', name, value, env=plain).returncode == 0
    written, attempt = 0, 0
    while written < 2:
        attempt += 1
        content = f'loose {attempt}\n'.encode()
        if hashlib.sha1(b'blob %d\0' % len(content) + content).hexdigest().startswith('17'):
            subprocess.run([git_runtime.git_executable(), '-C', str(repo), 'hash-object', '-w',
                            '--stdin'], input=content, env=plain, capture_output=True,
                           timeout=60, check=True)
            written += 1
    marker = tmp_path / 'auto-gc-started'
    hook = repo / '.git' / 'hooks' / 'pre-auto-gc'
    hook.write_text(f'#!/bin/sh\necho started >> "{marker}"\nexit 1\n')
    hook.chmod(0o755)
    return repo, plain, marker


def test_plain_git_commit_starts_automatic_maintenance(repository_due_for_gc, tmp_path):
    repo, plain, marker = repository_due_for_gc
    trace = tmp_path / 'trace'
    result = git(repo, 'commit', '--allow-empty', '-q', '-m', 'control',
                 env={**plain, 'GIT_TRACE': str(trace)})
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == 'started\n'
    # Git 2.29+ asks `maintenance run --auto`, which consults the hook itself.
    assert any(child in trace.read_text() for child in ('gc --auto', 'maintenance run --auto'))


def test_harness_git_writers_never_start_automatic_maintenance(
        repository_due_for_gc, tmp_path, monkeypatch):
    repo, plain, marker = repository_due_for_gc
    monkeypatch.setattr(os, 'environ', dict(plain, GIT_CONFIG_PARAMETERS="'tofu.ambient=dropped'"))
    environment = git_checkpoint_policy.git_policy_environment()
    assert 'tofu.ambient' not in environment['GIT_CONFIG_PARAMETERS']
    trace = tmp_path / 'trace'
    environment.update(IDENTITY, GIT_CONFIG_NOSYSTEM='1', GIT_TRACE=str(trace))
    result = git(repo, 'commit', '--allow-empty', '-q', '-m', 'policy', env=environment)
    assert result.returncode == 0, result.stderr
    assert 'gc --auto' not in trace.read_text()
    assert 'maintenance run --auto' not in trace.read_text()
    # Git before 2.29 calls gc --auto directly from its writers.
    assert git(repo, 'gc', '--auto', '--quiet', env=environment).returncode == 0
    assert not marker.exists()


def test_policy_is_idempotent_preserves_parameters_and_yields_to_explicit_options(tmp_path):
    caller = {'HOME': str(tmp_path), 'GIT_CONFIG_NOSYSTEM': '1',
              'GIT_CONFIG_PARAMETERS': "'tofu.test-label=retained value'"}
    before = dict(caller)
    environment = git_runtime.without_automatic_git_maintenance(caller)
    assert caller == before
    assert git_runtime.without_automatic_git_maintenance(environment) == environment
    assert read_git_setting(environment, 'tofu.test-label') == 'retained value'
    assert read_git_setting(environment, 'gc.auto') == '0'
    assert read_git_setting(environment, 'maintenance.auto') == 'false'
    assert read_git_setting(environment, 'gc.autoDetach') == 'false'
    assert read_git_setting(environment, 'maintenance.autoDetach') == 'false'
    assert read_git_setting(environment, 'gc.auto', '-c', 'gc.auto=7') == '7'
    later = dict(environment, GIT_CONFIG_PARAMETERS=environment['GIT_CONFIG_PARAMETERS']
                 + " 'gc.auto=9'")
    assert read_git_setting(git_runtime.without_automatic_git_maintenance(later), 'gc.auto') == '0'


@pytest.mark.parametrize('leased', [False, True])
@pytest.mark.parametrize('interactive', [False, True])
def test_every_model_command_runs_without_automatic_git_maintenance(
        monkeypatch, tmp_path, interactive, leased):
    environment = dict(os.environ, GIT_CONFIG_PARAMETERS="'tofu.test-label=retained value'")
    before = dict(environment)
    captured = {}

    def spawn(*args, **kwargs):
        captured.update(kwargs)
        raise FileNotFoundError('synthetic spawn failure after environment capture')

    monkeypatch.setattr(workspace_lease, 'workspace_lease_fds',
                        lambda: (123,) if leased else ())
    monkeypatch.setattr(commands.subprocess, 'Popen', spawn)
    if interactive:
        commands._run_command_interactive('git commit -qm x', 'git commit -qm x', 10,
                                         str(tmp_path), lambda *args: '', child_env=environment)
    else:
        commands._run_command_simple('git commit -qm x', 'git commit -qm x', 10,
                                    str(tmp_path), child_env=environment)
    monkeypatch.undo()
    assert captured['pass_fds'] == ((123,) if leased else ())
    assert environment == before
    assert read_git_setting(captured['env'], 'gc.auto') == '0'
    assert read_git_setting(captured['env'], 'maintenance.auto') == 'false'
    assert read_git_setting(captured['env'], 'tofu.test-label') == 'retained value'
