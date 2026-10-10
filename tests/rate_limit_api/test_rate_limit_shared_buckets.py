"""tests/rate_limit_api/test_rate_limit_shared_buckets.py — Cross-replica API quota buckets.

Proves the R5 fix: per-principal API token buckets are authoritative across
replicas in distributed mode (via lib.rate_limit_store.RedisTokenBucketStore)
instead of silently becoming limit × N, while personal mode keeps the
in-process path byte-equivalent.

Covers:
  1. Two bucket-store instances sharing one fakeredis server never exceed
     the global limit under concurrency (both at the store level and
     end-to-end through lib.rate_limit_api.check_request).
  2. Refill / consume math is identical to the in-process _Bucket across a
     scripted timeline (including the negative consume-force path).
  3. Backend selection: personal default = in-process (None), explicit
     redis opt-in fails open, distributed mode selects redis fail-closed.
"""

import threading

import pytest

import lib.rate_limit_api as rate_limit_api
import lib.rate_limit_store as rate_limit_store
from lib.api_keys import AuthContext
from lib.rate_limit_api import check_request, record_tokens


pytestmark = pytest.mark.unit


@pytest.fixture
def fake_server():
    fakeredis = pytest.importorskip('fakeredis')
    pytest.importorskip('redis')
    return fakeredis.FakeServer()


def _redis_store(server, *, fail_open=True):
    fakeredis = pytest.importorskip('fakeredis')
    store = rate_limit_store.RedisTokenBucketStore(fail_open=fail_open)
    store._client = fakeredis.FakeStrictRedis(
        server=server, decode_responses=True)
    store._available = True
    return store


@pytest.fixture(autouse=True)
def _clean_selection():
    rate_limit_store.reset_for_test()
    yield
    rate_limit_store.reset_for_test()


# ── 1. Global limit holds across two store instances under concurrency ──


def test_two_instances_never_exceed_global_limit(fake_server):
    replica_a = _redis_store(fake_server)
    replica_b = _redis_store(fake_server)
    limit, attempts_per_thread = 20, 20
    allowed_total = 0
    tally_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def hammer(store):
        nonlocal allowed_total
        barrier.wait()
        local = 0
        for i in range(attempts_per_thread):
            res = store.bucket_try_consume(
                'k1:rpm', float(limit), limit / 60.0, 1.0, 1000.0 + i * 0.001)
            assert res is not None
            if res[0]:
                local += 1
        with tally_lock:
            allowed_total += local

    threads = [threading.Thread(target=hammer, args=(replica_a,))
               for _ in range(4)] + \
              [threading.Thread(target=hammer, args=(replica_b,))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # 160 concurrent attempts across two "replicas", one shared bucket of 20.
    assert allowed_total == limit


def test_check_request_shared_store_global_limit(fake_server, monkeypatch):
    """End-to-end: two check_request 'replicas' sharing one store admit at
    most rpm tokens total."""
    store = _redis_store(fake_server)
    monkeypatch.setattr(rate_limit_store, 'get_token_bucket_store',
                        lambda: store)
    rate_limit_api._state.clear()

    def ctx():
        return AuthContext(key_id='k_shared', name='t',
                           scopes=frozenset({'chat'}),
                           rate_limit_rpm=10, rate_limit_tpd=0)

    allowed = sum(1 for _ in range(50) if check_request(ctx()).allowed)
    assert allowed == 10


# ── 2. Refill math identical across backends ──


def test_refill_math_matches_inprocess_bucket(fake_server):
    store = _redis_store(fake_server)
    ref = rate_limit_api._Bucket(capacity=60.0, tokens=60.0,
                                 refill_rate=1.0, last_refill=1000.0)
    # (advance_to, consume): drain, partial refill, over-consume, cost>1,
    # long refill clamping to capacity.
    script = [(1000.0, 60.0), (1000.0, 1.0), (1010.0, 12.0),
              (1010.0, 3.0), (1015.5, 7.0), (1100.0, 70.0), (1200.0, 0.0)]
    for now, n in script:
        expected_allowed = ref.try_consume(n, now)
        got = store.bucket_try_consume('kb:rpm', 60.0, 1.0, n, now)
        assert got is not None
        assert got[0] == expected_allowed, (now, n)
        assert got[1] == pytest.approx(ref.tokens), (now, n)


def test_consume_force_matches_inprocess_bucket(fake_server):
    """Negative consume path (post-hoc usage) matches _Bucket.consume_force."""
    store = _redis_store(fake_server)
    ref = rate_limit_api._Bucket(capacity=100.0, tokens=100.0,
                                 refill_rate=100.0 / 86400.0,
                                 last_refill=2000.0)
    for now, n in [(2000.0, 150.0), (2100.0, 20.0), (5000.0, 5.0)]:
        ref.consume_force(n, now)
        got = store.bucket_consume_force('kb:tpd', n, now,
                                         capacity=100.0,
                                         refill_rate=100.0 / 86400.0)
        assert got == pytest.approx(ref.tokens), (now, n)
    # Peek must agree with the in-process retry_after formula.
    _, tokens = store.bucket_try_consume('kb:tpd', 100.0, 100.0 / 86400.0,
                                         0.0, 5000.0)
    assert tokens < 0  # negative balances persist, recovering at refill rate


def test_retry_after_formula_matches():
    b = rate_limit_api._Bucket(capacity=10.0, tokens=2.0,
                               refill_rate=0.5, last_refill=0.0)
    assert rate_limit_api._bucket_retry_after(2.0, 6.0, 0.5) == \
        b.retry_after(6.0, 0.0)
    assert rate_limit_api._bucket_retry_after(2.0, 6.0, 0.0) == float('inf')
    assert rate_limit_api._bucket_retry_after(6.0, 6.0, 0.5) == 0.0


# ── TPD denial refunds the RPM token (shared path) ──


def test_tpd_denial_refunds_rpm(fake_server, monkeypatch):
    store = _redis_store(fake_server)
    monkeypatch.setattr(rate_limit_store, 'get_token_bucket_store',
                        lambda: store)
    ctx = AuthContext(key_id='k_refund', name='t', scopes=frozenset({'chat'}),
                      rate_limit_rpm=2, rate_limit_tpd=100)
    assert check_request(ctx).allowed
    # Drain well below zero: the shared peek refills at `now`, so a bucket at
    # exactly 0 would show a sub-token positive balance a moment later.
    record_tokens(ctx.key_id, 150, rpm_limit=2, tpd_limit=100)
    d = check_request(ctx)
    assert not d.allowed and d.reason == 'tpd'
    # The refunded RPM token must be back: an rpm-only principal sees it.
    peek = store.bucket_try_consume('k_refund:rpm', 2.0, 2.0 / 60.0, 0.0,
                                    9999999999.0)
    assert peek is not None and peek[1] >= 1.0


# ── 3. Backend selection / failure policy ──


def test_personal_default_is_inprocess(monkeypatch):
    monkeypatch.delenv('TOFU_API_BUCKET_BACKEND', raising=False)
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    rate_limit_store.reset_for_test()
    assert rate_limit_store.get_token_bucket_store() is None


def test_personal_redis_optin_fails_open(monkeypatch):
    monkeypatch.setenv('TOFU_API_BUCKET_BACKEND', 'redis')
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    rate_limit_store.reset_for_test()
    store = rate_limit_store.get_token_bucket_store()
    assert type(store).__name__ == 'RedisTokenBucketStore'
    assert store.fail_open is True


def test_unknown_backend_falls_back_to_memory(monkeypatch):
    monkeypatch.setenv('TOFU_API_BUCKET_BACKEND', 'bogus')
    monkeypatch.delenv('TOFU_DEPLOYMENT_MODE', raising=False)
    rate_limit_store.reset_for_test()
    assert rate_limit_store.get_token_bucket_store() is None


def _distributed_env(monkeypatch, tmp_path):
    dsn = tmp_path / 'pg.dsn'
    dsn.write_text('postgresql://u:p@db:5432/tofu?sslmode=verify-full')
    redis_url = tmp_path / 'redis.url'
    redis_url.write_text('rediss://cache:6379/0')
    monkeypatch.setenv('TOFU_DEPLOYMENT_MODE', 'distributed')
    monkeypatch.setenv('TOFU_DISTRIBUTED_PREVIEW_MODE', 'read-only')
    monkeypatch.setenv('TOFU_REPLICA_ID', 'replica-1')
    monkeypatch.setenv('TOFU_POSTGRES_DSN_FILE', str(dsn))
    monkeypatch.setenv('TOFU_REDIS_URL_FILE', str(redis_url))


def test_distributed_selects_redis_fail_closed(monkeypatch, tmp_path):
    _distributed_env(monkeypatch, tmp_path)
    rate_limit_store.reset_for_test()
    store = rate_limit_store.get_token_bucket_store()
    assert type(store).__name__ == 'RedisTokenBucketStore'
    assert store.fail_open is False
    assert store._redis_url == 'rediss://cache:6379/0'


def test_distributed_store_outage_denies_new_admission(monkeypatch,
                                                       tmp_path):
    """Fail-closed: an unreachable Redis must NOT silently lift quotas."""
    _distributed_env(monkeypatch, tmp_path)
    rate_limit_store.reset_for_test()
    store = rate_limit_store.get_token_bucket_store()
    monkeypatch.setattr(rate_limit_store, 'get_token_bucket_store',
                        lambda: store)
    # No client, no reachable server → every op reports failure.
    ctx = AuthContext(key_id='k_out', name='t', scopes=frozenset({'chat'}),
                      rate_limit_rpm=60, rate_limit_tpd=1000)
    d = check_request(ctx)
    assert not d.allowed
    assert d.reason == 'store'
    assert d.retry_after_s > 0


def test_personal_store_outage_fails_open(monkeypatch):
    store = rate_limit_store.RedisTokenBucketStore(fail_open=True)
    monkeypatch.setattr(rate_limit_store, 'get_token_bucket_store',
                        lambda: store)
    ctx = AuthContext(key_id='k_open', name='t', scopes=frozenset({'chat'}),
                      rate_limit_rpm=60, rate_limit_tpd=0)
    assert check_request(ctx).allowed


def test_distributed_event_counter_defaults_to_db(monkeypatch, tmp_path):
    """LOW finding: the event-count store follows the deployment contract
    when TOFU_RATE_LIMIT_BACKEND is unset instead of silently using memory."""
    _distributed_env(monkeypatch, tmp_path)
    monkeypatch.delenv('TOFU_RATE_LIMIT_BACKEND', raising=False)
    rate_limit_store.reset_for_test()
    assert type(rate_limit_store.get_store()).__name__ == \
        'DatabaseRateLimitStore'
