"""tests/test_mcp_install_nonblocking.py — slow install must not freeze the loop.

Regression guard for the "installing llm-mcp times out" bug. The first-connect
auto-install runs a BLOCKING ``subprocess.run`` (pip, up to 300s). It used to
be called inline inside the ``_server_owner`` coroutine, on the single shared
MCP event loop — so a cold install froze EVERY other server's keepalive /
tool-calls / connects, and the front-end's 30s fetch aborted with a useless
"timeout".

The fix offloads ``_try_autoinstall_launcher`` to a worker thread via
``loop.run_in_executor`` (mirroring ``_reconnect_server``). These tests prove:

  1. While a SLOW fake pip is "installing", a concurrent coroutine on the same
     loop keeps making progress (the loop is not blocked).
  2. The new ``prewarm_vendored_launcher`` helper (run by the install route in
     a Flask worker thread) installs a vendored launcher and reports failure
     reasons faithfully.

Run:  pytest tests/test_mcp_install_nonblocking.py -m unit
"""
from __future__ import annotations

import asyncio
import shutil
import threading
import time

import pytest

import lib.mcp.client as mc
from lib.mcp.client import MCPBridge, prewarm_vendored_launcher

pytestmark = pytest.mark.unit


def _register_fake_vendor(tmp_path, monkeypatch, command='slow-mcp', layout='sibling'):
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
    monkeypatch.setattr(mc, '_install_attempted', set())
    monkeypatch.setattr(mc, '_install_last_error', {})
    monkeypatch.setattr(mc, '_install_cmd_locks', {})
    monkeypatch.setattr(mc, '_install_jobs', {})
    monkeypatch.setattr(mc, '_vendored_mtime', float('inf'))
    return str(src), str(repo)


def test_slow_install_does_not_block_event_loop(tmp_path, monkeypatch):
    """A slow auto-install inside _server_owner must run off the loop.

    We drive ``_async_start_owner`` for a vendored command that is NOT on PATH,
    with ``_try_autoinstall_launcher`` monkeypatched to BLOCK (time.sleep) like
    a cold pip. Concurrently we run a 'heartbeat' coroutine that increments a
    counter every 10ms. If the install were inline on the loop, the heartbeat
    would freeze for the whole sleep; with the run_in_executor offload it keeps
    ticking.
    """
    _register_fake_vendor(tmp_path, monkeypatch, 'slow-mcp')

    INSTALL_SECS = 0.6
    install_started = threading.Event()

    def _slow_pip(command):
        install_started.set()
        time.sleep(INSTALL_SECS)        # blocking, like a real pip
        return None                     # pretend install failed → connect raises

    monkeypatch.setattr(mc, '_try_autoinstall_launcher', _slow_pip)
    # Force the "not on PATH / not resolvable" branch so the owner reaches the
    # auto-install fallback. client.py does a local `import shutil`, so patch
    # the real module's `which`.
    monkeypatch.setattr(shutil, 'which', lambda c: None)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: None)

    async def _drive():
        loop = asyncio.get_running_loop()
        bridge = MCPBridge()

        ticks = {'n': 0}

        async def _heartbeat():
            # Keep ticking until the connect attempt resolves.
            while True:
                ticks['n'] += 1
                await asyncio.sleep(0.01)

        hb = loop.create_task(_heartbeat())

        cfg = {'transport': 'stdio', 'command': 'slow-mcp', 'args': []}
        t0 = time.monotonic()
        with pytest.raises(Exception):
            # Install fails (returns None) → owner raises MCPConnectError /
            # FileNotFoundError. We only care that the loop stayed alive.
            await bridge._async_start_owner('slow-mcp', cfg)
        elapsed = time.monotonic() - t0

        hb.cancel()
        return ticks['n'], elapsed

    ticks, elapsed = asyncio.run(_drive())

    # The install blocked for ~INSTALL_SECS; the heartbeat fires every 10ms.
    # If the loop were frozen, ticks would be ~0. With the offload it should
    # tick many times during the install window.
    assert elapsed >= INSTALL_SECS, (
        f'install returned too fast ({elapsed:.2f}s) — slow path not exercised')
    assert ticks >= 10, (
        f'event loop appears blocked during install: only {ticks} heartbeat '
        f'ticks in {elapsed:.2f}s (expected many)')


def test_prewarm_installs_vendored_and_reports_failure(tmp_path, monkeypatch):
    """prewarm_vendored_launcher runs pip and surfaces success / failure."""
    _register_fake_vendor(tmp_path, monkeypatch, 'warm-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: None)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: None)

    # Success path: pip "succeeds" and the launcher resolves afterwards.
    calls = {'n': 0}

    def _fake_install(command):
        calls['n'] += 1
        return f'/env/bin/{command}'

    monkeypatch.setattr(mc, '_try_autoinstall_launcher', _fake_install)
    ready, detail = prewarm_vendored_launcher('warm-mcp')
    assert ready is True
    assert detail == '/env/bin/warm-mcp'
    assert calls['n'] == 1

    # Failure path: pip fails → reason is propagated from _install_last_error.
    def _fail_install(command):
        with mc._install_lock:
            mc._install_last_error[command] = 'pip exited 1: boom'
        return None

    monkeypatch.setattr(mc, '_try_autoinstall_launcher', _fail_install)
    ready, detail = prewarm_vendored_launcher('warm-mcp')
    assert ready is False
    assert 'boom' in detail


def test_prewarm_noop_when_already_resolvable(tmp_path, monkeypatch):
    """If the launcher already resolves, prewarm does NOT call pip."""
    _register_fake_vendor(tmp_path, monkeypatch, 'have-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: None)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: f'/env/bin/{c}')

    def _must_not_run(command):
        raise AssertionError('pip should not run when launcher already resolves')

    monkeypatch.setattr(mc, '_try_autoinstall_launcher', _must_not_run)
    ready, detail = prewarm_vendored_launcher('have-mcp')
    assert ready is True
    assert detail == '/env/bin/have-mcp'



def test_concurrent_autoinstall_runs_pip_once(tmp_path, monkeypatch):
    """Two concurrent installs of the SAME command must run pip exactly once.

    Guards the TOCTOU gap: the per-command lock must serialize callers so the
    second sees ``_install_attempted`` and re-resolves instead of launching a
    second simultaneous ``pip install`` of the same source (startup pre-warm
    racing a user click).
    """
    _register_fake_vendor(tmp_path, monkeypatch, 'race-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: None)

    pip_runs = {'n': 0}
    run_lock = threading.Lock()

    def _fake_run(args, **kw):
        with run_lock:
            pip_runs['n'] += 1
        time.sleep(0.3)  # hold the lock long enough for the racer to queue
        class R:
            returncode = 0
            stdout = 'ok'
            stderr = ''
        return R()

    monkeypatch.setattr(mc.subprocess, 'run', _fake_run)
    # After "pip", the launcher resolves.
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: f'/env/bin/{c}')

    results = {}

    def _call(tag):
        results[tag] = mc._try_autoinstall_launcher('race-mcp')

    t1 = threading.Thread(target=_call, args=('a',))
    t2 = threading.Thread(target=_call, args=('b',))
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert pip_runs['n'] == 1, f'pip ran {pip_runs["n"]}x — per-command lock missing'
    assert results['a'] == '/env/bin/race-mcp'
    assert results['b'] == '/env/bin/race-mcp'


def test_start_install_job_async_and_pollable(tmp_path, monkeypatch):
    """start_install_job returns immediately while pip runs in the background."""
    _register_fake_vendor(tmp_path, monkeypatch, 'job-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: None)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: None)

    release = threading.Event()

    def _slow_prewarm(command):
        release.wait(timeout=5)
        return True, f'/env/bin/{command}'

    monkeypatch.setattr(mc, 'prewarm_vendored_launcher', _slow_prewarm)

    t0 = time.monotonic()
    job = mc.start_install_job('job-mcp')
    elapsed = time.monotonic() - t0

    # Returned without waiting for the (blocked) install.
    assert elapsed < 0.5
    assert job['state'] == 'installing'
    assert mc.get_install_job('job-mcp')['state'] == 'installing'

    # Let the fake pip finish; the job must flip to ready.
    release.set()
    for _ in range(50):
        if mc.get_install_job('job-mcp')['state'] != 'installing':
            break
        time.sleep(0.05)
    final = mc.get_install_job('job-mcp')
    assert final['state'] == 'ready'
    assert final['detail'] == '/env/bin/job-mcp'


def test_start_install_job_ready_fast_path(tmp_path, monkeypatch):
    """If the launcher already resolves, no job/thread is started."""
    _register_fake_vendor(tmp_path, monkeypatch, 'fast-mcp', layout='vendored')
    monkeypatch.setattr(shutil, 'which', lambda c: None)
    monkeypatch.setattr(mc, '_resolve_launcher', lambda c: f'/env/bin/{c}')

    def _must_not_run(command):
        raise AssertionError('prewarm should not run on the ready fast path')

    monkeypatch.setattr(mc, 'prewarm_vendored_launcher', _must_not_run)
    job = mc.start_install_job('fast-mcp')
    assert job['state'] == 'ready'
