package com.tofu.client.data

import androidx.room.ColumnInfo
import androidx.room.Dao
import androidx.room.Embedded
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
 * Where a kernel — a Tofu server shown in the WebView — runs, and therefore
 * which [com.tofu.client.session.KernelController] owns its lifecycle.
 * Stored by name; every row written before schema v3 is [REMOTE_URL].
 */
enum class KernelKind {
    /** A server reached at [Profile.baseUrl], with the gateway auth of [AuthType]. */
    REMOTE_URL,

    /** The Tofu server running on this phone ("This phone"). */
    LOCAL,

    /** A remote Tofu reached through an SSH tunnel the phone dials out ([Profile.ssh]). */
    SSH,
}

/**
 * The SSH carrier of an [KernelKind.SSH] kernel: the phone dials
 * [user]@[host]:[port] and forwards to the remote Tofu on 127.0.0.1:[remotePort].
 *
 * Holds no credential. The password or private key lives in the vault, keyed
 * like every other secret; [hostKeySha256] is the PUBLIC host key's SHA-256
 * fingerprint, pinned on first trust and null until then.
 */
data class SshEndpoint(
    @ColumnInfo(name = "host") val host: String,
    @ColumnInfo(name = "port") val port: Int = DEFAULT_PORT,
    @ColumnInfo(name = "user") val user: String,
    @ColumnInfo(name = "host_key_sha256") val hostKeySha256: String? = null,
    @ColumnInfo(name = "remote_port") val remotePort: Int = DEFAULT_REMOTE_PORT,
) {
    companion object {
        /** The SSH protocol's registered port. */
        const val DEFAULT_PORT = 22

        /** Tofu's default listen port (`server.py`, `PORT`). */
        const val DEFAULT_REMOTE_PORT = 15000
    }
}

/**
 * A remembered kernel: a Tofu server the app can put in front, of the given
 * [kind]. Every row written before schema v3 is a [KernelKind.REMOTE_URL].
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
 *
 * Only [KernelKind.REMOTE_URL] rows have a stored address. A LOCAL or SSH
 * kernel's address is runtime state owned by its
 * [com.tofu.client.session.KernelController] (`ensureRunning`), so those rows
 * keep [baseUrl] empty and the session works on a copy bound to the live
 * endpoint, which is never written back.
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
    /** Added in schema v3; rows from earlier versions are [KernelKind.REMOTE_URL]. */
    @ColumnInfo(name = "kind", defaultValue = "REMOTE_URL") val kind: KernelKind = KernelKind.REMOTE_URL,
    /**
     * Set only for [KernelKind.SSH] (schema v3). Its columns are all NULL on
     * other rows, which Room reads back as a null endpoint — so none of them
     * may carry a column default.
     */
    @Embedded(prefix = "ssh_") val ssh: SshEndpoint? = null,
)

/**
 * Saved connection authority, not transport state. Recency, cookie stamps,
 * derived instance metadata and a first-trust pin are not reviewed config.
 * For URL/LOCAL rows, SSH columns belong to another owner and are ignored.
 * Compare source rows here; a runtime-bound LOCAL/SSH copy must first have
 * its transient URL removed, never persisted.
 */
internal fun Profile.sameConnectionConfiguration(other: Profile): Boolean =
    id == other.id && kind == other.kind && alias == other.alias &&
        baseUrl == other.baseUrl && authType == other.authType && projectPath == other.projectPath &&
        (kind != KernelKind.SSH || ssh?.copy(hostKeySha256 = null) == other.ssh?.copy(hostKeySha256 = null))

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
     * Save only reviewed connection fields, conditional on the row the editor
     * read. Recency, unrelated owner columns and a same-connection session stamp
     * survive concurrent writes. URL/auth changes invalidate only the marker.
     * No whole-row rollback or schema migration is needed.
     */
    @Query("UPDATE profiles SET alias = :alias, base_url = :baseUrl, auth_type = :authType, instance_uuid = :instanceUuid, project_path = :projectPath, cookie_host = CASE WHEN base_url = :baseUrl AND auth_type = :authType THEN cookie_host ELSE NULL END WHERE id = :id AND kind = 'REMOTE_URL' AND base_url = :expectedUrl AND alias = :expectedAlias AND auth_type = :expectedAuthType AND project_path IS :expectedProjectPath")
    suspend fun saveConnectionIfCurrent(
        id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType,
        expectedProjectPath: String?, alias: String, baseUrl: String, authType: AuthType,
        instanceUuid: String?, projectPath: String?,
    ): Int

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

    /** Guard late network completions against an edited/deleted profile. */
    @Query("UPDATE profiles SET cookie_host = :host WHERE id = :id AND kind = 'REMOTE_URL' AND base_url = :expectedUrl AND alias = :expectedAlias AND auth_type = :expectedAuthType")
    suspend fun setCookieHostIfCurrent(id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType, host: String): Int

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

    /**
     * Save only reviewed SSH fields against the endpoint-free source row.
     * A trust pin acquired after the editor's read belongs to the same host
     * and port: preserve the database's pin, never the snapshot's. A reviewed
     * host/port change clears it. Recency and unrelated owner columns survive.
     */
    @Query(
        "UPDATE profiles SET alias = :alias, ssh_host = :host, ssh_port = :port, " +
            "ssh_user = :user, ssh_remote_port = :remotePort, " +
            "ssh_host_key_sha256 = CASE WHEN ssh_host = :host AND ssh_port = :port " +
            "THEN ssh_host_key_sha256 ELSE NULL END " +
            "WHERE id = :id AND kind = 'SSH' AND base_url = '' AND base_url = :expectedUrl " +
            "AND alias = :expectedAlias AND auth_type = :expectedAuthType " +
            "AND project_path IS :expectedProjectPath AND ssh_host = :expectedHost " +
            "AND ssh_port = :expectedPort AND ssh_user = :expectedUser AND ssh_remote_port = :expectedRemotePort",
    )
    suspend fun saveSshConnectionIfCurrent(
        id: Long, expectedUrl: String, expectedAlias: String, expectedAuthType: AuthType,
        expectedProjectPath: String?, expectedHost: String, expectedPort: Int, expectedUser: String,
        expectedRemotePort: Int, alias: String, host: String, port: Int, user: String, remotePort: Int,
    ): Int

    /**
     * Pin an SSH kernel's host key on FIRST trust. Targeted for the same reason
     * as [setCookieHost], and conditional: it lands only while the row is still
     * that SSH endpoint and holds no pin. A host or port edited meanwhile is
     * another machine, and an existing pin is never replaced silently — a
     * changed host key is a hard stop. Returns the rows pinned (0 or 1).
     */
    @Query(
        "UPDATE profiles SET ssh_host_key_sha256 = :sha256 WHERE id = :id AND kind = 'SSH' " +
            "AND ssh_host = :host AND ssh_port = :port AND ssh_host_key_sha256 IS NULL",
    )
    suspend fun pinSshHostKey(id: Long, host: String, port: Int, sha256: String): Int

    @Query("DELETE FROM profiles WHERE id = :id")
    suspend fun deleteById(id: Long)
}
