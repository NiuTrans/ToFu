"""Read-guard metadata cannot silently exempt arbitrary source user rows."""

import sqlite3
import pytest
from lib.storage_authority_sql.migration.tofudb_migration_coverage import source_table_coverage
from lib.storage_authority_sql.migration.tofudb_migration_source import SQLiteMigrationSource

pytestmark = pytest.mark.unit


def coverage(
    tmp_path, *, source_id="a" * 32, appends=1, extra_column=False, mapped_events=True
):
    path = tmp_path / "source.db"
    with sqlite3.connect(path) as c:
        c.execute(
            "CREATE TABLE storage_events(task_id TEXT,sequence BIGINT,event_json BLOB,PRIMARY KEY(task_id,sequence))"
        )
        extra = ", body TEXT" if extra_column else ""
        c.execute(
            "CREATE TABLE storage_event_read_guards(task_id TEXT PRIMARY KEY,source_id TEXT,append_version BIGINT,prune_version BIGINT"
            + extra
            + ")"
        )
        c.execute(
            "INSERT INTO storage_event_read_guards(task_id,source_id,append_version,prune_version) VALUES (?,?,?,?)",
            ("task", source_id, appends, 0),
        )
    names = ["storage_events"] if mapped_events else []
    with SQLiteMigrationSource(path) as source:
        return source_table_coverage(
            source,
            {"id": "test", "domains": [{"tables": [{"name": n} for n in names]}]},
        )


def test_exact_lease_metadata_is_rebuilt_only_with_mapped_events(tmp_path):
    report = coverage(tmp_path)
    assert report["sourceTableCoverageComplete"]
    assert report["verifiedRebuilds"]["storage_event_read_guards"][
        "archivedInputTables"
    ] == ["storage_events"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_id": "user data"},
        {"source_id": b"a" * 32},
        {"appends": -1},
        {"appends": 1.5},
        {"extra_column": True},
        {"mapped_events": False},
    ],
)
def test_unknown_or_malformed_guard_rows_remain_blockers(tmp_path, kwargs):
    report = coverage(tmp_path, **kwargs)
    assert not report["sourceTableCoverageComplete"]
    assert "storage_event_read_guards" in report["unmappedPopulatedTables"]
