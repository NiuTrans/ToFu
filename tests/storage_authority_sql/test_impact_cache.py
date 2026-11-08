"""The private test-evidence cache preserves records, atomicity and error isolation."""
from __future__ import annotations

import json
import sqlite3
import zlib

import pytest

from lib.storage_authority_sql.impact_cache import (
    SCHEMA_VERSION,
    ImpactCache,
    ImpactCacheError,
    Record,
)

pytestmark = pytest.mark.unit


def record(name='tests/test_a.py'):
    return Record(name, 'tree', 'failed', [name + '::test_fails'], True,
                  {'pkg/a.py': ('blob', ['<module>', 'run']), 'data.txt': ('data', None)},
                  {'pkg': 'listing'})


def test_existing_records_survive_the_ownership_move(tmp_path):
    """Seed the deployed on-disk format, independently of the new writer."""
    expected = record()
    path = tmp_path / 'impact.sqlite'
    with sqlite3.connect(path) as database:
        database.executescript(f'''
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE records (test TEXT PRIMARY KEY, tree TEXT, outcome TEXT,
                failed TEXT, opaque INTEGER, deps BLOB, dirs BLOB, recorded_at INTEGER);
            CREATE TABLE fingerprints (blob TEXT PRIMARY KEY, units BLOB);
            INSERT INTO meta VALUES ('schema', '{SCHEMA_VERSION}');
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

    cache = ImpactCache(path, 'env')
    try:
        assert cache.records() == {expected.test: expected}
        assert cache.fingerprint('blob') == (True, {'run': 'digest'})
        assert cache.fingerprint('unparsable') == (True, None)
        assert cache.fingerprint('missing') == (False, None)
        cache.save([record('tests/test_b.py')])
    finally:
        cache.close()

    with sqlite3.connect(path) as database:
        assert database.execute('SELECT test FROM records ORDER BY test').fetchall() == [
            ('tests/test_a.py',), ('tests/test_b.py',)]
    database.close()


def test_a_failed_batch_rolls_back_and_exposes_only_a_cache_error(tmp_path):
    cache = ImpactCache(tmp_path / 'impact.sqlite', 'env')
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


@pytest.mark.parametrize('target', ['database', 'records', 'fingerprints'])
def test_corrupt_cache_bytes_become_cache_errors(tmp_path, target):
    path = tmp_path / 'impact.sqlite'
    if target == 'database':
        path.write_bytes(b'not a database')
        with pytest.raises(ImpactCacheError):
            ImpactCache(path, 'env')
        return

    cache = ImpactCache(path, 'env')
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
