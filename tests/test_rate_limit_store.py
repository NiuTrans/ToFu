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
  8. Process-local bucket identity, event residency, and retained key bytes
     stay finite under high-cardinality input.
  9. Cleanup applies each bucket's own window and hot checks are O(expiry),
     not a full-list rebuild.

Run:  pytest tests/test_rate_limit_store.py -v
"""
from __future__ import annotations

import concurrent.futures
import sqlite3
import threading
import time

import pytest

from lib.rate_limit_store import (
    DatabaseRateLimitStore,
    MemoryRateLimitStore,
    get_store,
    reset_for_test,
)
from lib.rate_limit_policy import (
    RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY,
    rate_limit_memory_bucket_capacity,
)
from lib.storage import StorageError, StorageSupervisor


pytestmark = pytest.mark.unit


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


class _ManualClock:
    def __init__(self, now: float = 1_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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

    def test_concurrent_admission_never_oversubscribes_memory_bucket(self):
        workers = 32
        limit = 5
        barrier = threading.Barrier(workers)
        store = MemoryRateLimitStore(bucket_capacity=8)

        def _hit(_index):
            barrier.wait(timeout=10)
            return store.record_and_check(
                '/memory-concurrent', '10.0.0.8',
                limit=limit, per_seconds=60)

        with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers) as pool:
            results = list(pool.map(_hit, range(workers)))

        assert sum(1 for allowed, _count in results if allowed) == limit
        assert all(count <= limit for _allowed, count in results)

    def test_cleanup_uses_each_bucket_own_window(self):
        clock = _ManualClock()
        store = MemoryRateLimitStore(
            bucket_capacity=8, event_capacity=32, clock=clock)
        assert store.record_and_check(
            '/long', 'ip', limit=10, per_seconds=1_000) == (True, 1)
        assert store.record_and_check(
            '/short', 'ip', limit=10, per_seconds=1) == (True, 1)

        clock.advance(2)
        store._last_cleanup = 0.0
        store.record_and_check('/probe', 'ip', limit=10, per_seconds=1)

        # The short bucket expired, but a cleanup triggered by a short-window
        # request cannot erase the still-live long-window event.
        assert store.record_and_check(
            '/long', 'ip', limit=10, per_seconds=1_000) == (True, 2)
        assert store.stats()['expired_bucket_evictions'] == 1

    def test_high_cardinality_is_lru_bounded(self):
        store = MemoryRateLimitStore(
            bucket_capacity=32, event_capacity=4_096)
        for index in range(5_000):
            store.record_and_check(
                f'/objects/{index}', f'peer-{index}',
                limit=120, per_seconds=60)

        stats = store.stats()
        assert stats['buckets'] == 32
        assert stats['events'] == 32
        assert stats['bucket_capacity_evictions'] == 5_000 - 32

    def test_total_event_capacity_makes_one_huge_bucket_stricter(self):
        store = MemoryRateLimitStore(
            bucket_capacity=4, event_capacity=2)
        assert store.record_and_check(
            '/hot', 'peer', limit=10, per_seconds=60) == (True, 1)
        assert store.record_and_check(
            '/hot', 'peer', limit=10, per_seconds=60) == (True, 2)
        assert store.record_and_check(
            '/hot', 'peer', limit=10, per_seconds=60) == (False, 2)
        stats = store.stats()
        assert stats['events'] == stats['event_capacity'] == 2
        assert stats['event_capacity_rejections'] == 1

    def test_long_identity_text_is_not_retained_as_a_key(self):
        store = MemoryRateLimitStore(bucket_capacity=4)
        endpoint = '/x/' + 'e' * 10_000
        client_key = 'c' * 10_000
        store.record_and_check(endpoint, client_key, limit=10, per_seconds=60)

        ((retained_endpoint, retained_client),) = store._buckets.keys()
        assert retained_endpoint.startswith('h:')
        assert retained_client.startswith('h:')
        assert len(retained_endpoint) == len(retained_client) == 66
        assert endpoint not in retained_endpoint
        assert client_key not in retained_client

    def test_operator_bucket_override_has_a_hard_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            'TOFU_RATE_LIMIT_MEMORY_BUCKET_CAPACITY', '999999999')
        assert rate_limit_memory_bucket_capacity() \
            == RATE_LIMIT_MEMORY_BUCKET_HARD_CAPACITY

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


def test_schema_42_reclaims_legacy_events_and_adds_expiry_index(tmp_path):
    """Legacy rows have no safe TTL; v43 resets only this reconstructible table."""
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema

    connection = sqlite3.connect(tmp_path / 'schema-v42.db')
    connection.row_factory = sqlite3.Row
    connection.execute(
        'CREATE TABLE storage_meta(meta_key TEXT PRIMARY KEY, meta_value TEXT)')
    connection.execute(
        'INSERT INTO storage_meta VALUES (?, ?)', ('schema_version', '42'))
    connection.execute(
        'CREATE TABLE storage_rate_limit_events('
        'event_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL, '
        'client_key TEXT NOT NULL, occurred_at_ms BIGINT NOT NULL)')
    connection.execute(
        'INSERT INTO storage_rate_limit_events VALUES (?, ?, ?, ?)',
        ('legacy-event', '/x', 'peer', 1))

    initialize_schema(SQLiteSession(connection))
    version = int(connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key='schema_version'"
    ).fetchone()[0])
    columns = {
        row['name'] for row in connection.execute(
            'PRAGMA table_info(storage_rate_limit_events)')
    }
    indexes = {
        row['name'] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' ")
    }
    rows = connection.execute(
        'SELECT COUNT(*) FROM storage_rate_limit_events').fetchone()[0]
    query_plan = ' '.join(
        str(row['detail']) for row in connection.execute(
            'EXPLAIN QUERY PLAN SELECT event_id '
            'FROM storage_rate_limit_events WHERE expires_at_ms <= ? '
            'ORDER BY expires_at_ms, event_id LIMIT ?', (1, 256))
    )
    connection.close()

    assert version == SCHEMA_VERSION == 51
    assert 'expires_at_ms' in columns
    assert 'storage_rate_limit_bucket_idx' in indexes
    assert 'storage_rate_limit_expiry_idx' in indexes
    assert 'storage_rate_limit_expiry_idx' in query_plan
    assert rows == 0


def test_sidecar_expiry_prune_has_a_hard_transaction_batch(
        tmp_path, monkeypatch):
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._records import (
        _rate_limit_record_and_check,
    )
    from lib.storage_sidecar.schema import initialize_schema

    connection = sqlite3.connect(tmp_path / 'rate-prune.db')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.executemany(
        'INSERT INTO storage_rate_limit_events('
        'event_id, endpoint, client_key, occurred_at_ms, expires_at_ms) '
        'VALUES (?, ?, ?, ?, ?)',
        ((f'expired-{index}', f'/old/{index}', f'peer-{index}', 0, 1)
         for index in range(300)),
    )
    monkeypatch.setattr(
        'lib.storage_sidecar.operations_pkg._records.time.time',
        lambda: 2.0)

    result = _rate_limit_record_and_check(session, {
        'endpoint': '/new', 'client_key': 'peer-new',
        'event_id': 'new-event', 'limit': 10, 'per_seconds': 60,
    })
    remaining = connection.execute(
        'SELECT COUNT(*) FROM storage_rate_limit_events').fetchone()[0]
    connection.close()

    assert result == {'allowed': True, 'count': 1, 'pruned': 256}
    assert remaining == 45  # 300 old - 256 bounded prune + one new


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
        stats = store.stats()
        assert stats['buckets'] == 1
        assert stats['events'] == 2  # only the 2 allowed requests recorded

    def test_decorator_keys_dynamic_paths_by_route_template(self, monkeypatch):
        import asyncio

        import quart

        from lib.rate_limiter import rate_limit

        monkeypatch.setenv('TOFU_RATE_LIMIT_BACKEND', 'memory')
        reset_for_test()
        store = get_store()
        app = quart.Quart(__name__)

        @app.route('/limited/<item_id>')
        @rate_limit(limit=2, per=60)
        def _limited_item(item_id):
            return {'item': item_id}

        async def _hit():
            client = app.test_client()
            return [
                (await client.get(path)).status_code
                for path in ('/limited/a', '/limited/b', '/limited/c')
            ]

        loop = asyncio.new_event_loop()
        try:
            statuses = loop.run_until_complete(_hit())
        finally:
            loop.close()

        assert statuses == [200, 200, 429]
        assert store.stats()['buckets'] == 1
