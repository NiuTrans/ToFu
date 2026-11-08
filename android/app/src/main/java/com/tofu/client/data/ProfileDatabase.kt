package com.tofu.client.data

import androidx.room.Database
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

/**
 * The profile store. Schemas are exported to `android/app/schemas/` (one JSON
 * per version) so `MigrationTestHelper` can rebuild any past version and prove
 * each migration against the schema Room itself generated.
 */
@Database(entities = [Profile::class], version = 3, exportSchema = true)
abstract class ProfileDatabase : RoomDatabase() {
    abstract fun profileDao(): ProfileDao

    companion object {
        /**
         * v1 → v2: add the nullable `project_path` column (supervisor start/stop).
         *
         * A plain additive ALTER — existing rows keep every value and get
         * project_path = NULL. This is the ONLY safe migration for saved
         * servers + their credentials: `fallbackToDestructiveMigration` would
         * drop the profiles table and orphan the alias-keyed secrets, forcing
         * every user to re-add every server. Explicit migration only.
         */
        val MIGRATION_1_2 = object : Migration(1, 2) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE profiles ADD COLUMN project_path TEXT")
            }
        }

        /**
         * v2 → v3: the kernel model. Every existing row becomes a REMOTE_URL
         * kernel through the column default, keeping every other value; the
         * SSH endpoint columns are added NULL (no endpoint). Additive only, for
         * the same reason as [MIGRATION_1_2].
         */
        val MIGRATION_2_3 = object : Migration(2, 3) {
            override fun migrate(db: SupportSQLiteDatabase) {
                db.execSQL("ALTER TABLE profiles ADD COLUMN kind TEXT NOT NULL DEFAULT 'REMOTE_URL'")
                db.execSQL("ALTER TABLE profiles ADD COLUMN ssh_host TEXT")
                db.execSQL("ALTER TABLE profiles ADD COLUMN ssh_port INTEGER")
                db.execSQL("ALTER TABLE profiles ADD COLUMN ssh_user TEXT")
                db.execSQL("ALTER TABLE profiles ADD COLUMN ssh_host_key_sha256 TEXT")
                db.execSQL("ALTER TABLE profiles ADD COLUMN ssh_remote_port INTEGER")
            }
        }

        /** Every migration, in order — the one list the app's builder registers. */
        val ALL_MIGRATIONS: Array<Migration> = arrayOf(MIGRATION_1_2, MIGRATION_2_3)
    }
}
