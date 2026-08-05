package com.tofu.client.session

import android.util.Log
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao

/**
 * Orchestrates every profile mutation the UI triggers, over the same seams
 * [SessionManager] uses (so it is unit-testable with fakes). Keeps the Compose
 * layer thin: the UI validates via [ProfileForm], then calls one of these.
 *
 * Ordering invariants that matter for correctness:
 *  - ADD: store the secret BEFORE login (login reads it via the vault).
 *  - EDIT with a URL host change: delegate to [SessionManager.updateUrlAndReauth]
 *    so the dead host's Domain-pinned jar is purged before re-login.
 *  - DELETE: remove the secret AND the row (never orphan a credential).
 *  - RENAME: the secret is alias-keyed, so a rename must MOVE the secret to the
 *    new alias key or login would silently lose the credential.
 */
class SessionController(
    private val dao: ProfileDao,
    private val secrets: SecretVault,
    private val session: SessionManager,
    private val clock: () -> Long = { System.currentTimeMillis() },
) {

    /**
     * Find a password already stored for ANOTHER profile on the SAME host as
     * [baseUrl]. code-server auth is per-HOST (its session cookie is
     * `Domain`-pinned to the host), so different `/proxy/PORT/` URLs on one host
     * share one password. Returns the reusable secret, or null if no same-host
     * profile has one. [excludeAlias] skips the profile being edited itself.
     *
     * Pure over the seams (DAO snapshot + vault) → unit-testable.
     */
    suspend fun findSharedSecret(baseUrl: String, excludeAlias: String? = null): String? {
        val host = ServerUrl.parse(baseUrl)?.host ?: return null
        for (p in dao.getAllOnce()) {
            if (p.alias == excludeAlias) continue
            if (ServerUrl.parse(p.baseUrl)?.host != host) continue
            val s = secrets.secretFor(p.alias)
            if (!s.isNullOrEmpty()) return s
        }
        return null
    }

    /** Add a new server, store its secret, then attempt the first login. */
    suspend fun addProfile(
        alias: String,
        baseUrl: String,
        authType: AuthType,
        secret: String,
        projectPath: String? = null,
    ): AddResult {
        val a = alias.trim()
        if (dao.getByAlias(a) != null) return AddResult.DuplicateAlias
        val profile = ProfileForm.toProfile(0, a, baseUrl, authType, clock(), projectPath)
        // Store the secret FIRST — login reads it via the vault. When the field
        // is left blank, REUSE a password already stored for the same host
        // (shared per-host code-server auth), so the user needn't re-type it.
        if (authType == AuthType.CODE_SERVER_PASSWORD) {
            val effective = secret.ifEmpty { findSharedSecret(baseUrl, excludeAlias = a) ?: "" }
            if (effective.isNotEmpty()) secrets.putSecret(a, effective)
        }
        val id = dao.insert(profile)
        val saved = profile.copy(id = id)
        val result = session.login(saved)
        Log.i(TAG, "addProfile alias=$a login=${result::class.simpleName}")
        return AddResult.Added(saved, result)
    }

    /**
     * Edit an existing profile. Handles four cases in one entry point:
     *  - rename (alias change) → move the secret key,
     *  - new secret provided → overwrite it,
     *  - URL host change → purge + re-login (via updateUrlAndReauth),
     *  - plain field change → persist + re-login.
     *
     * Returns BOTH the login outcome and the row as actually PERSISTED. The
     * caller must not reconstruct the edited profile itself: the two branches
     * below write different rows (the host-change path nulls `cookieHost` and
     * may refresh `instanceUuid`), so a hand-rolled `copy()` at the call site
     * is silently wrong exactly when it matters — and for SSO that stale object
     * is what gets handed to the WebView for the whole session.
     */
    suspend fun editProfile(
        current: Profile,
        newAlias: String,
        newUrl: String,
        newAuthType: AuthType,
        newSecret: String,
        newProjectPath: String? = null,
    ): ProfileResult {
        val a = newAlias.trim()
        val pp = newProjectPath?.trim()?.ifEmpty { null }

        // Rename: move the alias-keyed secret so the credential isn't orphaned.
        if (a != current.alias) {
            val existing = secrets.secretFor(current.alias)
            if (existing != null) {
                secrets.putSecret(a, existing)
                secrets.removeSecret(current.alias)
            }
        }
        // New secret overwrites; blank keeps the existing one. If blank AND this
        // profile has no secret of its own yet, reuse a same-host password
        // (shared per-host code-server auth) so it needn't be re-typed.
        if (newSecret.isNotEmpty()) {
            secrets.putSecret(a, newSecret)
        } else if (newAuthType == AuthType.CODE_SERVER_PASSWORD &&
            secrets.secretFor(a).isNullOrEmpty()
        ) {
            findSharedSecret(newUrl, excludeAlias = a)?.let { secrets.putSecret(a, it) }
        }

        val oldHost = ServerUrl.parse(current.baseUrl)?.host
        val newHost = ServerUrl.parse(newUrl)?.host
        val base = current.copy(alias = a, authType = newAuthType, projectPath = pp)

        return if (oldHost != null && newHost != null && oldHost != newHost) {
            // URL host changed → the purge-and-relogin path owns persistence, so
            // it also owns what the persisted row looks like.
            val reauth = session.updateUrlAndReauth(base, newUrl)
            ProfileResult(reauth.login, reauth.persisted)
        } else {
            val updated = ProfileForm.toProfile(
                current.id, a, newUrl, newAuthType, current.lastUsedAt, pp,
            )
            dao.update(updated)
            ProfileResult(session.login(updated), updated)
        }
    }

    /**
     * Activate a profile (make it current): bump recency, then log in.
     *
     * [profile] comes from the list Flow, i.e. a row as RENDERED — which lags
     * any write not yet re-emitted and recomposed. Two consequences, both fixed
     * here rather than papered over at the call site:
     *
     *  - the recency bump is a TARGETED write, so it cannot reinstate stale
     *    columns (a full-row update would happily restore a `cookieHost` that a
     *    login had just cleared, or wipe one it had just stamped);
     *  - the row is RE-READ before login, so the handshake and every decision
     *    downstream of it (authType, baseUrl, cookieHost) see the database's
     *    truth rather than what the screen happened to be showing.
     *
     * Returns the row actually used, so the caller navigates with that and not
     * with its own stale copy.
     */
    suspend fun activate(profile: Profile): ProfileResult {
        dao.touchLastUsed(profile.id, clock())
        val current = dao.getById(profile.id) ?: profile
        return ProfileResult(session.login(current), current)
    }

    /**
     * One-time upgrade migration (idempotent, safe to run every launch): fix
     * any persisted profile whose URL is a code-server proxy form but whose
     * stored authType is the stale NONE default (see
     * [ServerUrl.needsProxyAuthFix]) — flip it to CODE_SERVER_PASSWORD so it
     * can headless-login instead of being stranded on the code-server login
     * page with Start greyed forever. Returns the count of rows fixed.
     *
     * Only touches proxy+NONE rows; bare-host NONE and any non-NONE auth are
     * left as-is. Runs over the DAO seam → unit-testable with fakes.
     *
     * The write is TARGETED. This loop iterates a `getAllOnce()` snapshot and
     * suspends on every write, while running on `viewModelScope` concurrently
     * with the UI built moments later — so a card tap CAN land between the read
     * and the write. A full-row update would then reinstate that row's pre-tap
     * `cookieHost` / `lastUsedAt`. Narrow write, no interleaving hazard.
     */
    suspend fun migrateProxyAuthDefaults(): Int {
        var fixed = 0
        for (p in dao.getAllOnce()) {
            if (ServerUrl.needsProxyAuthFix(p.baseUrl, p.authType)) {
                dao.setAuthType(p.id, AuthType.CODE_SERVER_PASSWORD)
                fixed++
                Log.i(TAG, "migrateProxyAuthDefaults: fixed alias=%s NONE→CODE_SERVER_PASSWORD"
                    .format(p.alias))
            }
        }
        return fixed
    }

    /** Delete a profile and its stored secret (never orphan a credential). */
    suspend fun deleteProfile(profile: Profile) {
        secrets.removeSecret(profile.alias)
        dao.deleteById(profile.id)
        Log.i(TAG, "deleteProfile alias=${profile.alias}")
    }

    sealed interface AddResult {
        data class Added(val profile: Profile, val login: LoginResult) : AddResult
        data object DuplicateAlias : AddResult
    }

    /**
     * The login outcome plus the row as it was actually written to / read from
     * Room. The UI navigates with [persisted], never with a locally
     * reconstructed or list-rendered copy.
     */
    data class ProfileResult(val login: LoginResult, val persisted: Profile)

    private companion object {
        const val TAG = "SessionController"
    }
}
