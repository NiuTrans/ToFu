"""tests/test_auth_sources_cache_freshness.py

``lib.auth_sources`` used to cache the store ONCE per process and never
re-read it. That made a long-lived reader (a scheduler / optimizer worker, or
any non-server entrypoint) keep the snapshot it took at startup forever:
credentials the user connected later through the Settings UI — written by a
DIFFERENT process — were never picked up, so the fetch path kept hitting the
login wall with no recovery short of a restart.

The cache is now keyed on the store's mtime, so it is self-healing across
processes, and ``invalidate_cache()`` is a PUBLIC interface (reaching into
``_cache_loaded`` from outside the module is what it replaces).

These are the invariants that must not regress.
"""

import json
import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


def _write_store(path, value, domain='xiaohongshu.com', enabled=True):
    """Simulate ANOTHER process writing the store.

    The write must land on a mtime STRICTLY LATER than whatever is recorded
    now, otherwise the test is racing the filesystem's timestamp granularity
    rather than exercising the cache. Sleeping a fixed amount is a bet on that
    granularity (it lost ~1 run in 3 here); instead we bump the mtime
    explicitly and ASSERT it actually moved.
    """
    before = os.path.getmtime(path) if os.path.exists(path) else 0.0
    json.dump({'version': 1, 'sources': [{
        'domain': domain, 'label': 'X', 'enabled': enabled,
        'cookies': [{'name': 'web_session', 'value': value,
                     'domain': '.' + domain, 'path': '/'}],
        'proxy': '', 'updated_at': 1.0,
    }]}, open(path, 'w'))
    # Force a strictly-newer mtime so "another process wrote it" is
    # unambiguous regardless of clock/filesystem resolution.
    bumped = max(os.path.getmtime(path), before) + 1.0
    os.utime(path, (bumped, bumped))
    assert os.path.getmtime(path) > before, 'store mtime did not advance'


@pytest.fixture(autouse=True)
def _isolated_store():
    import lib.auth_sources as A
    prev = A._STORE_PATH
    A._STORE_PATH = os.path.join(tempfile.mkdtemp(), 'auth_sources.json')
    A.invalidate_cache()
    yield A
    A._STORE_PATH = prev
    A.invalidate_cache()


# ── The public interface ──

def test_invalidate_cache_is_public(_isolated_store):
    """It must stay in __all__ — callers must never poke _cache_loaded."""
    assert 'invalidate_cache' in _isolated_store.__all__
    assert callable(_isolated_store.invalidate_cache)


def test_invalidate_cache_forces_a_reread(_isolated_store):
    A = _isolated_store
    _write_store(A._STORE_PATH, 'V1')
    assert A.match_source('https://www.xiaohongshu.com/x') is not None
    A.invalidate_cache()
    # Still readable after invalidation (it re-loads, it does not wipe state).
    got = A.match_source('https://www.xiaohongshu.com/x')
    assert got and got['cookies'][0]['value'] == 'V1'


# ── The defect this fixes: a reader that started BEFORE the write ──

def test_reader_started_before_the_write_still_sees_it(_isolated_store):
    """THE regression test. Pre-fix this returned None forever."""
    A = _isolated_store
    # Reader warms its cache while the store is still empty.
    assert A.match_source('https://www.xiaohongshu.com/x') is None
    # Another process connects the source.
    _write_store(A._STORE_PATH, 'V1')
    got = A.match_source('https://www.xiaohongshu.com/x')
    assert got is not None, 'stale cache — the external write was never seen'
    assert got['cookies'][0]['value'] == 'V1'


def test_subsequent_external_updates_are_also_seen(_isolated_store):
    """Not just the first change — every later one too."""
    A = _isolated_store
    _write_store(A._STORE_PATH, 'V1')
    assert A.match_source('https://www.xiaohongshu.com/x')['cookies'][0]['value'] == 'V1'
    _write_store(A._STORE_PATH, 'V2')
    assert A.match_source('https://www.xiaohongshu.com/x')['cookies'][0]['value'] == 'V2'


def test_external_disable_is_seen(_isolated_store):
    """A source disabled elsewhere must stop matching here."""
    A = _isolated_store
    _write_store(A._STORE_PATH, 'V1', enabled=True)
    assert A.match_source('https://www.xiaohongshu.com/x') is not None
    _write_store(A._STORE_PATH, 'V1', enabled=False)
    assert A.match_source('https://www.xiaohongshu.com/x') is None


def test_list_sources_also_reflects_external_writes(_isolated_store):
    """Freshness belongs to the load path, so every reader inherits it."""
    A = _isolated_store
    _write_store(A._STORE_PATH, 'V1')
    rows = {r['domain']: r for r in A.list_sources()}
    assert rows['xiaohongshu.com']['cookie_count'] == 1


# ── Local writes must not regress ──

@pytest.mark.parametrize('write', ['upsert', 'toggle', 'delete'])
def test_local_writes_are_immediately_visible(_isolated_store, write):
    """Our own writes stay read-your-writes consistent."""
    A = _isolated_store
    A.upsert_source('xiaohongshu.com', cookie_fields={'web_session': 'V1'},
                    enabled=True)
    if write == 'upsert':
        A.upsert_source('xiaohongshu.com', cookie_fields={'web_session': 'V2'})
        assert A.match_source('https://www.xiaohongshu.com/x')['cookies'][0]['value'] == 'V2'
    elif write == 'toggle':
        A.set_enabled('xiaohongshu.com', False)
        assert A.match_source('https://www.xiaohongshu.com/x') is None
    else:
        A.delete_source('xiaohongshu.com')
        assert A.match_source('https://www.xiaohongshu.com/x') is None


def test_a_local_write_then_an_external_one_both_land(_isolated_store):
    """The mtime bookkeeping after our own write must not blind us to theirs."""
    A = _isolated_store
    A.upsert_source('xiaohongshu.com', cookie_fields={'web_session': 'MINE'},
                    enabled=True)
    _write_store(A._STORE_PATH, 'THEIRS')
    got = A.match_source('https://www.xiaohongshu.com/x')
    assert got and got['cookies'][0]['value'] == 'THEIRS'


def test_missing_store_file_is_not_an_error(_isolated_store):
    """A fresh install has no file at all — that must read as 'nothing'."""
    A = _isolated_store
    assert not os.path.exists(A._STORE_PATH)
    assert A.match_source('https://www.xiaohongshu.com/x') is None
    assert isinstance(A.list_sources(), list)


def test_same_tick_write_is_covered_by_invalidate(_isolated_store):
    """mtime cannot see a same-tick overwrite — that is why the public
    ``invalidate_cache()`` exists as the explicit escape hatch.

    Written as a REAL same-tick case: the external write is forced to carry the
    exact mtime the cache already recorded, so mtime alone provably cannot
    detect it.
    """
    A = _isolated_store
    A.upsert_source('xiaohongshu.com', cookie_fields={'web_session': 'MINE'},
                    enabled=True)
    recorded = A._cache_mtime

    json.dump({'version': 1, 'sources': [{
        'domain': 'xiaohongshu.com', 'label': 'X', 'enabled': True,
        'cookies': [{'name': 'web_session', 'value': 'THEIRS',
                     'domain': '.xiaohongshu.com', 'path': '/'}],
        'proxy': '', 'updated_at': 1.0,
    }]}, open(A._STORE_PATH, 'w'))
    os.utime(A._STORE_PATH, (recorded, recorded))   # same tick, on purpose

    # mtime is unchanged, so the cache legitimately still serves the old value.
    assert A.match_source('https://www.xiaohongshu.com/x')['cookies'][0]['value'] == 'MINE'
    # The public escape hatch recovers it.
    A.invalidate_cache()
    assert A.match_source('https://www.xiaohongshu.com/x')['cookies'][0]['value'] == 'THEIRS'
