package com.tofu.client.data

import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

/**
 * Proves the migration chain from the oldest schema, v1 → v2 → v3
 * ([ProfileDatabase.ALL_MIGRATIONS]), PRESERVES existing saved servers (and
 * therefore the alias-keyed credentials that point at them): v2 adds the
 * nullable `project_path`, v3 makes every row a REMOTE_URL kernel.
 *
 * A botched migration (or the `fallbackToDestructiveMigration` shortcut) would
 * drop the profiles table and orphan every stored secret, forcing users to
 * re-add every server. The test builds a REAL v1 database with a row, then
 * opens the current Room schema through the migrations and asserts via the
 * DAO that the row survived.
 *
 * v1 predates exported schemas, so it is hand-built here (matching Room's
 * generated DDL for the v1 entity) and Room's own on-open TableInfo validation
 * confirms the migrated schema matches the current entity. The v2 → v3 step is
 * also proven against Room's exported JSON by [ProfileSchemaMigrationTest].
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ProfileMigrationTest {

    private val dbName = "migration-test.db"

    private fun freshV1Db(): SQLiteDatabase {
        val ctx = RuntimeEnvironment.getApplication()
        ctx.deleteDatabase(dbName)
        val path = ctx.getDatabasePath(dbName)
        path.parentFile?.mkdirs()
        val db = SQLiteDatabase.openOrCreateDatabase(path, null)
        // Room's generated v1 DDL for the Profile entity (pre project_path).
        db.execSQL(
            "CREATE TABLE profiles (" +
                "id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, " +
                "alias TEXT NOT NULL, " +
                "instance_uuid TEXT, " +
                "base_url TEXT NOT NULL, " +
                "auth_type TEXT NOT NULL, " +
                "cookie_host TEXT, " +
                "last_used_at INTEGER NOT NULL)",
        )
        db.version = 1
        return db
    }

    private fun openCurrent(): ProfileDatabase {
        val ctx = RuntimeEnvironment.getApplication()
        return Room.databaseBuilder(ctx, ProfileDatabase::class.java, dbName)
            .addMigrations(*ProfileDatabase.ALL_MIGRATIONS)
            .allowMainThreadQueries()
            .build()
    }

    @Test
    fun migrations_from_v1_preserve_existing_profile_as_a_remote_url_kernel() {
        val v1 = freshV1Db()
        v1.execSQL(
            "INSERT INTO profiles (alias, instance_uuid, base_url, auth_type, cookie_host, last_used_at) " +
                "VALUES ('zw05', 'uuid-1', 'https://h.example/proxy/15000/', 'CODE_SERVER_PASSWORD', 'h.example', 42)",
        )
        v1.close()

        val db = openCurrent()
        try {
            val all = runBlocking { db.profileDao().getAllOnce() }
            assertEquals("the v1 row must survive the migration", 1, all.size)
            val p = all[0]
            assertEquals("zw05", p.alias)
            assertEquals("uuid-1", p.instanceUuid)
            assertEquals("https://h.example/proxy/15000/", p.baseUrl)
            assertEquals(AuthType.CODE_SERVER_PASSWORD, p.authType)
            assertEquals("h.example", p.cookieHost)
            assertEquals(42L, p.lastUsedAt)
            // The new column is present and defaults to NULL for pre-existing rows.
            assertNull("project_path must be null on a migrated row", p.projectPath)
            assertEquals(KernelKind.REMOTE_URL, p.kind)
            assertNull("a migrated row has no SSH endpoint", p.ssh)
        } finally {
            db.close()
        }
    }

    @Test
    fun project_path_is_writable_after_upgrade_from_v1() {
        val v1 = freshV1Db()
        v1.execSQL(
            "INSERT INTO profiles (alias, base_url, auth_type, last_used_at) " +
                "VALUES ('zw05', 'https://h.example/proxy/15000/', 'CODE_SERVER_PASSWORD', 1)",
        )
        v1.close()

        val db = openCurrent()
        try {
            val dao = db.profileDao()
            val original = runBlocking { dao.getByAlias("zw05") }!!
            runBlocking { dao.update(original.copy(projectPath = "/home/dev/tofu")) }
            val updated = runBlocking { dao.getByAlias("zw05") }
            assertEquals("/home/dev/tofu", updated?.projectPath)
        } finally {
            db.close()
        }
    }

    @Test fun conditional_session_write_preserves_edits_and_rejects_deleted_profiles() = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(RuntimeEnvironment.getApplication(), ProfileDatabase::class.java)
            .allowMainThreadQueries().build()
        try {
            val dao = db.profileDao()
            val id = dao.insert(Profile(alias = "test", baseUrl = "https://old.example/proxy/15000/"))
            val original = dao.getById(id)!!
            dao.update(original.copy(baseUrl = "https://new.example/proxy/15000/", alias = "renamed"))
            assertEquals(0, dao.setCookieHostIfCurrent(id, original.baseUrl, original.alias, original.authType, "old.example"))
            assertNull(dao.getById(id)!!.cookieHost)
            assertEquals(1, dao.setCookieHostIfCurrent(id, "https://new.example/proxy/15000/", "renamed", original.authType, "new.example"))
            assertEquals("renamed", dao.getById(id)!!.alias)
            dao.deleteById(id)
            assertEquals(0, dao.setCookieHostIfCurrent(id, "https://new.example/proxy/15000/", "renamed", original.authType, "new.example"))
        } finally { db.close() }
    }

    @Test fun ssh_kernels_round_trip_and_url_kernels_have_no_endpoint() = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(RuntimeEnvironment.getApplication(), ProfileDatabase::class.java)
            .allowMainThreadQueries().build()
        try {
            val dao = db.profileDao()
            val endpoint = SshEndpoint(host = "dev.example", user = "me")
            val sshId = dao.insert(Profile(alias = "box", baseUrl = "", kind = KernelKind.SSH, ssh = endpoint))
            val urlId = dao.insert(Profile(alias = "url", baseUrl = "https://u.example/"))
            val ssh = dao.getById(sshId)!!
            assertEquals(KernelKind.SSH, ssh.kind)
            assertEquals(SshEndpoint(host = "dev.example", port = 22, user = "me", hostKeySha256 = null, remotePort = 15000), ssh.ssh)
            val pinned = endpoint.copy(hostKeySha256 = "SHA256:abc")
            dao.update(ssh.copy(ssh = pinned))
            assertEquals(pinned, dao.getById(sshId)!!.ssh)
            val url = dao.getById(urlId)!!
            assertEquals(KernelKind.REMOTE_URL, url.kind)
            assertNull("every ssh_ column NULL reads back as no endpoint", url.ssh)
        } finally { db.close() }
    }

    @Test fun ssh_host_key_pins_once_and_only_for_the_endpoint_it_was_shown_for() = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(RuntimeEnvironment.getApplication(), ProfileDatabase::class.java)
            .allowMainThreadQueries().build()
        try {
            val dao = db.profileDao()
            val id = dao.insert(Profile(alias = "box", baseUrl = "", kind = KernelKind.SSH, ssh = SshEndpoint(host = "dev.example", user = "me")))
            assertEquals("another port is another machine", 0, dao.pinSshHostKey(id, "dev.example", 2222, "SHA256:a"))
            assertEquals(1, dao.pinSshHostKey(id, "dev.example", 22, "SHA256:a"))
            assertEquals("an existing pin is never replaced", 0, dao.pinSshHostKey(id, "dev.example", 22, "SHA256:b"))
            assertEquals("SHA256:a", dao.getById(id)!!.ssh?.hostKeySha256)

            val urlId = dao.insert(Profile(alias = "url", baseUrl = "https://u.example/"))
            assertEquals(0, dao.pinSshHostKey(urlId, "u.example", 22, "SHA256:a"))
            assertNull(dao.getById(urlId)!!.ssh)
        } finally { db.close() }
    }

    @Test fun conditional_session_write_rejects_auth_and_alias_changes_at_same_url() = runBlocking {
        val db = Room.inMemoryDatabaseBuilder(RuntimeEnvironment.getApplication(), ProfileDatabase::class.java)
            .allowMainThreadQueries().build()
        try {
            val dao = db.profileDao()
            val id = dao.insert(Profile(alias = "test", baseUrl = "https://host.example/", authType = AuthType.CODE_SERVER_PASSWORD))
            val original = dao.getById(id)!!
            for (edited in listOf(original.copy(alias = "renamed"), original.copy(authType = AuthType.NONE))) {
                dao.update(edited)
                assertEquals(0, dao.setCookieHostIfCurrent(id, original.baseUrl, original.alias, original.authType, "host.example"))
                assertNull(dao.getById(id)!!.cookieHost)
                assertEquals(edited, dao.getById(id))
            }
        } finally { db.close() }
    }
}
