"""Regression coverage for the thread-local DB connection leak fix.

Background
----------
``get_thread_db()`` caches one connection per (thread, domain) for the thread's
whole lifetime. Long-lived worker threads (the ``asyncio.to_thread`` default
pool, daemon task threads) therefore pinned a connection EACH until they died,
which under 1000+ concurrent users exhausted the bounded connection semaphore
("pool exhausted (200/200 in use, N pooled, 884 thread-tracked)").

The fix has three parts, all exercised here:

  1. ``close_thread_db()`` returns this thread's connection(s) to the shared
     pool and drops their registry entries — so a reused pool thread does NOT
     pin a connection between units of work.
  2. ``_register_thread_conn`` de-dups on (thread, domain) so the registry
     can't grow one stale tuple per reconnect.
  3. ``_reap_dead_thread_connections`` returns the reclaim count and can be
     invoked inline from the semaphore-acquire path (self-heal).

These tests run against whatever backend is active (PG or SQLite). On SQLite
the registry is a no-op, so the registry-specific assertions are skipped.

Run:  pytest tests/test_db_thread_conn_lifecycle.py -v
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time

import pytest

import lib.database._core as core
from lib.database import DOMAIN_CHAT, close_thread_db, get_thread_db


def _registry_len() -> int:
    with core._thread_conn_lock:
        return len(core._thread_conn_registry)


@pytest.mark.unit
class TestThreadConnLifecycle:

    def test_close_thread_db_releases_for_reuse(self, flask_client):
        """A fixed pool of reused threads running many tasks must not pin a
        connection per thread: after each ``close_thread_db()`` the worker's
        entry is gone, so the registry returns to its pre-test baseline and no
        entry is left behind by a now-dead worker thread.

        NOTE: we compare against a BASELINE rather than asserting the registry
        is globally empty — the ``flask_client`` fixture boots the full app,
        whose MainThread and background daemons (e.g. ``billing-janitor``)
        legitimately hold their own ``system``-domain connections for their
        whole lifetime. Those are alive and are NOT leaks; only an entry whose
        owning thread has DIED is a leak.
        """
        baseline = _registry_len()

        def task(_i):
            db = get_thread_db(DOMAIN_CHAT)
            db.execute('SELECT 1')
            db.commit()
            close_thread_db()

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(task, range(200)))

        if core._BACKEND == 'pg':
            # The 200 worker tasks all released, so the registry must be back
            # at baseline (the app's own live-thread entries, unchanged).
            assert _registry_len() == baseline, (
                f'thread-conn registry grew past baseline '
                f'({_registry_len()} > {baseline}) — a worker pinned a conn')
            # And NO surviving entry may belong to a dead thread (the real
            # leak signature the reaper exists to catch).
            with core._thread_conn_lock:
                dead = [d for (r, _c, d) in core._thread_conn_registry
                        if (th := r()) is None or not th.is_alive()]
            assert not dead, f'dead-thread connection entries leaked: {dead}'
            with core._conn_pool_lock:
                pooled = len(core._conn_pool)
            with core._conn_count_lock:
                active = core._conn_count
            assert pooled <= core._CONN_POOL_MAX
            # active counts pooled + in-use; in-use are only the app's live
            # baseline threads, so the delta must equal that baseline.
            assert active - pooled == baseline, (
                f'in-use leak: active={active} pooled={pooled} baseline={baseline}')

    def test_register_dedups_on_reconnect(self, flask_client):
        """Reconnecting in the SAME thread+domain must not append a second
        registry tuple — otherwise tracked_threads grows unbounded."""
        if core._BACKEND != 'pg':
            pytest.skip('registry is PG-only')

        result = {}

        def worker():
            get_thread_db(DOMAIN_CHAT)
            # Simulate a health-check failure forcing a reconnect by closing
            # the underlying connection, then re-fetching.
            db = getattr(core._thread_local, f'db_{DOMAIN_CHAT}', None)
            try:
                db._conn.close()  # next _test_connection() fails → reconnect
            except Exception:
                pass
            get_thread_db(DOMAIN_CHAT)
            get_thread_db(DOMAIN_CHAT)
            # Count entries belonging to THIS thread.
            me = threading.current_thread()
            with core._thread_conn_lock:
                mine = sum(1 for (r, _c, d) in core._thread_conn_registry
                           if r() is me and d == DOMAIN_CHAT)
            result['mine'] = mine
            close_thread_db()

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        assert result['mine'] == 1, (
            f'expected exactly 1 registry entry per (thread, domain), '
            f'got {result["mine"]}')

    def test_reaper_reclaims_dead_thread_conns(self, flask_client):
        """Connections held by threads that have died are reclaimable on
        demand (the self-heal path used by the semaphore acquire)."""
        if core._BACKEND != 'pg':
            pytest.skip('reaper is PG-only')

        before = _registry_len()

        def grab_and_die():
            get_thread_db(DOMAIN_CHAT)  # never released; thread then exits

        threads = [threading.Thread(target=grab_and_die) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        time.sleep(0.2)  # ensure threads are fully dead

        assert _registry_len() >= before + 1
        reclaimed = core._reap_dead_thread_connections()
        assert reclaimed >= 1, 'reaper should reclaim dead-thread connections'
        # All dead-thread entries gone (only live threads, if any, remain).
        with core._thread_conn_lock:
            for (r, _c, _d) in core._thread_conn_registry:
                th = r()
                assert th is not None and th.is_alive(), \
                    'dead-thread entry survived the reap'

    def test_close_thread_db_idempotent(self, flask_client):
        """Calling close_thread_db() with no active connection is a no-op,
        and a double call doesn't error."""
        close_thread_db()
        get_thread_db(DOMAIN_CHAT)
        close_thread_db()
        close_thread_db()  # second call: nothing to release, must not raise
