#!/usr/bin/env python3
"""Tests for bootstrap.py's external-kill (SIGKILL) handling.

A SIGKILLed server must NOT enter the LLM dependency-repair loop (the empty
stderr would be 'diagnosed' as nothing) — it must be recorded and relaunched
with backoff, handing the caller the real (stderr, rc) only when a relaunched
server dies of something OTHER than SIGKILL.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_bootstrap_external_kill.py -v
"""

import importlib.util
import os
import socket
import stat
import sys

import pytest

import bootstrap_pkg.launcher as _launcher

pytestmark = pytest.mark.unit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def boot():
    """Import bootstrap.py as a module (it's a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        'tofu_bootstrap', os.path.join(ROOT, 'bootstrap.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── _is_external_kill ──

def test_is_external_kill_signals(boot):
    assert boot._is_external_kill(-9) is True    # subprocess returncode for SIGKILL
    assert boot._is_external_kill(137) is True   # shell-style 128+9
    assert boot._is_external_kill(0) is False
    assert boot._is_external_kill(1) is False
    assert boot._is_external_kill(-15) is False  # SIGTERM = intentional stop
    assert boot._is_external_kill(143) is False


# ── _restart_after_external_kill ──

def test_relaunch_until_real_crash(boot, monkeypatch, tmp_path):
    """SIGKILL ×2 then a real import error → 3 launches, real (stderr, rc) returned."""
    monkeypatch.setattr(_launcher, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(boot.time, 'sleep', lambda s: None)
    launches = {'n': 0}

    def fake_start(*a, **k):
        launches['n'] += 1
        if launches['n'] <= 2:
            return False, '', -9                    # killed again
        return False, 'ModuleNotFoundError: foo', 1  # real crash
    monkeypatch.setattr(_launcher, '_try_start_server', fake_start)

    stderr, rc = boot._restart_after_external_kill(-9)
    assert launches['n'] == 3
    assert (stderr, rc) == ('ModuleNotFoundError: foo', 1)
    # evidence written to logs/watchdog.log — one line per kill: the entry
    # kill + 2 relaunch kills = 3.
    wlog = tmp_path / 'logs' / 'watchdog.log'
    assert wlog.exists()
    text = wlog.read_text()
    assert text.count('SIGKILLed') == 3
    assert stat.S_IMODE(wlog.stat().st_mode) == 0o600


def test_relaunch_budget_exhausted_exits(boot, monkeypatch, tmp_path):
    """Always-SIGKILL → gives up after max_relaunches with SystemExit(137)."""
    monkeypatch.setattr(_launcher, 'BASE_DIR', str(tmp_path))
    monkeypatch.setattr(boot.time, 'sleep', lambda s: None)
    launches = {'n': 0}

    def fake_start(*a, **k):
        launches['n'] += 1
        return False, '', -9
    monkeypatch.setattr(_launcher, '_try_start_server', fake_start)

    with pytest.raises(SystemExit) as ei:
        boot._restart_after_external_kill(-9, max_relaunches=3)
    assert ei.value.code == 137
    assert launches['n'] == 3


def test_backoff_is_linear_and_capped(boot, monkeypatch, tmp_path):
    monkeypatch.setattr(_launcher, 'BASE_DIR', str(tmp_path))
    sleeps = []
    monkeypatch.setattr(boot.time, 'sleep', lambda s: sleeps.append(s))
    monkeypatch.setattr(_launcher, '_try_start_server',
                        lambda *a, **k: (False, 'real', 1))
    boot._restart_after_external_kill(137)   # shell-style also accepted
    assert sleeps == [5]                     # attempt 1 → 5s backoff


def test_neuter_sigkill_without_branch_would_repair(boot):
    """NEUTER: documents why the branch exists — -9/137 must never look like a
    dependency error worth feeding to the LLM repair loop."""
    assert not boot._is_import_or_package_error('')
    # A SIGKILL leaves EMPTY stderr; if main() skipped the external-kill
    # branch, this empty stderr is exactly what the repair loop would get.
    # The branch exists so that can never happen.


def test_busy_configured_port_fails_without_start_or_implicit_shift(
        boot, monkeypatch, capsys):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        monkeypatch.setattr(
            _launcher.runtime,
            '_get_config',
            lambda: {
                'host': '127.0.0.1',
                'port': port,
                'api_keys': [],
            },
        )
        monkeypatch.setattr(
            _launcher,
            '_try_start_server',
            lambda *args, **kwargs: pytest.fail(
                'busy endpoint reached server startup'),
        )

        with pytest.raises(SystemExit) as exc_info:
            boot.main()

    assert exc_info.value.code == 1
    stderr = capsys.readouterr().err
    assert f'Cannot bind configured endpoint 127.0.0.1:{port}' in stderr
    assert 'Refusing to switch ports implicitly' in stderr
    assert 'serverctl.py doctor' in stderr
    assert 'change PORT explicitly' in stderr
    assert 'auto-switched' not in stderr


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
