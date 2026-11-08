"""Private persistence for reconstructible landing-test evidence.

Responsibility: own the SQLite cache file, its schema, record codecs and
transactions. This cache opens only the explicit file it is given, never the
application's selected authority. Verification can run before the application
or its storage service starts.

Entry points: ``ImpactCache``, ``Record`` and ``ImpactCacheError``. Callers
supply observed test records and Git-blob fingerprints; no SQL or connection
crosses this boundary. Dependencies: standard library only.
Contract: docs/modules/project_integration.md, "Landing verification".
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
from pathlib import Path
import sqlite3
import time
import zlib

# Bump only when existing records become unusable: a mismatch drops them and
# the next landings record again from nothing.
SCHEMA_VERSION = 2


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
    """Records and blob fingerprints in one local SQLite file.

    An interpreter-environment change invalidates test records while retaining
    content-addressed fingerprints.
    """

    def __init__(self, path, environment_key: str):
        self.path = Path(path)
        with _cache_errors():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(self.path))
            try:
                self._db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, '
                                 'value TEXT)')
                schema = self._meta('schema')
                if schema is None or int(schema) != SCHEMA_VERSION:
                    with self._db:
                        self._db.execute('DROP TABLE IF EXISTS records')
                        self._db.execute('DROP TABLE IF EXISTS fingerprints')
                        self._db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                                         ('schema', str(SCHEMA_VERSION)))
                self._db.execute('CREATE TABLE IF NOT EXISTS records (test TEXT PRIMARY KEY, '
                                 'tree TEXT, outcome TEXT, failed TEXT, opaque INTEGER, deps BLOB, '
                                 'dirs BLOB, recorded_at INTEGER)')
                self._db.execute('CREATE TABLE IF NOT EXISTS fingerprints (blob TEXT PRIMARY KEY, '
                                 'units BLOB)')
                if self._meta('environment') != environment_key:
                    with self._db:
                        self._db.execute('DELETE FROM records')
                        self._db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)',
                                         ('environment', environment_key))
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    self._db.close()
                raise

    def _meta(self, key):
        row = self._db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def close(self) -> None:
        with _cache_errors():
            self._db.close()

    def records(self) -> dict[str, Record]:
        with _cache_errors():
            found = {}
            for test, tree, outcome, failed, opaque, deps, dirs in self._db.execute(
                    'SELECT test, tree, outcome, failed, opaque, deps, dirs FROM records'):
                decoded = json.loads(zlib.decompress(deps))
                found[test] = Record(test, tree, outcome, json.loads(failed), bool(opaque),
                                     {path: (value[0], value[1]) for path, value in decoded.items()},
                                     json.loads(zlib.decompress(dirs)))
            return found

    def save(self, records: list[Record]) -> None:
        with _cache_errors():
            rows = [(record.test, record.tree, record.outcome, json.dumps(record.failed),
                     int(record.opaque),
                     zlib.compress(json.dumps({path: [blob, names] for path, (blob, names)
                                               in record.deps.items()}).encode('utf-8')),
                     zlib.compress(json.dumps(record.dirs).encode('utf-8')), int(time.time()))
                    for record in records]
            with self._db:
                self._db.executemany('INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                                     rows)

    def fingerprint(self, blob: str) -> tuple[bool, dict[str, str] | None]:
        """Return (found, units); a cached unparsable blob has units=None."""
        with _cache_errors():
            row = self._db.execute('SELECT units FROM fingerprints WHERE blob=?',
                                   (blob,)).fetchone()
            return (False, None) if row is None else (True, json.loads(zlib.decompress(row[0])))

    def save_fingerprint(self, blob: str, units: dict[str, str] | None) -> None:
        with _cache_errors(), self._db:
            self._db.execute('INSERT OR REPLACE INTO fingerprints VALUES (?, ?)',
                             (blob, zlib.compress(json.dumps(units).encode('utf-8'))))
