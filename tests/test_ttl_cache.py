#!/usr/bin/env python3
"""Unit tests for lib.ttl_cache.TTLCache."""

import os
import sys
import threading
import time
import gc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


def test_get_set_basic():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    assert c.get('x') is None
    c.set('x', 42)
    assert c.get('x') == 42
    _ok('get/set basic round-trip')


def test_get_default_when_missing():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    assert c.get('missing', 'fallback') == 'fallback'
    assert c.get('missing', default=[1, 2]) == [1, 2]
    _ok('get returns default on miss')


def test_ttl_expires():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=0.1)
    c.set('x', 'value')
    assert c.get('x') == 'value'
    time.sleep(0.15)
    assert c.get('x') is None  # expired
    _ok('TTL expiry: entries become misses after ttl')


def test_zero_ttl_means_no_expiry():
    """ttl=0 disables expiry — entries live until evicted by size."""
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=0)
    c.set('x', 'value')
    time.sleep(0.05)
    assert c.get('x') == 'value'
    _ok('ttl=0 disables expiry (entries persist)')


def test_negative_ttl_means_no_expiry():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=-1)
    c.set('x', 'value')
    time.sleep(0.05)
    assert c.get('x') == 'value'
    _ok('ttl<0 disables expiry')


def test_invalidate():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    c.set('x', 1)
    assert c.invalidate('x') is True
    assert c.invalidate('x') is False  # already gone
    assert c.get('x') is None
    _ok('invalidate removes entries; second call returns False')


def test_clear():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    for i in range(5):
        c.set(f'k{i}', i)
    n = c.clear()
    assert n == 5
    assert len(c) == 0
    _ok('clear() removes all entries')


def test_max_size_evicts_oldest():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60, max_size=3)
    c.set('a', 1)
    c.set('b', 2)
    c.set('c', 3)
    c.set('d', 4)  # should evict 'a' (oldest)
    assert c.get('a') is None
    assert c.get('b') == 2
    assert c.get('c') == 3
    assert c.get('d') == 4
    assert len(c) == 3
    _ok('max_size FIFO-evicts oldest entry')


def test_lru_touches_on_access():
    """Accessing an entry moves it to the back of the LRU queue."""
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60, max_size=3)
    c.set('a', 1)
    c.set('b', 2)
    c.set('c', 3)
    # Access 'a' — now most-recent
    _ = c.get('a')
    # Insert 'd' — should evict 'b' (now oldest), not 'a'
    c.set('d', 4)
    assert c.get('a') == 1   # still cached
    assert c.get('b') is None  # evicted
    _ok('get() updates LRU recency (touched key not evicted)')


def test_has_and_in():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    c.set('x', 1)
    assert c.has('x') is True
    assert 'x' in c
    assert c.has('missing') is False
    assert 'missing' not in c
    _ok('has() and __contains__ work')


def test_has_evicts_expired():
    """has() should NOT report True for expired entries."""
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=0.05)
    c.set('x', 1)
    time.sleep(0.1)
    assert c.has('x') is False
    _ok('has() returns False for expired (and lazily evicts)')


def test_cleanup_stale():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=0.05)
    c.set('a', 1)
    c.set('b', 2)
    c.set('c', 3)
    time.sleep(0.1)
    c.set('d', 4)  # fresh
    # Eagerly clean
    n = c.cleanup_stale()
    assert n == 3  # a, b, c expired
    assert len(c) == 1
    assert c.get('d') == 4
    _ok('cleanup_stale() removes expired entries eagerly')


def test_cleanup_noop_when_no_ttl():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=0)
    c.set('x', 1)
    time.sleep(0.05)
    assert c.cleanup_stale() == 0  # no expiry possible
    assert c.get('x') == 1
    _ok('cleanup_stale() is no-op when ttl=0')


def test_get_or_compute_caches_result():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    n_calls = 0
    def fn():
        nonlocal n_calls
        n_calls += 1
        return 42
    assert c.get_or_compute('k', fn) == 42
    assert c.get_or_compute('k', fn) == 42
    assert n_calls == 1  # second call is a hit
    _ok('get_or_compute caches the computed value')


def test_get_or_compute_serialises_concurrent_missers():
    """Two threads missing on the same key → only one fn() call."""
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)

    n_calls = 0
    barrier = threading.Barrier(2)

    def slow_fn():
        nonlocal n_calls
        # Both threads should land here at the same time;
        # the per-key lock serialises so only one wins.
        n_calls += 1
        time.sleep(0.05)
        return 'computed'

    def worker():
        barrier.wait()
        result = c.get_or_compute('k', slow_fn)
        assert result == 'computed'

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert n_calls == 1, f'fn called {n_calls} times, expected 1'
    _ok('get_or_compute serialises concurrent missers (1 fn call, not 2)')


def test_get_or_compute_propagates_exception():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    def fail():
        raise RuntimeError('boom')
    crashed = False
    try:
        c.get_or_compute('k', fail)
    except RuntimeError:
        crashed = True
    assert crashed
    # And nothing was cached
    assert c.get('k') is None
    _ok('get_or_compute propagates exception, does NOT cache')


def test_get_or_compute_does_not_retain_historical_key_locks():
    """A cache-disabled LRU must not become an unbounded lock registry."""
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60, max_size=0)
    for index in range(100):
        assert c.get_or_compute(index, lambda: index) == index
    gc.collect()
    assert len(c) == 0
    assert len(c._key_locks) == 0
    _ok('get_or_compute reclaims inactive per-key locks')


def test_thread_safe_concurrent_set():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    NUM_THREADS = 8

    def worker(tid):
        for i in range(100):
            c.set(f'k_{tid}_{i}', i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(NUM_THREADS)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(c) == NUM_THREADS * 100
    _ok(f'thread-safe set under {NUM_THREADS} threads × 100 inserts')


def test_stats():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=0.05, max_size=3, name='unit_test')
    # 3 hits, 2 misses, 1 size eviction, 1 expired evict
    c.set('a', 1)
    c.get('a')  # hit
    c.get('a')  # hit
    c.get('a')  # hit
    c.get('missing')  # miss
    c.get('also_missing')  # miss
    c.set('b', 2)
    c.set('c', 3)
    c.set('d', 4)  # evicts 'a' (size)
    time.sleep(0.1)  # all expire
    c.get('b')  # expired-miss
    s = c.stats()
    assert s['name'] == 'unit_test'
    assert s['hits'] == 3
    assert s['misses'] >= 3
    assert s['size_evicts'] == 1
    assert s['expired_evicts'] >= 1
    _ok('stats() reports hits/misses/evictions correctly')


def test_complex_value_types():
    """Cache should handle arbitrary value types — dicts, lists, None."""
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    c.set('dict', {'nested': [1, 2]})
    c.set('list', [1, 2, 3])
    c.set('none', None)
    assert c.get('dict') == {'nested': [1, 2]}
    assert c.get('list') == [1, 2, 3]
    # Note: get('none') returns None, but that's the cached value, not a miss.
    # The user can disambiguate via has() or a sentinel default.
    assert c.has('none') is True
    _ok('cache handles dict, list, None values correctly')


def test_keys_can_be_tuples():
    from lib.ttl_cache import TTLCache
    c = TTLCache(ttl=60)
    c.set(('compound', 'key', 1), 'value')
    assert c.get(('compound', 'key', 1)) == 'value'
    _ok('hashable tuple keys work')


def main():
    print()
    print(_color('═══ ttl_cache.py Unit Tests ═══', '36'))
    print()
    tests = [
        test_get_set_basic,
        test_get_default_when_missing,
        test_ttl_expires,
        test_zero_ttl_means_no_expiry,
        test_negative_ttl_means_no_expiry,
        test_invalidate,
        test_clear,
        test_max_size_evicts_oldest,
        test_lru_touches_on_access,
        test_has_and_in,
        test_has_evicts_expired,
        test_cleanup_stale,
        test_cleanup_noop_when_no_ttl,
        test_get_or_compute_caches_result,
        test_get_or_compute_serialises_concurrent_missers,
        test_get_or_compute_propagates_exception,
        test_get_or_compute_does_not_retain_historical_key_locks,
        test_thread_safe_concurrent_set,
        test_stats,
        test_complex_value_types,
        test_keys_can_be_tuples,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()
