package com.tofu.client.data

import android.content.ContentValues
import android.database.sqlite.SQLiteDatabase
import androidx.room.Room
import androidx.room.testing.MigrationTestHelper
import androidx.test.platform.app.InstrumentationRegistry
import com.tofu.client.BuildConfig
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

/**
 * v2 → v3 ([ProfileDatabase.MIGRATION_2_3]) against the schemas Room itself
 * exported to `android/app/schemas/`: the v2 database is built from `2.json`
 * (the schema installed apps hold today) and the migrated result must equal
 * `3.json` — the same schema a fresh install creates.
 *
 * The exported schemas reach MigrationTestHelper as debug-only assets (see
 * `app/build.gradle.kts`), so this runs in the debug unit-test variant and is
 * reported as skipped in the release one.
 */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33])
class ProfileSchemaMigrationTest {

    private val dbName = "schema-migration-test.db"

    @get:Rule
    val helper = MigrationTestHelper(
        InstrumentationRegistry.getInstrumentation(),
        ProfileDatabase::class.java,
    )

    @Before
    fun schemasArePackaged() {
        assumeTrue("exported Room schemas are debug-only test assets", BuildConfig.DEBUG)
    }

    private fun v2Row(
        alias: String,
        baseUrl: String,
        authType: AuthType,
        instanceUuid: String?,
        cookieHost: String?,
        lastUsedAt: Long,
        projectPath: String?,
    ) = ContentValues().apply {
        put("alias", alias)
        put("instance_uuid", instanceUuid)
        put("base_url", baseUrl)
        put("auth_type", authType.name)
        put("cookie_host", cookieHost)
        put("last_used_at", lastUsedAt)
        put("project_path", projectPath)
    }

    @Test
    fun migration_2_3_keeps_every_v2_value_and_makes_rows_remote_url_kernels() {
        helper.createDatabase(dbName, 2).apply {
            insert("profiles", SQLiteDatabase.CONFLICT_ABORT, v2Row(
                alias = "zw05", baseUrl = "https://h.example/proxy/15000/",
                authType = AuthType.CODE_SERVER_PASSWORD, instanceUuid = "uuid-1",
                cookieHost = "h.example", lastUsedAt = 42, projectPath = "/home/dev/tofu",
            ))
            insert("profiles", SQLiteDatabase.CONFLICT_ABORT, v2Row(
                alias = "nas", baseUrl = "http://mynas.local:8080",
                authType = AuthType.NONE, instanceUuid = null,
                cookieHost = null, lastUsedAt = 7, projectPath = null,
            ))
            close()
        }

        // Validates the migrated schema against 3.json (validateDroppedTables too).
        val migrated = helper.runMigrationsAndValidate(dbName, 3, true, ProfileDatabase.MIGRATION_2_3)
        migrated.query(
            "SELECT alias, kind, ssh_host, ssh_port, ssh_user, ssh_host_key_sha256, ssh_remote_port " +
                "FROM profiles ORDER BY id",
        ).use { c ->
            assertEquals(2, c.count)
            while (c.moveToNext()) {
                assertEquals("row ${c.getString(0)}", "REMOTE_URL", c.getString(1))
                for (column in 2..6) assertTrue("row ${c.getString(0)} column $column", c.isNull(column))
            }
        }
        migrated.close()

        // The app's own builder reads the migrated rows with every v2 field intact.
        val db = Room.databaseBuilder(
            InstrumentationRegistry.getInstrumentation().targetContext, ProfileDatabase::class.java, dbName,
        ).addMigrations(*ProfileDatabase.ALL_MIGRATIONS).allowMainThreadQueries().build()
        try {
            val dao = db.profileDao()
            val zw05 = runBlocking { dao.getByAlias("zw05") }!!
            assertEquals(
                Profile(
                    id = zw05.id, alias = "zw05", instanceUuid = "uuid-1",
                    baseUrl = "https://h.example/proxy/15000/", authType = AuthType.CODE_SERVER_PASSWORD,
                    cookieHost = "h.example", lastUsedAt = 42, projectPath = "/home/dev/tofu",
                    kind = KernelKind.REMOTE_URL, ssh = null,
                ),
                zw05,
            )
            val nas = runBlocking { dao.getByAlias("nas") }!!
            assertEquals(
                Profile(
                    id = nas.id, alias = "nas", baseUrl = "http://mynas.local:8080",
                    authType = AuthType.NONE, lastUsedAt = 7,
                ),
                nas,
            )
        } finally {
            db.close()
        }
    }
}
