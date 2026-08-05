#!/usr/bin/env python3
"""Guard test — the sync→loop request-body read must be BOUNDED.

Regression cover for the "whole site frozen, must restart" failure mode
(2026-07-15). ``server._run_coro_sync`` bridges a sync route handler (executor
thread) to the MAIN event loop to read the request body. Historically its
``future.result()`` had NO timeout, so once the event loop was wedged by any
blocking call, every request-body read queued behind it forever and the entire
sync-executor pool was exhausted — indistinguishable from a crash, only
recoverable by a manual restart.

The fix bounds that cross-thread wait via ``TOFU_SYNC_BODY_TIMEOUT`` (default
300s — wide enough that a genuine slow upload is never cut short; this is a
backstop against an infinitely wedged loop, NOT a tight per-request budget),
mirroring the ``timeout=30`` contract the sibling ``_sync_safe`` wrapper
(server.py) already carries.

These tests exercise the two extracted, module-level seams directly:
  * ``_resolve_sync_body_timeout`` — env parsing + opt-out semantics.
  * ``_await_coro_on_loop`` — actually RAISES (does not hang) when the target
    loop never completes the coroutine within the bound.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_sync_body_timeout.py -v
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit


# ═══════════════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture(scope='module')
def server_module():
    """Import server.py once (installs the Flask→Quart shim, builds the app).

    The extracted helpers live at module scope, so importing the module is all
    we need — we call the helpers directly against our OWN background loop, not
    the server's. conftest forces a throwaway SQLite DB, so this never touches
    production state.
    """
    try:
        import hypercorn  # noqa: F401
        import quart      # noqa: F401
    except ImportError as e:
        pytest.skip(f'async deps unavailable: {e}')
    os.environ.setdefault('TUNNEL_TOKEN', '__sync_body_timeout_test__')
    spec = importlib.util.spec_from_file_location(
        'server',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    # Save + restore — see test_restart_smoke for why an unrestored
    # sys.modules['server'] swap silently breaks later suites' monkeypatches.
    prev = sys.modules.get('server')
    sys.modules['server'] = mod
    try:
        spec.loader.exec_module(mod)
    except SystemExit as e:
        sys.modules.pop('server', None)
        if prev is not None:
            sys.modules['server'] = prev
        pytest.skip(f'server.py exited at import: {e}')
    yield mod
    sys.modules.pop('server', None)
    if prev is not None:
        sys.modules['server'] = prev


@pytest.fixture
def bg_loop():
    """A real asyncio loop running in a background thread (stand-in for the
    server's main loop that sync threads bridge onto)."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, name='test-bg-loop',
                         daemon=True)
    t.start()
    # Wait until the loop is actually spinning.
    for _ in range(200):
        if loop.is_running():
            break
        time.sleep(0.01)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=5)
    loop.close()


# ═══════════════════════════════════════════════════════════════════════
#  1. _resolve_sync_body_timeout — env parsing + contract
# ═══════════════════════════════════════════════════════════════════════


def test_default_timeout_is_generous(server_module, monkeypatch):
    """Absent env → a wide default (300s), NOT unbounded — the whole point is a
    finite ceiling that never fires on a normal (even slow-upload) request."""
    monkeypatch.delenv('TOFU_SYNC_BODY_TIMEOUT', raising=False)
    val = server_module._resolve_sync_body_timeout()
    assert val == 300.0
    assert val is not None  # a finite bound must exist


def test_custom_timeout_honored(server_module, monkeypatch):
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', '45')
    assert server_module._resolve_sync_body_timeout() == 45.0


def test_zero_or_negative_opts_out(server_module, monkeypatch):
    """<= 0 is an explicit opt-out to the legacy unbounded behaviour (None)."""
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', '0')
    assert server_module._resolve_sync_body_timeout() is None
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', '-1')
    assert server_module._resolve_sync_body_timeout() is None


def test_bad_value_falls_back_to_default(server_module, monkeypatch):
    monkeypatch.setenv('TOFU_SYNC_BODY_TIMEOUT', 'not-a-number')
    assert server_module._resolve_sync_body_timeout() == 300.0


# ═══════════════════════════════════════════════════════════════════════
#  2. _await_coro_on_loop — bounded wait actually RAISES, never hangs
# ═══════════════════════════════════════════════════════════════════════


def test_normal_coro_returns_value(server_module, bg_loop):
    """A fast coroutine returns its value through the bridge unchanged."""
    async def _quick():
        return 'pong'

    result = server_module._await_coro_on_loop(_quick(), bg_loop, timeout=5)
    assert result == 'pong'


def test_wedged_loop_raises_instead_of_hanging(server_module, bg_loop):
    """THE regression guard: when the loop is wedged and the coroutine can't
    complete within the bound, the bridge RAISES TimeoutError promptly — it does
    NOT block the calling (worker) thread forever."""
    # Wedge the loop: schedule a synchronous blocking callback ON the loop so it
    # cannot service our coroutine for a while — the exact starvation shape that
    # froze the whole server.
    release = threading.Event()

    def _block_the_loop():
        release.wait(timeout=10)

    bg_loop.call_soon_threadsafe(_block_the_loop)
    time.sleep(0.05)  # ensure the blocking callback is running

    async def _never_in_time():
        return 'too-late'

    t0 = time.time()
    with pytest.raises(FuturesTimeoutError):
        server_module._await_coro_on_loop(
            _never_in_time(), bg_loop, timeout=0.3)
    elapsed = time.time() - t0

    # It must give up near the bound, NOT hang for the full 10s wedge.
    assert elapsed < 3.0, (
        f'bounded wait took {elapsed:.2f}s — it must abort near the '
        f'0.3s timeout, not block on the wedged loop')

    # Let the loop recover so the fixture can shut it down cleanly.
    release.set()


def test_no_timeout_would_hang_neuter(server_module, bg_loop):
    """Load-bearing NEUTER: prove the bound is what prevents the hang.

    With ``timeout=None`` (the legacy unbounded behaviour, still reachable via
    ``TOFU_SYNC_BODY_TIMEOUT<=0``) the SAME wedge blocks past a short deadline —
    demonstrating the timeout argument is what severs the freeze, not some other
    incidental scheduling behaviour.
    """
    release = threading.Event()

    def _block_the_loop():
        release.wait(timeout=10)

    bg_loop.call_soon_threadsafe(_block_the_loop)
    time.sleep(0.05)

    async def _blocked():
        return 'value'

    future = asyncio.run_coroutine_threadsafe(_blocked(), bg_loop)
    # Unbounded semantics: within a short window the result is NOT available
    # because the loop is wedged — i.e. an unbounded wait here WOULD hang.
    with pytest.raises(FuturesTimeoutError):
        future.result(timeout=0.3)

    # Recover, then confirm it eventually completes once the wedge clears.
    release.set()
    assert future.result(timeout=5) == 'value'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
