package com.tofu.client.data

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Entity
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.PrimaryKey
import androidx.room.Query
import androidx.room.Update
import kotlinx.coroutines.flow.Flow

/** Auth mechanism used to obtain a session for a server. */
enum class AuthType {
    /** code-server `--auth password`: headless POST /login is replayable. */
    CODE_SERVER_PASSWORD,

    /** Layer-1 interactive SSO: WebView completes login once, we persist the jar. */
    INTERACTIVE_SSO,

    /** No auth (bare Tofu / trusted network). */
    NONE,
}

/**
 * A remembered server.
 *
 * The [alias] is the STABLE logical identity and the switcher key. The secret
 * (in [com.tofu.client.session.SecretStore]) is keyed by [alias], NOT by
 * [baseUrl] — so when a sandbox is re-provisioned the user edits [baseUrl] in
 * one tap and the saved credential is reused.
 *
 * [instanceUuid] is the codelab instance id parsed out of the host (nullable —
 * only present for MLP-style `<uuid>-vscode-<idc>.…` hosts). It lets us detect
 * "same logical server, new URL" and drive the purge-and-relogin path.
 *
 * [cookieHost] records the exact host the current cached session cookie is
 * `Domain`-pinned to. On a [baseUrl] change we compare against it: a mismatch
 * means the cached jar is bound to a dead host and MUST be hard-invalidated.
 */
@Entity(tableName = "profiles")
data class Profile(
    @PrimaryKey(autoGenerate = true) val id: Long = 0,
    @ColumnInfo(name = "alias") val alias: String,
    @ColumnInfo(name = "instance_uuid") val instanceUuid: String? = null,
    @ColumnInfo(name = "base_url") val baseUrl: String,
    @ColumnInfo(name = "auth_type") val authType: AuthType = AuthType.NONE,
    @ColumnInfo(name = "cookie_host") val cookieHost: String? = null,
    @ColumnInfo(name = "last_used_at") val lastUsedAt: Long = 0,
    /**
     * Absolute host path of the Tofu project this server runs from, used by the
     * supervisor to start/stop it remotely. Null → the server is "open only"
     * (no start/stop controls shown). Added in schema v2.
     */
    @ColumnInfo(name = "project_path") val projectPath: String? = null,
)

@Dao
interface ProfileDao {
    @Query("SELECT * FROM profiles ORDER BY last_used_at DESC")
    fun observeAll(): Flow<List<Profile>>

    @Query("SELECT * FROM profiles WHERE id = :id")
    suspend fun getById(id: Long): Profile?

    @Query("SELECT * FROM profiles WHERE alias = :alias LIMIT 1")
    suspend fun getByAlias(alias: String): Profile?

    /** One-shot snapshot (not a Flow) — used to find a same-host stored secret. */
    @Query("SELECT * FROM profiles")
    suspend fun getAllOnce(): List<Profile>

    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insert(profile: Profile): Long

    @Update
    suspend fun update(profile: Profile): Int

    /**
     * Stamp ONLY the session-cookie host, leaving every other column untouched.
     *
     * Deliberately NOT `@Update`: that overwrites the whole row from whatever
     * Profile instance the caller happens to hold. The callers here hold
     * long-lived snapshots — `WebScreen` keeps the profile it was opened with
     * for the entire session, and an SSO sign-in can take minutes (IdP redirect,
     * one-time code). Writing the full row from that snapshot would silently
     * roll back anything edited meanwhile. A targeted write makes "only this
     * column changes" a fact of the query rather than a discipline the caller
     * has to remember.
     */
    @Query("UPDATE profiles SET cookie_host = :host WHERE id = :id")
    suspend fun setCookieHost(id: Long, host: String?): Int

    /**
     * Bump ONLY the recency stamp. Same reasoning as [setCookieHost], and the
     * hotter path of the two: this runs on every card tap, and its caller holds
     * a row rendered from the `observeAll()` Flow — a snapshot that lags any
     * write which has not yet been re-emitted and recomposed. A full-row
     * `@Update` from that snapshot would reinstate every stale column, most
     * damagingly a `cookie_host` that a login had just stamped, silently
     * re-locking the supervisor controls the user had already unlocked.
     */
    @Query("UPDATE profiles SET last_used_at = :timestamp WHERE id = :id")
    suspend fun touchLastUsed(id: Long, timestamp: Long): Int

    /**
     * Flip ONLY the auth type. Used by the launch migration, which iterates a
     * `getAllOnce()` snapshot: the loop suspends on every write, and it runs on
     * `viewModelScope` concurrently with the UI that `setContent` builds
     * moments later — so a card tap can land between the read and the write. A
     * full-row update would then reinstate that row's pre-tap `cookie_host`
     * and `last_used_at`.
     */
    @Query("UPDATE profiles SET auth_type = :authType WHERE id = :id")
    suspend fun setAuthType(id: Long, authType: AuthType): Int

    @Query("DELETE FROM profiles WHERE id = :id")
    suspend fun deleteById(id: Long)
}
