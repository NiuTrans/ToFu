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
 * Proves the Room v1 → v2 migration ([ProfileDatabase.MIGRATION_1_2]) PRESERVES
 * existing saved servers (and therefore the alias-keyed credentials that point
 * at them) while adding the nullable `project_path` column.
 *
 * This is the highest-risk change in the supervisor feature: a botched
 * migration (or the `fallbackToDestructiveMigration` shortcut) would drop the
 * profiles table and orphan every stored secret, forcing users to re-add every
 * server. The test builds a REAL v1 database with a row, then opens Room v2
 * with the migration and asserts via the DAO that the row survived.
 *
 * The project sets `exportSchema = false`, so Room's MigrationTestHelper (which
 * needs exported JSON schemas) is unavailable — instead we hand-build the v1
 * schema (matching Room's generated DDL for the v1 entity) and let Room's own
 * on-open TableInfo validation confirm the migrated schema matches v2.
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

    private fun openV2(): ProfileDatabase {
        val ctx = RuntimeEnvironment.getApplication()
        return Room.databaseBuilder(ctx, ProfileDatabase::class.java, dbName)
            .addMigrations(ProfileDatabase.MIGRATION_1_2)
            .allowMainThreadQueries()
            .build()
    }

    @Test
    fun migration_1_2_preserves_existing_profile_and_adds_null_project_path() {
        val v1 = freshV1Db()
        v1.execSQL(
            "INSERT INTO profiles (alias, instance_uuid, base_url, auth_type, cookie_host, last_used_at) " +
                "VALUES ('zw05', 'uuid-1', 'https://h.example/proxy/15000/', 'CODE_SERVER_PASSWORD', 'h.example', 42)",
        )
        v1.close()

        val db = openV2()
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
        } finally {
            db.close()
        }
    }

    @Test
    fun migration_1_2_project_path_is_writable_after_upgrade() {
        val v1 = freshV1Db()
        v1.execSQL(
            "INSERT INTO profiles (alias, base_url, auth_type, last_used_at) " +
                "VALUES ('zw05', 'https://h.example/proxy/15000/', 'CODE_SERVER_PASSWORD', 1)",
        )
        v1.close()

        val db = openV2()
        try {
            val dao = db.profileDao()
            val original = runBlocking { dao.getByAlias("zw05") }!!
            runBlocking { dao.update(original.copy(projectPath = "/home/dev/chatui")) }
            val updated = runBlocking { dao.getByAlias("zw05") }
            assertEquals("/home/dev/chatui", updated?.projectPath)
        } finally {
            db.close()
        }
    }
}
