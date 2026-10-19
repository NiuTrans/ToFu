"""Native delivery recipe fails closed; installed health probe needs no checkout."""
import importlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', ['none', 'install', 'probe'])
def test_every_role_must_install_and_certify_before_build_continues(tmp_path, failure):
    docker = (ROOT / 'Dockerfile').read_text()
    fragment = docker.split('&& for environment', 1)[1].split('done', 1)[0]
    script = ('for environment' + fragment + 'done').replace('\\\n', '')
    trace = tmp_path / 'trace'
    executable = tmp_path / 'bin'; executable.mkdir()
    uv = executable / 'uv'
    uv.write_text('#!/bin/sh\n'
                  'case "$*" in\n'
                  '  *"/api/bin/python"*) role=api ;;\n'
                  '  *"/worker/bin/python"*) role=worker ;;\n'
                  '  *"/agent/bin/python"*) role=agent ;;\n'
                  '  *) exit 9 ;;\n'
                  'esac\n'
                  'echo "install_$role" >> "$NATIVE_ROLE_TRACE"\n'
                  'if [ "$NATIVE_ROLE_FAILURE" = install ] && [ "$role" = worker ]; then exit 7; fi\n')
    uv.chmod(0o755)
    for role in ['api', 'worker', 'agent']:
        directory = tmp_path / role
        (directory / 'bin').mkdir(parents=True)
        python = directory / 'bin/python'
        python.write_text('#!/bin/sh\n'
                          f'echo probe_{role} >> "$NATIVE_ROLE_TRACE"\n'
                          f'if [ "$NATIVE_ROLE_FAILURE" = probe ] && [ {role} = worker ]; then exit 8; fi\n')
        python.chmod(0o755)
        script = script.replace('/opt/tofu-' + role, shlex.quote(str(directory)))
    environment = {**os.environ, 'PATH': str(executable) + os.pathsep + os.environ['PATH'],
                   'NATIVE_ROLE_TRACE': str(trace), 'NATIVE_ROLE_FAILURE': failure}
    result = subprocess.run(['/bin/sh', '-c', script], env=environment,
                            capture_output=True, text=True, timeout=5)
    calls = trace.read_text().splitlines()
    if failure == 'none':
        assert result.returncode == 0
        assert calls == ['install_api', 'probe_api', 'install_worker', 'probe_worker', 'install_agent', 'probe_agent']
    else:
        assert result.returncode != 0
        assert calls == (['install_api', 'probe_api', 'install_worker'] if failure == 'install'
                         else ['install_api', 'probe_api', 'install_worker', 'probe_worker'])


def test_kernel_readme_survives_docker_context_filter():
    assert '!packages/tofu-repo-kernel/README.md' in (ROOT / '.dockerignore').read_text()


def test_installed_kernel_probe_runs_outside_checkout(tmp_path):
    pytest.importorskip('tofu_repo_kernel.__main__')
    python = os.environ.get('TOFU_BENCHMARK_PYTHON', sys.executable)
    result = subprocess.run([python, '-m', 'tofu_repo_kernel'], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'native repository ranking, discovery and stream artifacts ready'


def test_installed_probe_rejects_incorrect_computation(monkeypatch):
    probe = pytest.importorskip('tofu_repo_kernel.__main__')
    class BadKernel:
        def solve(self, *args): return [0.0]
    monkeypatch.setattr(probe, 'load_kernel', lambda: BadKernel())
    with pytest.raises(RuntimeError, match='installed-artifact probe'):
        probe.main()


@pytest.mark.parametrize('compiler', [None, 'gcc', 'rustc'])
def test_platform_image_gate_rejects_runtime_compilers(tmp_path, compiler):
    workflow = (ROOT / '.github/workflows/publish-developer-runtime.yml').read_text()
    match = re.search(r"sh -eu -c\s*\\\n\s*'([^']+)'", workflow)
    assert match is not None
    script = match.group(1)
    trace = tmp_path / 'trace'
    python = tmp_path / 'python'
    python.write_text('#!/bin/sh\necho probe >> "$NATIVE_ROLE_TRACE"\n')
    python.chmod(0o755)
    if compiler:
        tool = tmp_path / compiler
        tool.write_text('#!/bin/sh\nexit 0\n'); tool.chmod(0o755)
    result = subprocess.run(['/bin/sh', '-eu', '-c', script],
        env={**os.environ, 'PATH': str(tmp_path), 'NATIVE_ROLE_TRACE': str(trace)},
        capture_output=True, text=True, timeout=5)
    if compiler:
        assert result.returncode != 0
        assert not trace.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert trace.read_text().splitlines() == ['probe', 'probe']
