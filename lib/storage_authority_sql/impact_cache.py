"""Private persistence for reconstructible landing-test evidence.

Responsibility: own the versioned SQLite cache, record codecs, atomic batches
and cross-process file lock. This cache opens only its explicit repository
cache directory, never the application's selected authority. Verification can
run before the application or its storage service starts.

Entry points: ``ImpactCache``, ``Record`` and ``ImpactCacheError``. Callers
supply observed test records and Git-blob fingerprints; no SQL or connection
crosses this boundary. Dependencies: standard library only.
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
from pathlib import Path
import sqlite3
import time
import zlib

# v7 splits conftest and inert definition headers out of the module unit, so
# editing an unused helper no longer invalidates every run in its scope.
# Older files stay for processes still running older code; v6 evidence seeds
# this format once, under its original identity (its coarser units only make
# the records it seeds more conservative).
SCHEMA_VERSION = 7


class ImpactCacheError(Exception):
    """The optional verification cache could not be read or written."""


@contextlib.contextmanager
def _cache_errors():
    try:
        yield
    except (OSError, sqlite3.Error, ValueError, zlib.error) as error:
        raise ImpactCacheError(str(error)) from error


@dataclasses.dataclass
class Record:
    test: str
    tree: str
    outcome: str
    failed: list[str]
    opaque: bool
    deps: dict[str, tuple[str, list[str] | None]]
    dirs: dict[str, str]


class ImpactCache:
    """One repository's records and fingerprints, serialized by a sibling lock.

    Each environment owns its latest records per test. Switching environments
    cannot consume or evict another environment's evidence. Content-addressed
    fingerprints remain shared; an existing v5 cache seeds the new format.
    """

    def __init__(self, directory, environment_key: str):
        self._environment_key = environment_key
        directory = Path(directory)
        self.path = directory / f'impact-v{SCHEMA_VERSION}.sqlite'
        self._lock_path = directory / f'impact-v{SCHEMA_VERSION}.lock'
        with _cache_errors():
            directory.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path))
            try:
                with self._exclusive(), self._db:
                    self._db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, '
                                     'value TEXT)')
                    self._db.execute('CREATE TABLE IF NOT EXISTS records (environment TEXT NOT NULL, '
                                     'test TEXT NOT NULL, '
                                     'tree TEXT, outcome TEXT, failed TEXT, opaque INTEGER, deps BLOB, '
                                     'dirs BLOB, recorded_at INTEGER, PRIMARY KEY (environment, test))')
                    self._db.execute('CREATE TABLE IF NOT EXISTS fingerprints (blob TEXT PRIMARY KEY, '
                                     'units BLOB)')
                    self._import_legacy(directory)
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    self._db.close()
                raise

    def _import_legacy(self, directory: Path) -> None:
        """Seed once from a locked v5/v6 snapshot without modifying older writers' data."""
        if self._db.execute("SELECT value FROM meta WHERE key='legacy-v6-imported'").fetchone():
            return
        legacy = directory / 'impact-v6.sqlite'
        if legacy.exists():
            with open(directory / 'impact-v6.lock', 'a+') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                with contextlib.closing(sqlite3.connect(legacy.resolve().as_uri() + '?mode=ro',
                                                       uri=True)) as source:
                    rows = source.execute('SELECT environment, test, tree, outcome, failed, opaque, '
                                          'deps, dirs, recorded_at FROM records').fetchall()
                    self._db.executemany('INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
                    self._db.executemany('INSERT OR IGNORE INTO fingerprints VALUES (?, ?)',
                                         source.execute('SELECT blob, units FROM fingerprints'))
        legacy = directory / 'impact-v5.sqlite'
        if legacy.exists():
            with open(directory / 'impact-v5.lock', 'a+') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                with contextlib.closing(sqlite3.connect(legacy.resolve().as_uri() + '?mode=ro',
                                                       uri=True)) as source:
                    environment = source.execute(
                        "SELECT value FROM meta WHERE key='environment'").fetchone()
                    if environment and environment[0] is not None:
                        rows = source.execute('SELECT test, tree, outcome, failed, opaque, deps, dirs, '
                                              'recorded_at FROM records').fetchall()
                        self._db.executemany(
                            'INSERT OR IGNORE INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            ((environment[0], *row) for row in rows))
                    self._db.executemany('INSERT OR IGNORE INTO fingerprints VALUES (?, ?)',
                                         source.execute('SELECT blob, units FROM fingerprints'))
        self._db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', ('legacy-v5-imported', '1'))
        self._db.execute('INSERT INTO meta VALUES (?, ?)', ('legacy-v6-imported', '1'))

    @contextlib.contextmanager
    def _exclusive(self):
        with _cache_errors(), open(self._lock_path, 'a+') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def close(self) -> None:
        with _cache_errors():
            self._db.close()

    def records(self) -> dict[str, Record]:
        with self._exclusive():
            rows = self._db.execute(
                'SELECT test, tree, outcome, failed, opaque, deps, dirs FROM records '
                'WHERE environment=?', (self._environment_key,)).fetchall()
            found = {}
            for test, tree, outcome, failed, opaque, deps, dirs in rows:
                decoded = json.loads(zlib.decompress(deps))
                found[test] = Record(test, tree, outcome, json.loads(failed), bool(opaque),
                                     {path: (value[0], value[1]) for path, value in decoded.items()},
                                     json.loads(zlib.decompress(dirs)))
            return found

    def save(self, records: list[Record], *, replace_tests=()) -> None:
        rows = [(self._environment_key, record.test, record.tree, record.outcome, json.dumps(record.failed),
                 int(record.opaque),
                 zlib.compress(json.dumps({path: [blob, names] for path, (blob, names)
                                           in record.deps.items()}).encode('utf-8')),
                 zlib.compress(json.dumps(record.dirs).encode('utf-8')), int(time.time()))
                for record in records]
        with self._exclusive(), self._db:
            self._db.executemany('DELETE FROM records WHERE environment=? AND test=?',
                                 ((self._environment_key, test) for test in replace_tests))
            self._db.executemany('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                                 rows)

    def fingerprint(self, blob: str) -> tuple[bool, dict[str, str] | None]:
        """Return (found, units); a cached unparsable blob has units=None."""
        with self._exclusive():
            row = self._db.execute('SELECT units FROM fingerprints WHERE blob=?',
                                   (blob,)).fetchone()
            return (False, None) if row is None else (True, json.loads(zlib.decompress(row[0])))

    def save_fingerprint(self, blob: str, units: dict[str, str] | None) -> None:
        with self._exclusive(), self._db:
            self._db.execute('INSERT OR REPLACE INTO fingerprints VALUES (?, ?)',
                             (blob, zlib.compress(json.dumps(units).encode('utf-8'))))
