"""The private test-evidence cache preserves records, atomicity and error isolation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading
import zlib

import pytest

from lib.storage_authority_sql.impact_cache import ImpactCache, ImpactCacheError, Record

pytestmark = pytest.mark.unit

_AUDIT_SYNTHETIC_REPO_PATHS = {
    'tests/test_a.py', 'tests/test_b.py', 'tests/test_c.py', 'tests/test_other.py',
}


def record(name='tests/test_a.py'):
    return Record(name, 'tree', 'failed', [name + '::test_fails'], True,
                  {'pkg/a.py': ('blob', ['<module>', 'run']), 'data.txt': ('data', None)},
                  {'pkg': 'listing'})


@pytest.mark.parametrize('first_environment', ['env', 'other-env'])
def test_deployed_v5_records_keep_their_original_environment(tmp_path, first_environment):
    """Seed the deployed wire-on-disk format, independently of the new writer."""
    expected = record()
    path = tmp_path / 'impact-v5.sqlite'
    with sqlite3.connect(path) as database:
        database.executescript('''
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE records (test TEXT PRIMARY KEY, tree TEXT, outcome TEXT,
                failed TEXT, opaque INTEGER, deps BLOB, dirs BLOB, recorded_at INTEGER);
            CREATE TABLE fingerprints (blob TEXT PRIMARY KEY, units BLOB);
            INSERT INTO meta VALUES ('environment', 'env');
        ''')
        database.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (expected.test, expected.tree, expected.outcome,
                          json.dumps(expected.failed), 1,
                          zlib.compress(json.dumps(expected.deps).encode()),
                          zlib.compress(json.dumps(expected.dirs).encode()), 1))
        database.executemany('INSERT INTO fingerprints VALUES (?, ?)',
                             [('blob', zlib.compress(b'{"run":"digest"}')),
                              ('unparsable', zlib.compress(b'null'))])
    database.close()

    cache = ImpactCache(tmp_path, first_environment)
    try:
        assert cache.records() == ({expected.test: expected} if first_environment == 'env' else {})
    finally:
        cache.close()

    cache = ImpactCache(tmp_path, 'env')
    try:
        assert cache.records() == {expected.test: expected}
        assert cache.fingerprint('blob') == (True, {'run': 'digest'})
        assert cache.fingerprint('unparsable') == (True, None)
        assert cache.fingerprint('missing') == (False, None)
        cache.save([record('tests/test_b.py')])
    finally:
        cache.close()

    with sqlite3.connect(cache.path) as database:
        assert database.execute('SELECT environment, test FROM records ORDER BY test').fetchall() == [
            ('env', 'tests/test_a.py'), ('env', 'tests/test_b.py')]
    database.close()
    with sqlite3.connect(path) as database:
        assert database.execute('SELECT test FROM records').fetchall() == [('tests/test_a.py',)]
    database.close()
    cache = ImpactCache(tmp_path, 'env')
    try:
        cache.save([], replace_tests=['tests/test_a.py'])
    finally:
        cache.close()
    cache = ImpactCache(tmp_path, 'env')
    try:
        assert cache.records() == {'tests/test_b.py': record('tests/test_b.py')}
    finally:
        cache.close()


def test_a_failed_batch_rolls_back_and_exposes_only_a_cache_error(tmp_path):
    cache = ImpactCache(tmp_path, 'env')
    original = record()
    try:
        cache.save([original])
        with sqlite3.connect(cache.path) as database:
            database.execute('''CREATE TRIGGER reject_record BEFORE INSERT ON records
                WHEN NEW.test = 'rejected'
                BEGIN SELECT RAISE(ABORT, 'injected cache failure'); END''')
        database.close()

        with pytest.raises(ImpactCacheError, match='injected cache failure'):
            cache.save([record('accepted'), record('rejected')])

        assert cache.records() == {original.test: original}
        cache.save_fingerprint('blob', {'run': 'digest'})
        assert cache.fingerprint('blob') == (True, {'run': 'digest'})
    finally:
        cache.close()


@pytest.mark.parametrize('target', ['impact-v5.sqlite', 'impact-v6.sqlite', 'records', 'fingerprints'])
def test_corrupt_cache_bytes_become_cache_errors(tmp_path, target):
    if target.endswith('.sqlite'):
        (tmp_path / target).write_bytes(b'not a database')
        with pytest.raises(ImpactCacheError):
            ImpactCache(tmp_path, 'env')
        return

    cache = ImpactCache(tmp_path, 'env')
    try:
        cache.save([record()])
        cache.save_fingerprint('blob', {'run': 'digest'})
        with sqlite3.connect(cache.path) as database:
            if target == 'records':
                database.execute('UPDATE records SET deps=?', (b'not compressed',))
            else:
                database.execute('UPDATE fingerprints SET units=?', (zlib.compress(b'{'),))
        database.close()

        with pytest.raises(ImpactCacheError):
            if target == 'records':
                cache.records()
            else:
                cache.fingerprint('blob')
    finally:
        cache.close()


@pytest.mark.parametrize('environments', [('env', 'env'), ('env-a', 'env-b')])
def test_concurrent_cache_owners_keep_both_record_batches(tmp_path, environments):
    ready = threading.Barrier(2)

    def save(item):
        name, environment = item
        ready.wait()
        cache = ImpactCache(tmp_path, environment)
        try:
            ready.wait()
            cache.save([record(name)])
        finally:
            cache.close()

    names = ['tests/test_a.py', 'tests/test_b.py']
    with ThreadPoolExecutor(max_workers=2) as workers:
        list(workers.map(save, zip(names, environments)))
    for environment in set(environments):
        cache = ImpactCache(tmp_path, environment)
        try:
            assert cache.records() == {name: record(name) for name, owner in zip(names, environments)
                                       if owner == environment}
        finally:
            cache.close()


def test_overlapping_environments_never_read_each_others_evidence(tmp_path):
    first = ImpactCache(tmp_path, 'env-a')
    second = ImpactCache(tmp_path, 'env-b')
    try:
        second.save([record('tests/test_b.py')])
        assert first.records() == {}
        first.save([record('tests/test_a.py')])
        assert second.records() == {'tests/test_b.py': record('tests/test_b.py')}
        assert first.records() == {'tests/test_a.py': record('tests/test_a.py')}
        second.save([record('tests/test_c.py')])
        assert first.records() == {'tests/test_a.py': record('tests/test_a.py')}
        assert second.records() == {name: record(name)
                                    for name in ['tests/test_b.py', 'tests/test_c.py']}
    finally:
        first.close()
        second.close()


def test_returning_to_an_environment_reuses_its_own_records(tmp_path):
    for environment in ('env-a', 'env-b'):
        cache = ImpactCache(tmp_path, environment)
        try:
            cache.save([record(environment)])
        finally:
            cache.close()
    cache = ImpactCache(tmp_path, 'env-a')
    try:
        assert cache.records() == {'env-a': record('env-a')}
    finally:
        cache.close()


def test_partial_execution_evicts_only_its_environment_even_for_the_same_test(tmp_path):
    first, second = ImpactCache(tmp_path, 'env-a'), ImpactCache(tmp_path, 'env-b')
    try:
        first.save([record()])
        second.save([record()])
        first.save([], replace_tests=[record().test])
        assert first.records() == {}
        assert second.records() == {record().test: record()}
    finally:
        first.close()
        second.close()


def test_incomplete_current_runs_remove_only_their_previous_evidence(tmp_path):
    cache = ImpactCache(tmp_path, 'env')
    first, other = record(), record('tests/test_other.py')
    try:
        cache.save([first, other])
        cache.save([], replace_tests=[first.test])
        assert cache.records() == {other.test: other}
    finally:
        cache.close()
