"""Guard: /api/health liveness is DECOUPLED from the DB probe (pt_afbaf3d7 ②).

/api/health is the frontend's offline ARBITER (backend_offline_monitor: two
failed probes → red "backend offline" banner). The old implementation ran
``SELECT 1`` inline, so a PG-on-FUSE stall (measured 4–7s Slow queries) pushed
the answer past the frontend's 3–4s probe budget and raised the banner on a
perfectly alive process.

INVARIANTS under test (each fails on the pre-fix inline implementation):
  1. The request path NEVER touches the DB inline — with a warm cache, a
     get_db/get_thread_db spy that raises on any call stays uncalled.
  2. A HUNG DB probe cannot push the health answer past the arbiter budget:
     cold start waits at most the bounded join (2s ≪ 3s), returns ok=True,
     and reports no db_responsive verdict yet.
  3. A cached FAILURE degrades db_responsive but never flips `ok` — process
     liveness and DB responsiveness are separate axes.
  4. A cached success is served straight from cache (fast path).

Pure-logic: the route function is called under a minimal Quart request
context; the DB layer is stubbed by monkeypatch. ``unit`` marker.
"""
from __future__ import annotations

import threading
import time

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def probe_env(monkeypatch):
    """Reset the probe cache and force db_available=True for each test."""
    import quart  # noqa: F401 — installing the same shim the suite relies on
    import lib.database as db_mod
    import routes.common as common

    monkeypatch.setattr(db_mod, 'db_available', True, raising=False)
    monkeypatch.setattr(db_mod, '_BACKEND', 'pg', raising=False)
    cache = {'at': 0.0, 'responsive': None, 'error': '', 'ever': False}
    monkeypatch.setattr(common, '_db_probe_cache', cache)
    return common, cache


def _call_health(common):
    import asyncio
    import quart
    app = quart.Quart('health-test')

    async def _run():
        async with app.test_request_context('/api/health'):
            # health_check() returns (resp, status) since api-contract batch 21
            resp, _status = common.health_check()
            return await resp.get_json()

    return asyncio.run(_run())


def test_request_path_never_touches_db_inline(probe_env, monkeypatch):
    """Warm cache + a DB spy that raises on ANY call → spy stays uncalled.
    (The pre-fix code called get_db().execute('SELECT 1') inline — this spy
    would have fired.)"""
    common, cache = probe_env
    cache.update({'at': time.time(), 'responsive': True, 'ever': True})

    def _boom(*a, **k):
        raise AssertionError('health request path touched the DB inline')

    import lib.database as db_mod
    monkeypatch.setattr(db_mod, 'get_db', _boom, raising=False)
    monkeypatch.setattr(db_mod, 'get_thread_db', _boom, raising=False)

    # Warm the ONE-TIME per-process initializations the health route performs
    # on its first call, OUTSIDE the timed region: the cross_dc cluster index
    # and the boot_identity code fingerprint (a `git diff HEAD` subprocess —
    # measured 1.4s on a cold process over FUSE, 0.0s from cache afterwards).
    # The invariant here is "the request path never blocks on the DB inline",
    # not "first call in a fresh process is fast"; both warm-ups are
    # config/repo-size-driven, so their one-time cost legitimately differs
    # between deployments.
    import logging
    _warm_log = logging.getLogger(__name__)
    try:
        from lib.cross_dc import get_status as _cdc_status
        _cdc_status()
    except Exception as _w1:  # cross_dc is optional; absence is fine
        _warm_log.debug('cross_dc warm-up skipped: %s', _w1)
    try:
        from lib import boot_identity as _bi
        _bi.code_fingerprint()
    except Exception as _w2:  # fingerprint is best-effort on the route
        _warm_log.debug('code_fingerprint warm-up skipped: %s', _w2)

    t0 = time.monotonic()
    data = _call_health(common)
    elapsed = time.monotonic() - t0
    assert data['ok'] is True
    assert data['db_responsive'] is True
    assert elapsed < 1.0, f'warm-cache health took {elapsed:.2f}s — blocking on something'


def test_hung_db_cannot_stall_health_past_arbiter_budget(probe_env, monkeypatch):
    """Cold start + a probe that hangs forever (FUSE stall shape): the request
    waits at most the bounded cold join (2s ≪ the frontend's 3s probe budget),
    returns ok=True with no db_responsive verdict yet."""
    common, _cache = probe_env

    def _hang():
        threading.Event().wait(60)  # never completes within the test

    monkeypatch.setattr(common, '_refresh_db_probe', _hang)

    t0 = time.monotonic()
    data = _call_health(common)
    elapsed = time.monotonic() - t0
    assert elapsed < 2.9, (f'health took {elapsed:.2f}s with a hung DB — '
                           f'past the frontend 3s arbiter budget')
    assert data['ok'] is True
    assert 'db_responsive' not in data or data.get('db_responsive') is None


def test_cached_db_failure_never_flips_liveness(probe_env):
    """A cached SELECT-1 failure degrades db_responsive but `ok` stays True —
    the pre-fix code set ok=False, letting a DB stall masquerade as a dead
    process to any ok-consumer."""
    common, cache = probe_env
    cache.update({'at': time.time(), 'responsive': False,
                  'error': 'FUSE hang', 'ever': True})
    data = _call_health(common)
    assert data['ok'] is True
    assert data['db_responsive'] is False
    assert data['db_error'] == 'FUSE hang'


def test_storage_degradation_is_visible_without_flipping_liveness(
        probe_env, monkeypatch):
    common, cache = probe_env
    cache.update({'at': time.time(), 'responsive': True, 'ever': True})
    import lib.storage as storage
    monkeypatch.setattr(storage, 'storage_status', lambda: {
        'ready': False,
        'state': 'restarting',
        'backend': 'sqlite',
        'restart_attempts': 2,
    })

    data = _call_health(common)

    assert data['ok'] is True
    assert data['storage_ready'] is False
    assert data['storage']['state'] == 'restarting'
    assert data['storage']['restart_attempts'] == 2


def test_refresh_thread_populates_cache(probe_env, monkeypatch):
    """The background refresh is the ONE place SELECT 1 runs: stub the thread
    target's DB call, run _refresh_db_probe, and see the cache update."""
    common, cache = probe_env

    class _FakeResult:
        def fetchone(self):
            return (1,)

    class _FakeDB:
        def execute(self, sql):
            assert sql == 'SELECT 1'
            return _FakeResult()

    import lib.database as db_mod
    monkeypatch.setattr(db_mod, 'get_thread_db', lambda: _FakeDB(), raising=False)

    common._refresh_db_probe()
    assert cache['responsive'] is True
    assert cache['error'] == ''
    assert cache['ever'] is True
