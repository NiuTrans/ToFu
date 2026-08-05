"""tests/test_mcp_install_nonblocking.py — install flow over the ISOLATED env.

Regression guard for the "installing llm-mcp times out" bug lineage. The
original bug: a BLOCKING ``pip install`` ran inline on the single shared MCP
event loop, freezing every other server. The architecture has since changed
twice:

  1. pip install offloaded to a worker thread (the fix that suite pinned);
  2. pip-into-shared-interpreter REMOVED entirely (pt_9345a80f417d43ca) —
     vendored servers now launch isolated via
     ``uv run --no-project --with-editable <src>``. There is no longer any
     blocking install call inside the bridge: launch-arg resolution is pure
     path stat'ing, and the dependency resolve happens INSIDE the spawned
     child process (async) or in ``prewarm_vendored_launcher`` (worker
     thread).

These tests pin the CURRENT contract:

  * the bridge performs no blocking subprocess/pip call before spawn;
  * prewarm runs the uv warm and surfaces success / failure faithfully;
  * start_install_job stays async, pollable, and idempotent.

Run:  pytest tests/test_mcp_install_nonblocking.py -m unit
"""
from __future__ import annotations

import shutil
import threading
import time

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge, prewarm_vendored_launcher

pytestmark = pytest.mark.unit


def _register_fake_vendor(tmp_path, monkeypatch, command='fake-mcp', layout='sibling'):
    """Register a vendored launcher backed by a real (empty) source dir."""
    repo = tmp_path / 'repo'
    repo.mkdir(exist_ok=True)
    if layout == 'sibling':
        src = tmp_path / command
        rel = f'../{command}'
    else:
        src = repo / 'tools' / command
        rel = f'tools/{command}'
    src.mkdir(parents=True)
    (src / 'pyproject.toml').write_text('[project]\nname="x"\n')
    monkeypatch.setattr(mc, '_repo_root', lambda: str(repo))
    monkeypatch.setitem(mc._VENDORED_LAUNCHERS, command, {'sources': [rel]})
    monkeypatch.setattr(mc, '_install_last_error', {})
    monkeypatch.setattr(mc, '_install_jobs', {})
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    return str(src), str(repo)


def test_server_owner_performs_no_blocking_subprocess_before_spawn():
    """The owner task must not run a BLOCKING subprocess before spawning.

    The original freeze came from a blocking pip inside ``_server_owner`` on
    the shared event loop. After the isolation migration there is no install
    step in the owner at all — pin that structurally so a future "just call
    the installer here" convenience reintroduces the freeze loudly.
    """
    import inspect

    from lib.mcp.client._bridge import MCPBridge  # noqa: F811

    src = inspect.getsource(MCPBridge._server_owner)
    assert 'subprocess.run' not in src, (
        '_server_owner gained a blocking subprocess.run — that is the '
        'event-loop freeze this suite exists to prevent'
    )
    assert '_try_autoinstall_launcher' not in src, (
        'the deleted pip auto-install path is referenced again'
    )


def test_bridge_translates_vendored_command_to_uv(tmp_path, monkeypatch):
    """A vendored bare command must become the isolated uv argv before spawn."""
    src, _repo = _register_fake_vendor(tmp_path, monkeypatch, 'fake-mcp')

    argv = mc.vendored_launch_argv('fake-mcp')
    assert argv is not None, 'registered vendored command did not translate'
    assert argv[:4] == ['uv', 'run', '--no-project', '--with-editable']
    assert argv[4] == src, (
        f'translation must point at the registered source {src}, got {argv[4]}'
    )
    assert argv[-1] == 'fake-mcp'

    # And a command with NO source stays untranslated (connect will surface
    # the install hint instead of launching anything).
    monkeypatch.setattr(mc, '_find_vendored_source', lambda c: None)
    assert mc.vendored_launch_argv('fake-mcp') is None


def test_prewarm_runs_uv_warm_and_reports_failure(tmp_path, monkeypatch):
    """prewarm_vendored_launcher runs the uv warm and surfaces rc/stderr."""
    src, _repo = _register_fake_vendor(tmp_path, monkeypatch, 'warm-mcp', layout='vendored')

    calls = {'n': 0}
    seen = {}

    class _R:
        returncode = 0
        stdout = 'ok'
        stderr = ''

    def _fake_run(args, **kw):
        calls['n'] += 1
        seen['args'] = args
        return _R()

    monkeypatch.setattr(mc.subprocess, 'run', _fake_run)
    ready, detail = prewarm_vendored_launcher('warm-mcp')
    assert ready is True
    assert calls['n'] == 1
    warm = seen['args']
    assert warm[:4] == ['uv', 'run', '--no-project', '--with-editable']
    assert warm[4] == src
    assert warm[-3:] == ['python', '-c', 'import warm_mcp'], (
        f'the warm must import the server package, got {warm[-3:]}'
    )
    assert 'uv run' in detail

    # Failure path: non-zero rc → reason propagated + stored.
    class _F:
        returncode = 1
        stdout = ''
        stderr = 'ResolutionImpossible: boom'

    monkeypatch.setattr(mc.subprocess, 'run', lambda *a, **k: _F())
    ready, detail = prewarm_vendored_launcher('warm-mcp')
    assert ready is False
    assert 'boom' in detail
    assert mc._install_last_error.get('warm-mcp'), (
        'failure reason must be stored for the connect-error hint'
    )


def test_prewarm_noop_for_non_vendored():
    """Non-vendored commands are a no-op — nothing to warm."""
    ready, detail = prewarm_vendored_launcher('definitely-not-vendored')
    assert ready is True
    assert detail == ''
    ready, detail = prewarm_vendored_launcher('/abs/path/to/thing')
    assert ready is True
    assert detail == ''


def test_start_install_job_async_and_pollable(tmp_path, monkeypatch):
    """start_install_job returns immediately while the warm runs in background."""
    _register_fake_vendor(tmp_path, monkeypatch, 'job-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: None)

    release = threading.Event()

    def _slow_prewarm(command):
        release.wait(timeout=5)
        return True, f'uv run --with-editable /x {command}'

    monkeypatch.setattr(mc, 'prewarm_vendored_launcher', _slow_prewarm)

    t0 = time.monotonic()
    job = mc.start_install_job('job-mcp')
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5
    assert job['state'] == 'installing'
    assert mc.get_install_job('job-mcp')['state'] == 'installing'

    # A second click while installing re-attaches instead of spawning a
    # second warm (the job-state dedup replaces the old per-command pip lock).
    again = mc.start_install_job('job-mcp')
    assert again['state'] == 'installing'

    release.set()
    for _ in range(50):
        if mc.get_install_job('job-mcp')['state'] != 'installing':
            break
        time.sleep(0.05)
    final = mc.get_install_job('job-mcp')
    assert final['state'] == 'ready'
    assert 'uv run' in final['detail']


def test_start_install_job_ready_fast_path_for_non_vendored(tmp_path, monkeypatch):
    """A NON-vendored launcher already on PATH short-circuits to ready."""
    monkeypatch.setattr(shutil, 'which', lambda c: f'/usr/bin/{c}')

    def _must_not_run(command):
        raise AssertionError('prewarm should not run for a non-vendored on-PATH command')

    monkeypatch.setattr(mc, 'prewarm_vendored_launcher', _must_not_run)
    job = mc.start_install_job('some-foreign-tool')
    assert job['state'] == 'ready'


def test_start_install_job_warms_vendored_even_when_on_path(tmp_path, monkeypatch):
    """A VENDORED command on PATH must STILL warm — that copy may be the stale
    coupled pip-era install, and the bridge launches the isolated env anyway.
    """
    _register_fake_vendor(tmp_path, monkeypatch, 'fast-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: f'/usr/bin/{c}')

    calls = {'n': 0}
    release = threading.Event()

    def _quick_prewarm(command):
        calls['n'] += 1
        release.wait(timeout=5)
        return True, 'uv run --with-editable /x fast-mcp'

    monkeypatch.setattr(mc, 'prewarm_vendored_launcher', _quick_prewarm)
    job = mc.start_install_job('fast-mcp')
    assert job['state'] == 'installing', (
        'an on-PATH vendored command must NOT skip the uv warm'
    )
    release.set()
    for _ in range(50):
        if mc.get_install_job('fast-mcp')['state'] != 'installing':
            break
        time.sleep(0.05)
    assert mc.get_install_job('fast-mcp')['state'] == 'ready'
    assert calls['n'] == 1
