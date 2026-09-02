"""Paper artifact ownership is repaired before Sidecar schema publication."""

from __future__ import annotations

import sqlite3

import pytest

from lib.storage_sidecar.adapters.sqlite import SQLiteSession
from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema


pytestmark = pytest.mark.unit


_LEGACY_TABLES = (
    "CREATE TABLE paper_reports (paper_hash TEXT, lang TEXT, report TEXT, "
    "model TEXT, meta TEXT, created_at BIGINT, PRIMARY KEY(paper_hash, lang))",
    "CREATE TABLE paper_translations (paper_hash TEXT, lang TEXT, text TEXT, "
    "model TEXT, created_at BIGINT, PRIMARY KEY(paper_hash, lang))",
    "CREATE TABLE paper_podcasts (paper_hash TEXT, mode TEXT, lang TEXT, "
    "voice TEXT, status TEXT, script_json TEXT, file_path TEXT, "
    "duration_sec DOUBLE PRECISION, model TEXT, tts_model TEXT, meta TEXT, "
    "created_at BIGINT, updated_at BIGINT, "
    "PRIMARY KEY(paper_hash, mode, lang, voice))",
    "CREATE TABLE paper_notes (id TEXT PRIMARY KEY, paper_hash TEXT, lang TEXT, "
    "anchor TEXT, note TEXT, created_at BIGINT, updated_at BIGINT)",
)


@pytest.mark.parametrize("published_version", [None, 32, 33])
def test_legacy_paper_tables_are_owner_scoped_before_schema_is_accepted(
    published_version,
):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    for statement in _LEGACY_TABLES:
        connection.execute(statement)
    if published_version is not None:
        connection.execute(
            "CREATE TABLE storage_meta (meta_key TEXT PRIMARY KEY, meta_value TEXT)"
        )
        connection.execute(
            "INSERT INTO storage_meta(meta_key, meta_value) VALUES (?, ?)",
            ("schema_version", str(published_version)),
        )

    initialize_schema(SQLiteSession(connection))

    for table_name in (
        "paper_reports",
        "paper_translations",
        "paper_podcasts",
        "paper_notes",
    ):
        columns = {
            row["name"]
            for row in connection.execute(f'PRAGMA table_info("{table_name}")')
        }
        assert "user_id" in columns
    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key = 'schema_version'"
    ).fetchone()[0]
    assert int(version) == SCHEMA_VERSION == 40
