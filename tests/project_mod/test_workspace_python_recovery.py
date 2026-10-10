"""Unavailable Python must not disable diagnostics or contaminate server packages."""
import os
from pathlib import Path
import subprocess
import sys
import venv

import pytest

from lib.project_mod.run_command import _get_cmd_env, tool_run_command
from lib.project_mod.workspace_runtime import prepare_workspace_runtime

pytestmark = [pytest.mark.unit, pytest.mark.skipif(os.name == 'nt', reason='POSIX shell launchers')]


@pytest.fixture(autouse=True)
def private_cache(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_MCP_CACHE_DIR', str(tmp_path / 'cache'))


def broken_environment(root):
    environment = root / '.venv'
    environment.mkdir(parents=True)
    (environment / 'pyvenv.cfg').write_text('home = /unavailable/python\n')
    return environment


@pytest.mark.parametrize('failure', ['missing', 'broken-link', 'non-executable'])
def test_diagnostics_run_but_python_and_pip_do_not_fall_through(tmp_path, failure):
    environment = broken_environment(tmp_path)
    if failure != 'missing':
        (environment / 'bin').mkdir()
        python = environment / 'bin/python'
        if failure == 'broken-link':
            python.symlink_to('/unavailable/python')
        else:
            python.write_text('#!/bin/sh\necho incorrect-python\n')
    sub = tmp_path / 'sub'
    sub.mkdir()
    subprocess.run(['git', 'init', str(tmp_path)], check=True, capture_output=True)
    child = _get_cmd_env(str(sub), project_root=str(tmp_path))
    output = subprocess.run(['sh', '-c', 'pwd; date --iso-8601=seconds; git rev-parse --show-toplevel; git status --short; echo diagnostics-ok'],
                            cwd=sub, env=child, capture_output=True, text=True, check=True)
    assert 'diagnostics-ok' in output.stdout
    for name in ('python', 'python3', 'pip', 'pip3', Path(sys.executable).name):
        result = subprocess.run(['sh', '-c', f'{name} --version'], env=child,
                                capture_output=True, text=True)
        assert result.returncode == 127
        assert 'Project .venv is incomplete' in result.stderr
    assert 'VIRTUAL_ENV' not in child
    assert child['PIP_REQUIRE_VIRTUALENV'] == '1'
    # Environment repair is picked up on the next invocation, without restart.
    if (environment / 'bin/python').is_symlink() or (environment / 'bin/python').exists():
        (environment / 'bin/python').unlink()
    venv.EnvBuilder(with_pip=False).create(environment)
    repaired = _get_cmd_env(str(sub), project_root=str(tmp_path))
    assert repaired['VIRTUAL_ENV'] == str(environment)
    assert 'unavailable-project-python' not in repaired['PATH']


def test_broken_source_keeps_node_and_shell_working_in_private_workspace(tmp_path):
    source, work = tmp_path / 'source', tmp_path / 'work'
    broken_environment(source)
    work.mkdir()
    (source / 'node_modules/sample').mkdir(parents=True)
    assert prepare_workspace_runtime(str(source), str(work)) == ['.venv', 'node_modules']
    assert (work / 'node_modules/sample').is_dir()
    assert 'diagnostics-ok' in tool_run_command(str(work), 'echo diagnostics-ok')
    assert 'Project .venv is incomplete' in tool_run_command(str(work), 'python --version')
    assert not (source / '.venv/bin').exists()
    assert prepare_workspace_runtime(str(source), str(work)) == []


def test_healthy_task_environment_survives_broken_source(tmp_path):
    source, work = tmp_path / 'source', tmp_path / 'work'
    broken_environment(source)
    venv.EnvBuilder(with_pip=False).create(work / '.venv')
    assert prepare_workspace_runtime(str(source), str(work)) == []
    result = tool_run_command(str(work), 'python -c "print(42)"')
    assert '\n42\n' in result


@pytest.mark.parametrize('failing_stage', [1, 2, 3])
def test_failed_bridge_rolls_back_and_retry_reuses_source_packages(tmp_path, monkeypatch, failing_stage):
    source, work = tmp_path / 'source', tmp_path / 'work'
    venv.EnvBuilder(with_pip=False).create(source / '.venv')
    work.mkdir()
    packages = Path(subprocess.check_output([str(source / '.venv/bin/python'), '-c',
        'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip())
    (packages / 'borrowed_fixture.py').write_text('VALUE = 42\n')
    run = subprocess.run
    calls = 0
    def fail(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == failing_stage:
            (work / '.venv').mkdir(exist_ok=True)
            (work / '.venv/partial').write_text('partial bridge')
            raise subprocess.TimeoutExpired(args[0], 60)
        return run(*args, **kwargs)
    monkeypatch.setattr(subprocess, 'run', fail)
    with pytest.raises(subprocess.TimeoutExpired):
        prepare_workspace_runtime(str(source), str(work))
    assert not (work / '.venv').exists()
    assert (source / '.venv/pyvenv.cfg').is_file()
    monkeypatch.setattr(subprocess, 'run', run)
    assert prepare_workspace_runtime(str(source), str(work)) == ['.venv']
    assert _get_cmd_env(str(work))['VIRTUAL_ENV'] == str(work / '.venv')
    result = subprocess.check_output([str(work / '.venv/bin/python'), '-c',
        'import sys, borrowed_fixture; print(sys.prefix); print(borrowed_fixture.VALUE)'], text=True)
    assert result.splitlines() == [str(work / '.venv'), '42']


def test_restricted_broken_source_symlink_is_still_refused(tmp_path):
    source, work = tmp_path / 'source', tmp_path / 'work'
    source.mkdir()
    work.mkdir()
    outside = broken_environment(tmp_path / 'outside')
    (source / '.venv').symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='authorized environment root'):
        prepare_workspace_runtime(str(source), str(work), restricted=True)
    assert not (work / '.venv').exists()
