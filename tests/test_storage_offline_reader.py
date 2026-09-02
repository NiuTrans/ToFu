"""The diagnostic SQLite lane is physically read-only."""

from __future__ import annotations

import sqlite3

import pytest

from lib.storage_sidecar.offline import open_readonly_sqlite_authority


pytestmark = pytest.mark.unit


def test_offline_reader_reads_existing_authority_and_denies_writes(tmp_path):
    path = tmp_path / 'authority.sqlite3'
    writer = sqlite3.connect(path)
    writer.execute('CREATE TABLE facts(value TEXT)')
    writer.execute('INSERT INTO facts VALUES (?)', ('kept',))
    writer.commit()
    writer.close()

    reader = open_readonly_sqlite_authority(path)
    try:
        assert reader.execute('SELECT value FROM facts').fetchone()[0] == 'kept'
        with pytest.raises(sqlite3.OperationalError):
            reader.execute('DELETE FROM facts')
    finally:
        reader.close()


def test_offline_reader_never_creates_a_missing_database(tmp_path):
    missing = tmp_path / 'missing.sqlite3'
    with pytest.raises(FileNotFoundError):
        open_readonly_sqlite_authority(missing)
    assert not missing.exists()
