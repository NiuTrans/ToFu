"""Execute the production Room save query against SQLite without an Android SDK.

This verifies SQL scope/identity/NULL behavior, not generated Room bindings or an
APK. The query is read from ProfileDao; there is no second maintained SQL copy.
"""
import json
from pathlib import Path
import re
import sqlite3
import unittest


ANDROID = Path(__file__).resolve().parents[1]
SOURCE = ANDROID / "app/src/main/java/com/tofu/client/data/Profile.kt"
SCHEMA = ANDROID / "app/schemas/com.tofu.client.data.ProfileDatabase/3.json"


def query_for(method: str) -> str:
    source = SOURCE.read_text(encoding="utf-8")
    match = re.search(r'@Query\("([^"\n]+)"\)\s+suspend fun ' + re.escape(method) + r'\(', source)
    if match is None:
        raise AssertionError(f"Production DAO query not found: {method}")
    return match.group(1)


class ProfileSaveQueryTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        entity = next(item for item in schema["database"]["entities"] if item["tableName"] == "profiles")
        self.db.execute(entity["createSql"].replace("${TABLE_NAME}", "profiles"))
        self.db.execute("ALTER TABLE profiles ADD COLUMN concurrent_owner_data TEXT")
        self.db.execute("INSERT INTO profiles (id, alias, base_url, auth_type, instance_uuid, "
                        "project_path, cookie_host, last_used_at, concurrent_owner_data) "
                        "VALUES (1, 'old', 'https://old.test/proxy/15005/', "
                        "'CODE_SERVER_PASSWORD', 'old-instance', NULL, 'old.test', 42, 'preserve')")
        self.params = dict(id=1, expectedUrl="https://old.test/proxy/15005/", expectedAlias="old",
                           expectedAuthType="CODE_SERVER_PASSWORD", expectedProjectPath=None,
                           alias="renamed", baseUrl="https://old.test/proxy/15005/",
                           authType="CODE_SERVER_PASSWORD", instanceUuid="old-instance", projectPath=None)

    def tearDown(self):
        self.db.close()

    def row(self):
        return self.db.execute("SELECT * FROM profiles WHERE id=1").fetchone()

    def save(self):
        return self.db.execute(query_for("saveConnectionIfCurrent"), self.params).rowcount

    def test_same_connection_save_preserves_concurrent_session_recency_and_other_owner_columns(self):
        self.db.execute("UPDATE profiles SET cookie_host='fresh.test', last_used_at=999")
        self.assertEqual(1, self.save())
        row = self.row()
        self.assertEqual("renamed", row["alias"])
        self.assertEqual("fresh.test", row["cookie_host"])
        self.assertEqual(999, row["last_used_at"])
        self.assertEqual("preserve", row["concurrent_owner_data"])

    def test_url_or_auth_change_clears_only_the_session_marker(self):
        for field, value in (("baseUrl", "https://new.test/dev/proxy/15006/"), ("authType", "NONE")):
            with self.subTest(field=field):
                self.params[field] = value
                self.assertEqual(1, self.save())
                row = self.row()
                self.assertIsNone(row["cookie_host"])
                self.assertEqual(42, row["last_used_at"])
                self.assertEqual("preserve", row["concurrent_owner_data"])
                self.db.execute("UPDATE profiles SET alias='old', base_url='https://old.test/proxy/15005/', "
                                "auth_type='CODE_SERVER_PASSWORD', cookie_host='old.test'")
                self.params[field] = "https://old.test/proxy/15005/" if field == "baseUrl" else "CODE_SERVER_PASSWORD"

    def test_stale_alias_url_auth_or_project_path_cannot_roll_back_another_save(self):
        for column, value in (("alias", "foreign"), ("base_url", "https://foreign.test/"),
                              ("auth_type", "NONE"), ("project_path", "/foreign/project")):
            with self.subTest(column=column):
                self.db.execute(f"UPDATE profiles SET {column}=?", (value,))
                before = dict(self.row())
                self.assertEqual(0, self.save())
                self.assertEqual(before, dict(self.row()))
                self.db.execute("UPDATE profiles SET alias='old', base_url='https://old.test/proxy/15005/', "
                                "auth_type='CODE_SERVER_PASSWORD', project_path=NULL")

    def test_nullable_project_path_matches_only_the_expected_value(self):
        self.params["projectPath"] = "/new/project"
        self.assertEqual(1, self.save())
        self.assertEqual("/new/project", self.row()["project_path"])
        self.assertEqual(0, self.save())
        self.params.update(expectedAlias="renamed", expectedProjectPath="/new/project", projectPath=None)
        self.assertEqual(1, self.save())
        self.assertIsNone(self.row()["project_path"])

    def test_url_save_cannot_overwrite_a_profile_retyped_as_a_kernel(self):
        for kind in ("LOCAL", "SSH"):
            with self.subTest(kind=kind):
                self.db.execute("UPDATE profiles SET kind=?", (kind,))
                before = dict(self.row())
                self.assertEqual(0, self.save())
                self.assertEqual(before, dict(self.row()))
                self.db.execute("UPDATE profiles SET kind='REMOTE_URL'")

    def test_deleted_profile_cannot_be_resurrected_by_a_save(self):
        self.db.execute("DELETE FROM profiles WHERE id=1")
        self.assertEqual(0, self.save())
        self.assertIsNone(self.row())


if __name__ == "__main__":
    unittest.main(verbosity=2)
