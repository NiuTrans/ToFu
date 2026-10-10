"""Benchmark shell scripts must not mutate the caller when /testbed is invalid."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess
from unittest import mock

import pytest

from benchmarks.context_efficiency import runtime
from tests.context.test_context_efficiency_benchmark import _task

pytestmark = pytest.mark.unit


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


def repository(path):
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    (path / 'tracked').write_text('committed\n')
    git(path, 'add', 'tracked')
    git(path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
        'commit', '-qm', 'fixture')
    return git(path, 'rev-parse', 'HEAD')


def generated_script(kind, task, tmp_path, monkeypatch):
    if kind == 'evaluation':
        monkeypatch.setattr(runtime, '_PROOT_SHIM', '')
        monkeypatch.setattr(runtime, 'ensure_container', lambda _: ('fixture', ''))
        monkeypatch.setattr(runtime, '_udocker', lambda *a, **k: mock.Mock(
            returncode=1, stdout='Could not download gradle-wrapper.jar UnknownHostException'))
        runtime.evaluate_patch(task, 'synthetic patch', tmp_path / 'run')
        return (tmp_path / 'run/eval/run.sh').read_text().split('APPLIED=0')[0]
    reset, diff = runtime._base_reset_and_diff(task)
    return reset if kind == 'reset' else diff


@pytest.mark.parametrize('kind', ['reset', 'diff', 'evaluation'])
@pytest.mark.parametrize('target_kind', ['missing', 'nested_non_repository', 'external_git_directory'])
def test_invalid_testbed_never_mutates_callers_repository(tmp_path, monkeypatch, kind, target_kind):
    caller = tmp_path / 'caller'
    task = replace(_task(1), base_commit=repository(caller))
    (caller / 'tracked').write_text('user work\n')
    (caller / 'untracked').write_text('keep untracked\n')
    target = caller / 'nested' if target_kind == 'nested_non_repository' else tmp_path / 'missing'
    if target_kind == 'nested_non_repository': target.mkdir()
    if target_kind == 'external_git_directory':
        target.mkdir()
        (target / '.git').symlink_to(caller / '.git', target_is_directory=True)
    host = tmp_path / 'host'; host.mkdir()
    script = generated_script(kind, task, tmp_path, monkeypatch)
    script = script.replace('/testbed', str(target)).replace('/host', str(host))
    before = git(caller, 'status', '--porcelain')
    result = subprocess.run(['bash', '-c', script], cwd=caller, text=True,
                            capture_output=True, timeout=10)
    assert (caller / 'tracked').read_text() == 'user work\n'
    assert (caller / 'untracked').read_text() == 'keep untracked\n'
    assert git(caller, 'status', '--porcelain') == before
    assert result.returncode != 0
    assert 'CTXEFF_TESTBED_GUARD_FAILED' in result.stderr


@pytest.mark.parametrize('kind', ['reset', 'diff', 'evaluation'])
def test_valid_testbed_ignores_inherited_git_routing(tmp_path, monkeypatch, kind):
    caller = tmp_path / 'caller'; repository(caller)
    (caller / 'tracked').write_text('caller work\n')
    (caller / 'untracked').write_text('caller new file\n')
    target = tmp_path / 'target'
    task = replace(_task(1), base_commit=repository(target))
    (target / 'tracked').write_text('target edit\n')
    (target / 'new-source').write_text('target new file\n')
    host = tmp_path / 'host'; host.mkdir()
    script = generated_script(kind, task, tmp_path, monkeypatch)
    script = script.replace('/testbed', str(target)).replace('/host', str(host))
    before = git(caller, 'status', '--porcelain')
    result = subprocess.run(['bash', '-c', script], cwd=caller, text=True,
        capture_output=True, timeout=10, env={**os.environ,
            'GIT_DIR': str(caller / '.git'), 'GIT_WORK_TREE': str(caller),
            'GIT_INDEX_FILE': str(caller / '.git/index')})
    assert result.returncode == 0, result.stderr
    assert git(caller, 'status', '--porcelain') == before
    assert (caller / 'tracked').read_text() == 'caller work\n'
    assert (caller / 'untracked').exists()
    if kind == 'diff':
        patch = (host / 'model_patch.diff').read_text()
        assert '+target edit' in patch and '+target new file' in patch
    else:
        assert (target / 'tracked').read_text() == 'committed\n'
        assert not (target / 'new-source').exists()


@pytest.mark.parametrize('kind', ['reset', 'diff', 'evaluation'])
def test_invalid_base_commit_fails_before_mutation(tmp_path, monkeypatch, kind):
    target = tmp_path / 'target'; repository(target)
    (target / 'tracked').write_text('keep edit\n')
    (target / 'untracked').write_text('keep new file\n')
    task = replace(_task(1), base_commit='--not-a-commit')
    host = tmp_path / 'host'; host.mkdir()
    script = generated_script(kind, task, tmp_path, monkeypatch)
    script = script.replace('/testbed', str(target)).replace('/host', str(host))
    before = git(target, 'status', '--porcelain')
    result = subprocess.run(['bash', '-c', script], cwd=tmp_path, text=True,
                            capture_output=True, timeout=10)
    assert result.returncode != 0
    assert 'CTXEFF_TESTBED_GUARD_FAILED' in result.stderr
    assert git(target, 'status', '--porcelain') == before
    assert (target / 'tracked').read_text() == 'keep edit\n'
    assert (target / 'untracked').exists()


def test_testbed_guard_failure_is_infrastructure_error(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, 'ensure_container', lambda _: ('fixture', ''))
    monkeypatch.setattr(runtime, '_udocker', lambda *a, **k: mock.Mock(
        returncode=86, stdout='CTXEFF_TESTBED_GUARD_FAILED'))
    result = runtime.evaluate_patch(_task(1), 'synthetic patch', tmp_path / 'run')
    assert result.infrastructure_error
    assert not result.patch_applies
    assert 'safety guard' in result.error
