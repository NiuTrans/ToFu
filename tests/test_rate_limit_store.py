"""PR3c / C7 step 2 — rate-limit store coverage.

Two backends behind one ``record_and_check`` API; both must satisfy:

  1. Within the limit → ``(True, count)``.
  2. Beyond the limit → ``(False, count)`` (count == limit).
  3. After the window slides forward → counter resets.
  4. Distinct IPs share no bucket.
  5. Distinct endpoints share no bucket.
  6. Backend selection honours the ``TOFU_RATE_LIMIT_BACKEND`` env var.
  7. DB backend fails open (allows the request) when the underlying
     table is missing — never aborts the server.

Run:  pytest tests/test_rate_limit_store.py -v
"""
from __future__ import annotations

import concurrent.futures
import threading
import time

import pytest

from lib.rate_limit_store import (
    DatabaseRateLimitStore,
    MemoryRateLimitStore,
    get_store,
    reset_for_test,
)
from lib.storage import StorageError, StorageSupervisor


@pytest.fixture
def storage_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv('TOFU_STORAGE_SQLITE_READ_POOL', '1')
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    supervisor.start()
    try:
        yield supervisor
    finally:
        supervisor.stop()


def _database_store(storage_sidecar):
    return DatabaseRateLimitStore(
        client_provider=lambda: storage_sidecar.client)


# ═══════════════════════════════════════════════════════════
#  Memory backend
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestMemoryStore:

    def test_within_limit_returns_allowed(self):
        store = MemoryRateLimitStore()
        for i in range(1, 6):
            allowed, count = store.record_and_check('/x', '1.2.3.4', limit=10, per_seconds=60)
            assert allowed is True
            assert count == i

    def test_at_limit_rejects(self):
        store = MemoryRateLimitStore()
        for _ in range(10):
            store.record_and_check('/x', '1.2.3.4', limit=10, per_seconds=60)
        allowed, count = store.record_and_check('/x', '1.2.3.4', limit=10, per_seconds=60)
        assert allowed is False
        assert count == 10  # never recorded; still 10

    def test_distinct_ips_have_separate_buckets(self):
        store = MemoryRateLimitStore()
        for _ in range(10):
            store.record_and_check('/x', '1.1.1.1', limit=10, per_seconds=60)
        # IP #1 is at the cap; IP #2 should still get through.
        allowed_blocked, _ = store.record_and_check('/x', '1.1.1.1', limit=10, per_seconds=60)
        allowed_fresh, count = store.record_and_check('/x', '2.2.2.2', limit=10, per_seconds=60)
        assert allowed_blocked is False
        assert allowed_fresh is True
        assert count == 1

    def test_distinct_endpoints_have_separate_buckets(self):
        store = MemoryRateLimitStore()
        for _ in range(10):
            store.record_and_check('/x', '1.1.1.1', limit=10, per_seconds=60)
        allowed, count = store.record_and_check('/y', '1.1.1.1', limit=10, per_seconds=60)
        assert allowed is True
        assert count == 1

    def test_window_slide_resets_counter(self):
        """A 1-second window with sleep > 1s must let the next request
        through — otherwise the counter never resets."""
        store = MemoryRateLimitStore()
        for _ in range(3):
            store.record_and_check('/x', '1.1.1.1', limit=3, per_seconds=1)
        # 4th request inside the window: blocked.
        blocked, _ = store.record_and_check('/x', '1.1.1.1', limit=3, per_seconds=1)
        assert blocked is False
        time.sleep(1.2)  # slide past window
        allowed, count = store.record_and_check('/x', '1.1.1.1', limit=3, per_seconds=1)
        assert allowed is True
        assert count == 1

    def test_concurrent_admission_never_oversubscribes_bucket(
            self, storage_sidecar):
        """Count + admission + insert is one database-serialized decision."""
        endpoint = f'/db-concurrent-{time.time_ns()}'
        ip = '10.0.0.77'
        limit = 5
        workers = 16
        barrier = threading.Barrier(workers)
        store = _database_store(storage_sidecar)

        def _hit(_index):
            barrier.wait(timeout=10)
            return store.record_and_check(
                endpoint, ip, limit=limit, per_seconds=60)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as pool:
            results = list(pool.map(_hit, range(workers)))

        assert sum(1 for allowed, _count in results if allowed) == limit
        assert all(count <= limit for _allowed, count in results)

# ═══════════════════════════════════════════════════════════
#  Database backend
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDatabaseStore:
    """The DB backend uses only the authenticated semantic Sidecar API."""

    @pytest.fixture(autouse=True)
    def _provision_schema(self, storage_sidecar):
        self.storage = storage_sidecar
        yield

    def _store(self):
        return _database_store(self.storage)

    def test_within_limit_returns_allowed(self):
        store = self._store()
        for i in range(1, 6):
            allowed, count = store.record_and_check('/dbx', '10.0.0.1', limit=10, per_seconds=60)
            assert allowed is True, f'iteration {i} unexpectedly blocked'
            assert count == i

    def test_at_limit_rejects(self):
        store = self._store()
        for _ in range(10):
            store.record_and_check('/dby', '10.0.0.1', limit=10, per_seconds=60)
        allowed, count = store.record_and_check('/dby', '10.0.0.1', limit=10, per_seconds=60)
        assert allowed is False
        assert count == 10

    def test_distinct_ips_have_separate_buckets(self):
        store = self._store()
        for _ in range(10):
            store.record_and_check('/dbz', '10.0.0.1', limit=10, per_seconds=60)
        allowed, count = store.record_and_check('/dbz', '10.0.0.2', limit=10, per_seconds=60)
        assert allowed is True
        assert count == 1

    def test_window_slide_resets_counter(self):
        """1-second window: events older than per_seconds drop out of the
        SELECT COUNT — the next call gets through."""
        store = self._store()
        for _ in range(3):
            store.record_and_check('/dbsl', '10.0.0.5', limit=3, per_seconds=1)
        time.sleep(1.2)
        allowed, count = store.record_and_check('/dbsl', '10.0.0.5', limit=3, per_seconds=1)
        assert allowed is True
        assert count == 1

    def test_missing_table_fails_open(self, monkeypatch):
        """If the table is missing, the store must NOT crash the server —
        it logs a WARN, marks itself unavailable, and allows the request."""
        class _BrokenClient:
            def command(self, *_a, **_kw):
                raise StorageError(
                    'database_integrity', 'schema unavailable')

        store = DatabaseRateLimitStore(
            client_provider=lambda: _BrokenClient())

        allowed, count = store.record_and_check('/missing', '10.0.0.9', limit=1, per_seconds=60)
        assert allowed is True
        assert count == 0
        # Subsequent calls should still fail open (cached _db_available=False)
        # without re-trying the broken DB:
        allowed2, _ = store.record_and_check('/missing', '10.0.0.9', limit=1, per_seconds=60)
        assert allowed2 is True


# ═══════════════════════════════════════════════════════════
#  Backend selection (env var + factory)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBackendSelection:

    def test_default_is_memory(self, monkeypatch):
        monkeypatch.delenv('TOFU_RATE_LIMIT_BACKEND', raising=False)
        reset_for_test()
        store = get_store()
        assert isinstance(store, MemoryRateLimitStore)

    def test_db_backend_selected_when_env_set(self, monkeypatch):
        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'db')
        reset_for_test()
        store = get_store()
        assert isinstance(store, DatabaseRateLimitStore)


    def test_unknown_backend_falls_back_to_memory(self, monkeypatch, caplog):
        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'dynamodb')
        reset_for_test()
        store = get_store()
        assert isinstance(store, MemoryRateLimitStore)

    def test_memoization_within_same_backend(self, monkeypatch):
        """Repeated get_store() calls return the same instance until the
        backend env var changes — avoids re-instantiating per request."""
        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'memory')
        reset_for_test()
        s1 = get_store()
        s2 = get_store()
        assert s1 is s2

    def test_backend_swap_rebuilds_store(self, monkeypatch):
        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'memory')
        reset_for_test()
        s1 = get_store()
        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'db')
        s2 = get_store()
        assert s1 is not s2
        assert isinstance(s2, DatabaseRateLimitStore)


# ═══════════════════════════════════════════════════════════
#  Decorator wiring (smoke — proves rate_limiter.py uses the store)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestDecoratorIntegration:
    """Lightweight smoke test: the @rate_limit decorator goes through
    get_store().record_and_check.  We verify by checking the store's
    counter increments."""

    def test_decorator_calls_store(self, monkeypatch):
        import asyncio

        # ``lib.rate_limiter`` does ``from flask import request`` at module
        # top, which under the test suite's flask→quart shim binds to Quart's
        # request proxy. The app under test must therefore be a Quart app (a
        # real-Flask app would push a Flask request context the decorator
        # can't see → "Not within a request context"). Quart's test client is
        # async, so drive it on a private event loop.
        import quart

        from lib.rate_limiter import rate_limit

        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'memory')
        reset_for_test()
        store = get_store()

        app = quart.Quart(__name__)

        @app.route('/limited')
        @rate_limit(limit=2, per=60)
        def _limited():
            return {'ok': True}

        async def _hit():
            client = app.test_client()
            r1 = await client.get('/limited')
            r2 = await client.get('/limited')
            r3 = await client.get('/limited')
            return r1.status_code, r2.status_code, r3.status_code

        loop = asyncio.new_event_loop()
        try:
            s1, s2, s3 = loop.run_until_complete(_hit())
        finally:
            loop.close()

        assert s1 == 200
        assert s2 == 200
        assert s3 == 429

        # The store knows about the bucket too. The exact peer key depends on
        # the test client (Quart reports ``<local>``; Werkzeug ``127.0.0.1``),
        # so assert on the single bucket the decorator created rather than
        # hardcoding the IP.
        buckets = store._counts['/limited']
        assert len(buckets) == 1, f'expected one peer bucket, got {dict(buckets)}'
        (peer_hits,) = buckets.values()
        assert len(peer_hits) == 2  # only the 2 allowed requests recorded
