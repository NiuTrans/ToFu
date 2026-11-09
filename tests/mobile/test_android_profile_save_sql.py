"""Execute the Android DAO and migration SQL, not a maintained query copy.

This smallest CORE gate needs only Python/SQLite. It proves SQL scope and the
exported v3 schema, not generated Room bindings, an Android SDK, or an APK.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sqlite3

import pytest


pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "android/app/src/main/java/com/tofu/client/data"
SCHEMAS = ROOT / "android/app/schemas/com.tofu.client.data.ProfileDatabase"


def query_for(method: str) -> str:
    source = (DATA / "Profile.kt").read_text(encoding="utf-8")
    declaration = re.search(r"suspend fun " + re.escape(method) + r"\(", source)
    assert declaration is not None, f"Production DAO method not found: {method}"
    start = source.rfind("@Query(", 0, declaration.start())
    assert start >= 0, f"Production DAO query not found: {method}"
    annotation = source[start:declaration.start()]
    return "".join(json.loads(part) for part in re.findall(r'"(?:[^"\\]|\\.)*"', annotation))


def create_sql(version: int) -> str:
    schema = json.loads((SCHEMAS / f"{version}.json").read_text(encoding="utf-8"))
    entity = next(item for item in schema["database"]["entities"] if item["tableName"] == "profiles")
    return entity["createSql"].replace("${TABLE_NAME}", "profiles")


def migration_sql(start: int, end: int) -> list[str]:
    source = (DATA / "ProfileDatabase.kt").read_text(encoding="utf-8")
    body = source.split(f"val MIGRATION_{start}_{end} =", 1)[1].split("\n        val ", 1)[0]
    return [json.loads(part) for part in re.findall(r'db\.execSQL\(("(?:[^"\\]|\\.)*")\)', body)]


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(create_sql(3))
    connection.execute("ALTER TABLE profiles ADD COLUMN unrelated_owner_data TEXT")
    yield connection
    connection.close()


def seed(db, kind="REMOTE_URL"):
    values = dict(
        id=7, alias="old", instance_uuid="instance", base_url="https://old.test/proxy/15000/",
        auth_type="CODE_SERVER_PASSWORD", cookie_host="fresh.test", last_used_at=999,
        project_path=None, kind=kind, ssh_host="host.test", ssh_port=22, ssh_user="me",
        ssh_host_key_sha256=None, ssh_remote_port=15000, unrelated_owner_data="preserve",
    )
    if kind != "REMOTE_URL":
        values["base_url"] = ""
        values["cookie_host"] = None
    columns = ", ".join(values)
    bindings = ", ".join(f":{column}" for column in values)
    db.execute(f"INSERT INTO profiles ({columns}) VALUES ({bindings})", values)
    return values


def row(db):
    found = db.execute("SELECT * FROM profiles WHERE id=7").fetchone()
    return dict(found) if found is not None else None


def url_params(before):
    return dict(
        id=before["id"], expectedUrl=before["base_url"], expectedAlias=before["alias"],
        expectedAuthType=before["auth_type"], expectedProjectPath=before["project_path"],
        alias="renamed", baseUrl=before["base_url"], authType=before["auth_type"],
        instanceUuid=before["instance_uuid"], projectPath=before["project_path"],
    )


def ssh_params(before):
    return dict(
        id=before["id"], expectedUrl=before["base_url"], expectedAlias=before["alias"],
        expectedAuthType=before["auth_type"], expectedProjectPath=before["project_path"],
        expectedHost=before["ssh_host"], expectedPort=before["ssh_port"],
        expectedUser=before["ssh_user"], expectedRemotePort=before["ssh_remote_port"],
        alias="renamed", host=before["ssh_host"], port=before["ssh_port"],
        user="reviewed-user", remotePort=16000,
    )


@pytest.mark.parametrize("kind", ["LOCAL", "SSH"])
def test_url_save_rejects_a_concurrent_kind_change(db, kind):
    before = seed(db)
    db.execute("UPDATE profiles SET kind=?", (kind,))
    current = row(db)
    assert db.execute(query_for("saveConnectionIfCurrent"), url_params(before)).rowcount == 0
    assert row(db) == current


@pytest.mark.parametrize("kind", ["LOCAL", "SSH"])
def test_late_gateway_cookie_stamp_is_remote_url_only(db, kind):
    before = seed(db)
    db.execute("UPDATE profiles SET kind=?", (kind,))
    current = row(db)
    params = dict(id=7, expectedUrl=before["base_url"], expectedAlias="old",
                  expectedAuthType=before["auth_type"], host="late.test")
    assert db.execute(query_for("setCookieHostIfCurrent"), params).rowcount == 0
    assert row(db) == current


def test_same_connection_url_save_preserves_concurrent_metadata_and_all_ssh_columns(db):
    before = seed(db)
    db.execute("UPDATE profiles SET ssh_host_key_sha256='SHA256:concurrent'")
    current = row(db)
    assert db.execute(query_for("saveConnectionIfCurrent"), url_params(before)).rowcount == 1
    expected = {**current, "alias": "renamed"}
    assert row(db) == expected


@pytest.mark.parametrize("field,value", [("baseUrl", "https://new.test/"), ("authType", "NONE")])
def test_url_or_auth_change_invalidates_only_the_cookie_marker(db, field, value):
    before = seed(db)
    params = {**url_params(before), field: value}
    assert db.execute(query_for("saveConnectionIfCurrent"), params).rowcount == 1
    after = row(db)
    assert after["cookie_host"] is None
    for column in ("last_used_at", "ssh_host", "ssh_port", "ssh_user", "ssh_host_key_sha256",
                   "ssh_remote_port", "unrelated_owner_data"):
        assert after[column] == before[column]


@pytest.mark.parametrize("column,value", [
    ("alias", "foreign"), ("base_url", "https://foreign.test/"), ("auth_type", "NONE"),
    ("project_path", "/foreign/project"),
])
def test_url_save_rejects_changed_reviewed_fields(db, column, value):
    before = seed(db)
    db.execute(f"UPDATE profiles SET {column}=?", (value,))
    current = row(db)
    assert db.execute(query_for("saveConnectionIfCurrent"), url_params(before)).rowcount == 0
    assert row(db) == current


def test_ssh_edit_preserves_a_pin_acquired_after_the_editor_read(db):
    before = seed(db, "SSH")
    db.execute("UPDATE profiles SET ssh_host_key_sha256='SHA256:acquired', "
               "last_used_at=1234, cookie_host='owner-marker'")
    current = row(db)
    assert db.execute(query_for("saveSshConnectionIfCurrent"), ssh_params(before)).rowcount == 1
    assert row(db) == {**current, "alias": "renamed", "ssh_user": "reviewed-user", "ssh_remote_port": 16000}


@pytest.mark.parametrize("field,value", [("host", "reviewed.test"), ("port", 2222)])
def test_reviewed_ssh_host_or_port_change_clears_even_a_concurrently_acquired_pin(db, field, value):
    before = seed(db, "SSH")
    db.execute("UPDATE profiles SET ssh_host_key_sha256='SHA256:acquired'")
    params = {**ssh_params(before), field: value}
    assert db.execute(query_for("saveSshConnectionIfCurrent"), params).rowcount == 1
    assert row(db)["ssh_host_key_sha256"] is None


@pytest.mark.parametrize("column,value", [
    ("kind", "REMOTE_URL"), ("kind", "LOCAL"), ("alias", "foreign"),
    ("base_url", "http://127.0.0.2:40000/"), ("auth_type", "NONE"),
    ("project_path", "/foreign/project"), ("ssh_host", "foreign.test"),
    ("ssh_port", 2222), ("ssh_user", "foreign"), ("ssh_remote_port", 18000),
])
def test_ssh_edit_rejects_a_changed_source_kind_or_configuration(db, column, value):
    before = seed(db, "SSH")
    db.execute(f"UPDATE profiles SET {column}=?", (value,))
    current = row(db)
    assert db.execute(query_for("saveSshConnectionIfCurrent"), ssh_params(before)).rowcount == 0
    assert row(db) == current


@pytest.mark.parametrize("kind,method,parameters", [
    ("REMOTE_URL", "saveConnectionIfCurrent", url_params),
    ("SSH", "saveSshConnectionIfCurrent", ssh_params),
])
def test_conditional_edits_do_not_resurrect_deleted_rows(db, kind, method, parameters):
    before = seed(db, kind)
    db.execute("DELETE FROM profiles WHERE id=7")
    assert db.execute(query_for(method), parameters(before)).rowcount == 0
    assert row(db) is None


@pytest.mark.parametrize("start", [1, 2])
def test_actual_additive_migrations_preserve_rows_and_match_exported_v3(start):
    with sqlite3.connect(":memory:") as migrated, sqlite3.connect(":memory:") as fresh:
        ddl = create_sql(2)
        if start == 1:
            ddl = ddl.replace(", `project_path` TEXT", "")
        migrated.execute(ddl)
        migrated.execute("INSERT INTO profiles (alias, base_url, auth_type, last_used_at, cookie_host) "
                         "VALUES ('legacy', 'https://legacy.test/', 'NONE', 42, 'legacy.test')")
        before = migrated.execute("SELECT id, alias, base_url, auth_type, last_used_at, cookie_host FROM profiles").fetchall()
        if start == 1:
            for sql in migration_sql(1, 2):
                migrated.execute(sql)
        for sql in migration_sql(2, 3):
            migrated.execute(sql)
        fresh.execute(create_sql(3))
        assert migrated.execute("PRAGMA table_info(profiles)").fetchall() == fresh.execute("PRAGMA table_info(profiles)").fetchall()
        assert migrated.execute("SELECT id, alias, base_url, auth_type, last_used_at, cookie_host FROM profiles").fetchall() == before
        assert migrated.execute("SELECT kind, ssh_host, ssh_port, ssh_user, ssh_host_key_sha256, ssh_remote_port "
                                "FROM profiles").fetchone() == ("REMOTE_URL", None, None, None, None, None)
