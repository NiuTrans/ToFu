"""Native capability failures are environment errors, never program authoring errors."""
import builtins
import subprocess
from types import SimpleNamespace

import pytest

from bootstrap_pkg import runtime_checks
from lib.tools.javascript import execute_javascript_program
from lib.tools.program_contract import ProgramExecutionError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('failure', [ImportError('wasmtime missing'),
    OSError("/lib64/libc.so.6: version `GLIBC_2.18' not found")])
def test_loader_failure_is_typed_before_any_tool_call(monkeypatch, failure):
    import lib.tools.javascript as javascript
    original_import = builtins.__import__
    def failing_import(name, *args, **kwargs):
        if name == 'wasmtime':
            raise failure
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(javascript, '_compiled', None)
    monkeypatch.setattr(builtins, '__import__', failing_import)
    calls = []
    with pytest.raises(ProgramExecutionError) as caught:
        execute_javascript_program('return 42;', search=lambda *args: [],
            call=lambda *args: calls.append(args), call_many=lambda *args: [])
    assert caught.value.code == 'runtime_unavailable'
    assert str(failure) in str(caught.value)
    assert 'changing tool arguments' in str(caught.value)
    assert calls == []


@pytest.mark.parametrize('returncode, stderr', [(0, ''), (1, 'GLIBC_2.18 not found'), (-11, '')])
def test_probe_checks_child_exit_and_preserves_selected_interpreter(monkeypatch, tmp_path, returncode, stderr):
    def run(command, **kwargs):
        assert command[0] == '/selected/python'
        assert kwargs['cwd'] == tmp_path
        assert kwargs['timeout'] == 30
        assert kwargs['capture_output']
        return SimpleNamespace(returncode=returncode, stdout='smoke', stderr=stderr)
    monkeypatch.setattr(runtime_checks.subprocess, 'run', run)
    healthy, detail = runtime_checks.check_javascript_runtime(python='/selected/python', root=tmp_path)
    assert healthy is (returncode == 0)
    if returncode:
        assert f'exit {returncode}' in detail
        assert stderr in detail


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('probe', 30), OSError('missing interpreter')])
def test_probe_reports_timeout_or_spawn_failure(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure
    monkeypatch.setattr(runtime_checks.subprocess, 'run', run)
    healthy, detail = runtime_checks.check_javascript_runtime()
    assert not healthy and detail
