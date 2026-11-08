package com.tofu.client.session

import android.util.Log
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint

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
 *  - KIND: only LOCAL and SSH kernels go through [kernels]; a REMOTE_URL
 *    profile follows exactly the pre-kernel path.
 */
class SessionController(
    private val dao: ProfileDao,
    private val secrets: SecretVault,
    private val session: SessionManager,
    private val kernels: KernelControllers,
    private val clock: () -> Long = { System.currentTimeMillis() },
    /** Native logging is injectable so orchestration contracts remain JVM-only. */
    private val report: (String) -> Unit = { Log.i(TAG, it) },
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
        val server = ServerUrl.parse(baseUrl) ?: return null
        for (p in dao.getAllOnce()) {
            if (p.alias == excludeAlias) continue
            val other = ServerUrl.parse(p.baseUrl) ?: continue
            if (other.origin != server.origin || other.codeServerRootPath != server.codeServerRootPath) continue
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
        report("addProfile alias=$a login=${result::class.simpleName}")
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

        val oldHost = ServerUrl.parse(current.baseUrl)?.origin
        val newHost = ServerUrl.parse(newUrl)?.origin
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
     * Save a new SSH kernel: the endpoint in Room (no pinned host key yet) and
     * a non-blank [password] for the SSH account in the vault under the alias,
     * like a code-server password. Connecting is a separate [activate].
     */
    suspend fun addSshKernel(alias: String, endpoint: SshEndpoint, password: String = ""): AddResult {
        val a = alias.trim()
        if (dao.getByAlias(a) != null) return AddResult.DuplicateAlias
        if (password.isNotEmpty()) secrets.putSecret(a, password)
        val profile = Profile(alias = a, baseUrl = "", kind = KernelKind.SSH, ssh = endpoint, lastUsedAt = clock())
        val saved = profile.copy(id = dao.insert(profile))
        report("addSshKernel alias=$a")
        return AddResult.Saved(saved)
    }

    /**
     * Edit a saved SSH kernel and return the row as persisted. A rename moves
     * the alias-keyed secret, as for URL profiles; a non-blank [newPassword]
     * replaces it, a blank one keeps it. A changed host or port is a
     * different machine to trust, so the pinned host key is dropped and the
     * next connect pins again; user and remote port keep it.
     */
    suspend fun editSshKernel(current: Profile, newAlias: String, endpoint: SshEndpoint, newPassword: String = ""): Profile {
        val a = newAlias.trim()
        if (a != current.alias) {
            secrets.secretFor(current.alias)?.let {
                secrets.putSecret(a, it)
                secrets.removeSecret(current.alias)
            }
        }
        if (newPassword.isNotEmpty()) secrets.putSecret(a, newPassword)
        val stored = dao.getById(current.id) ?: current
        val old = stored.ssh
        val samePlace = old != null && old.host == endpoint.host && old.port == endpoint.port
        val updated = stored.copy(
            alias = a,
            ssh = endpoint.copy(hostKeySha256 = if (samePlace) old?.hostKeySha256 else null),
        )
        dao.update(updated)
        report("editSshKernel alias=$a pinKept=${samePlace && old?.hostKeySha256 != null}")
        return updated
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
     *
     * A LOCAL or SSH kernel is first brought up by its [KernelController]; the
     * session then signs in against the returned endpoint, and the result
     * carries the row bound to it (see [ProfileResult]). A kernel that cannot
     * start comes back as [LoginResult.Error] with the controller's reason.
     * A kernel whose WebView opens is put in front ([KernelControllers.putInFront]).
     */
    suspend fun activate(profile: Profile): ProfileResult {
        dao.touchLastUsed(profile.id, clock())
        val current = dao.getById(profile.id) ?: profile
        val result = when (current.kind) {
            KernelKind.REMOTE_URL -> ProfileResult(session.login(current), current)
            KernelKind.LOCAL, KernelKind.SSH -> startAndSignIn(current)
        }
        // The WebView switches to it: this kernel is now the one in front.
        if (InteractiveSso.shouldOpenWebView(result.login)) kernels.putInFront(result.persisted)
        return result
    }

    private suspend fun startAndSignIn(kernel: Profile): ProfileResult {
        val endpoint = try {
            kernels.of(kernel.kind).ensureRunning(kernel)
        } catch (unavailable: KernelUnavailableException) {
            report("activate alias=${kernel.alias} kind=${kernel.kind} unavailable=${unavailable.reason}")
            return ProfileResult(LoginResult.Error(unavailable.reason), kernel)
        }
        val live = kernel.boundTo(endpoint)
        return ProfileResult(session.login(live), live)
    }

    /**
     * Delete a profile and its stored secret (never orphan a credential). A
     * LOCAL or SSH kernel is stopped first so no process or tunnel outlives its
     * row; a REMOTE_URL server is someone else's and keeps running.
     */
    suspend fun deleteProfile(profile: Profile) {
        when (profile.kind) {
            KernelKind.REMOTE_URL -> Unit
            KernelKind.LOCAL, KernelKind.SSH -> kernels.of(profile.kind).stop(profile)
        }
        secrets.removeSecret(profile.alias)
        dao.deleteById(profile.id)
        report("deleteProfile alias=${profile.alias}")
    }

    sealed interface AddResult {
        data class Added(val profile: Profile, val login: LoginResult) : AddResult
        /** Stored without connecting (an SSH kernel connects on [activate]). */
        data class Saved(val profile: Profile) : AddResult
        data object DuplicateAlias : AddResult
    }

    /**
     * The login outcome plus the row as it was actually written to / read from
     * Room. The UI navigates with [persisted], never with a locally
     * reconstructed or list-rendered copy. For a LOCAL or SSH kernel that
     * started, it is that row bound to the live endpoint ([boundTo]); Room
     * keeps the endpoint-free row.
     */
    data class ProfileResult(val login: LoginResult, val persisted: Profile)

    private companion object {
        const val TAG = "SessionController"
    }
}
