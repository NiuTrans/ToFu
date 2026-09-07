package com.tofu.client.data

import androidx.room.Database
import androidx.room.RoomDatabase
import androidx.room.migration.Migration
import androidx.sqlite.db.SupportSQLiteDatabase

@Database(entities = [Profile::class], version = 2, exportSchema = false)
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
    }
}
