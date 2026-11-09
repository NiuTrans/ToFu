package com.tofu.client.session

import android.util.Log
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import com.tofu.client.data.sameConnectionConfiguration
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.coroutines.withTimeout

/**
 * Profile mutation owner over Room, the vault and the kind's kernel controller.
 * Reviewed fields commit before credentials; one five-second noncancellable
 * readback reconciles a known/lost commit without rolling back a row or replacing
 * a newer vault value. Network login runs outside the graph's mutation lock.
 * LOCAL/SSH addresses are bound runtime copies, never saved connection fields.
 */
class SessionController(
    private val dao: ProfileDao,
    private val secrets: SecretVault,
    private val session: SessionManager,
    private val kernels: KernelControllers? = null,
    private val clock: () -> Long = { System.currentTimeMillis() },
    private val report: (String) -> Unit = { Log.i(TAG, it) },
) {
    /** Borrow only a URL profile's secret, from the same origin and gateway prefix. */
    suspend fun findSharedSecret(baseUrl: String, excludeAlias: String? = null): String? {
        currentCoroutineContext().ensureActive()
        if (ServerUrl.storageError(baseUrl) != null || LocalKernelCookie.isReservedUrl(baseUrl)) return null
        val server = ServerUrl.parse(baseUrl) ?: return null
        val profiles = dao.getAllOnce()
        currentCoroutineContext().ensureActive()
        for (profile in profiles) {
            if (profile.kind != KernelKind.REMOTE_URL || profile.alias == excludeAlias ||
                ServerUrl.storageError(profile.baseUrl) != null) continue
            val other = ServerUrl.parse(profile.baseUrl) ?: continue
            if (other.origin != server.origin || other.codeServerRootPath != server.codeServerRootPath) continue
            val current = dao.getById(profile.id) ?: continue
            currentCoroutineContext().ensureActive()
            if (!current.sameConnectionConfiguration(profile)) continue
            val secret = secrets.secretFor(current.alias)
            if (!secret.isNullOrEmpty()) return secret
        }
        return null
    }

    suspend fun addProfile(
        alias: String,
        baseUrl: String,
        authType: AuthType,
        secret: String,
        projectPath: String? = null,
    ): AddResult {
        val persisted = session.profileMutationMutex.withLock {
            currentCoroutineContext().ensureActive()
            val profile = ProfileForm.toProfile(0, alias, baseUrl, authType, clock(), projectPath)
            if (dao.getByAlias(profile.alias) != null) return AddResult.DuplicateAlias
            currentCoroutineContext().ensureActive()
            val beforeSecret = secrets.secretFor(profile.alias)
            val effective = if (authType == AuthType.CODE_SERVER_PASSWORD)
                secret.ifEmpty { findSharedSecret(profile.baseUrl, excludeAlias = profile.alias).orEmpty() }.ifEmpty { null }
                else null
            currentCoroutineContext().ensureActive()
            insertAndReconcile(profile, beforeSecret, effective)
        }
        val result = session.login(persisted)
        report("addProfile login=${result::class.simpleName}")
        return AddResult.Added(persisted, result)
    }

    /** Save reviewed URL fields, then reconcile rename/password state before login. */
    suspend fun editProfile(
        current: Profile,
        newAlias: String,
        newUrl: String,
        newAuthType: AuthType,
        newSecret: String,
        newProjectPath: String? = null,
    ): ProfileResult {
        val saved = session.profileMutationMutex.withLock {
            currentCoroutineContext().ensureActive()
            if (current.kind != KernelKind.REMOTE_URL) return changed(current)
            // Admission precedes every vault operation, including a rename.
            val intent = ProfileForm.toProfile(current.id, newAlias, newUrl, newAuthType, current.lastUsedAt, newProjectPath)
            val stored = dao.getById(current.id) ?: return changed(current)
            currentCoroutineContext().ensureActive()
            if (!stored.matchesEditingSnapshot(current)) return changed(stored)
            if (aliasOwnedByAnotherRow(intent.alias, stored.id)) return changed(stored)
            val oldSecret = secrets.secretFor(stored.alias)
            val targetSecret = secrets.secretFor(intent.alias)
            val inherited = if (intent.alias != stored.alias) oldSecret ?: targetSecret else targetSecret
            val effective = newSecret.ifEmpty { inherited.orEmpty() }.ifEmpty {
                if (newAuthType == AuthType.CODE_SERVER_PASSWORD)
                    findSharedSecret(intent.baseUrl, excludeAlias = stored.alias).orEmpty() else ""
            }.ifEmpty { null }
            currentCoroutineContext().ensureActive()
            val base = stored.copy(alias = intent.alias, authType = newAuthType, projectPath = intent.projectPath)
            session.persistUrlChangeAndReconcile(
                base, intent.baseUrl, expected = stored,
                onCommitted = { row -> reconcileEditedSecret(stored, row, oldSecret, targetSecret, effective) },
                onIncomplete = report,
            ) ?: return changed(stored)
        }
        return ProfileResult(session.login(saved), saved)
    }

    /** Unsafe URL rows refuse without mutation; internal kinds use their live controller endpoint. */
    suspend fun activate(profile: Profile): ProfileResult {
        currentCoroutineContext().ensureActive()
        val current = freshSource(profile) ?: return changed(profile)
        currentCoroutineContext().ensureActive()
        if (current.kind == KernelKind.REMOTE_URL) {
            urlAdmissionError(current.baseUrl)?.let { return unsafe(it, current) }
        } else if (current.baseUrl.isNotEmpty()) return changed(current)
        if (current.id != 0L && dao.touchLastUsed(current.id, clock()) == 0) return changed(current)
        currentCoroutineContext().ensureActive()
        val touched = freshSource(current) ?: return changed(current)
        currentCoroutineContext().ensureActive()
        val result = when (touched.kind) {
            KernelKind.REMOTE_URL -> {
                urlAdmissionError(touched.baseUrl)?.let { return unsafe(it, touched) }
                ProfileResult(session.login(touched), touched)
            }
            KernelKind.LOCAL, KernelKind.SSH -> startAndSignIn(touched)
        }
        currentCoroutineContext().ensureActive()
        if (InteractiveSso.shouldOpenWebView(result.login)) {
            val latest = freshSource(touched) ?: return changed(touched)
            currentCoroutineContext().ensureActive()
            if (!latest.sameConnectionConfiguration(touched)) return changed(latest)
            kernels?.putInFront(result.persisted)
        }
        return result
    }

    private suspend fun startAndSignIn(source: Profile): ProfileResult {
        if (source.baseUrl.isNotEmpty()) return changed(source)
        val endpoint = try {
            val controller = kernels?.of(source.kind)
                ?: throw KernelUnavailableException(UnavailableKernelController.NOT_IN_THIS_BUILD)
            controller.ensureRunning(source)
        } catch (unavailable: KernelUnavailableException) {
            report("activate kind=${source.kind} unavailable=${unavailable.reason}")
            return ProfileResult(LoginResult.Error(unavailable.reason), source)
        }
        currentCoroutineContext().ensureActive()
        val latest = freshSource(source) ?: return changed(source)
        currentCoroutineContext().ensureActive()
        if (!latest.sameConnectionConfiguration(source)) return changed(latest)
        val live = latest.boundTo(endpoint)
        return ProfileResult(session.login(live), live)
    }

    /** The one explicitly synthetic row is This phone; a deleted saved row never falls back. */
    private suspend fun freshSource(profile: Profile): Profile? =
        if (profile.id == 0L && profile.copy(baseUrl = "", cookieHost = null) == KernelList.THIS_PHONE)
            dao.getAllOnce().firstOrNull { it.kind == KernelKind.LOCAL } ?: KernelList.THIS_PHONE
        else dao.getById(profile.id)

    /** Stop an owned kernel first; a failed/cancelled Room delete keeps its credential. */
    suspend fun deleteProfile(profile: Profile) = session.profileMutationMutex.withLock {
        currentCoroutineContext().ensureActive()
        val stored = dao.getById(profile.id) ?: return@withLock
        currentCoroutineContext().ensureActive()
        val beforeSecret = secrets.secretFor(stored.alias)
        if (stored.kind != KernelKind.REMOTE_URL) {
            val controller = kernels?.of(stored.kind)
                ?: throw KernelUnavailableException(UnavailableKernelController.NOT_IN_THIS_BUILD)
            controller.stop(stored)
            currentCoroutineContext().ensureActive()
            val afterStop = dao.getById(stored.id)
            currentCoroutineContext().ensureActive()
            check(afterStop?.sameConnectionConfiguration(stored) == true) { CHANGED_MESSAGE }
        }
        try { dao.deleteById(stored.id) }
        finally {
            reconcileSecrets {
                if (dao.getById(stored.id) == null && dao.getByAlias(stored.alias) == null &&
                    !secrets.compareAndSetSecret(stored.alias, beforeSecret, null))
                    error("Credential changed concurrently")
            }
        }
        currentCoroutineContext().ensureActive()
        report("deleteProfile completed")
    }

    /** Store SSH config (never a form-provided pin) before its optional account password. */
    suspend fun addSshKernel(alias: String, endpoint: SshEndpoint, password: String = ""): AddResult =
        session.profileMutationMutex.withLock {
            currentCoroutineContext().ensureActive()
            val reviewed = reviewedSshEndpoint(alias, endpoint)
            val a = alias.trim()
            if (dao.getByAlias(a) != null) return AddResult.DuplicateAlias
            currentCoroutineContext().ensureActive()
            val beforeSecret = secrets.secretFor(a)
            val profile = Profile(alias = a, baseUrl = "", kind = KernelKind.SSH, ssh = reviewed, lastUsedAt = clock())
            val saved = insertAndReconcile(profile, beforeSecret, password.ifEmpty { null })
            report("addSshKernel saved")
            AddResult.Saved(saved)
        }

    /**
     * Conditional SSH-field save; a concurrently acquired same-machine trust
     * pin is kept by SQL. A reviewed host/port change clears it. Bound snapshots
     * can identify their source row, but their loopback URL is never written.
     */
    suspend fun editSshKernel(current: Profile, newAlias: String, endpoint: SshEndpoint, newPassword: String = ""): Profile =
        session.profileMutationMutex.withLock {
            currentCoroutineContext().ensureActive()
            check(current.kind == KernelKind.SSH) { CHANGED_MESSAGE }
            val reviewed = reviewedSshEndpoint(newAlias, endpoint)
            val stored = dao.getById(current.id) ?: error(CHANGED_MESSAGE)
            currentCoroutineContext().ensureActive()
            check(stored.baseUrl.isEmpty() && stored.sameConnectionConfiguration(current.copy(baseUrl = ""))) { CHANGED_MESSAGE }
            val a = newAlias.trim()
            check(!aliasOwnedByAnotherRow(a, stored.id)) { CHANGED_MESSAGE }
            val old = checkNotNull(stored.ssh) { CHANGED_MESSAGE }
            val oldSecret = secrets.secretFor(stored.alias)
            val targetSecret = secrets.secretFor(a)
            val inherited = if (a != stored.alias) oldSecret ?: targetSecret else targetSecret
            val effective = newPassword.ifEmpty { inherited.orEmpty() }.ifEmpty { null }
            val samePlace = old.host == reviewed.host && old.port == reviewed.port
            val intent = stored.copy(alias = a, ssh = reviewed.copy(hostKeySha256 = old.hostKeySha256.takeIf { samePlace }))
            var saved: Profile? = null
            var returned = false
            var committed = false
            try {
                currentCoroutineContext().ensureActive()
                committed = dao.saveSshConnectionIfCurrent(
                    stored.id, stored.baseUrl, stored.alias, stored.authType, stored.projectPath,
                    old.host, old.port, old.user, old.remotePort,
                    a, reviewed.host, reviewed.port, reviewed.user, reviewed.remotePort,
                ) > 0
                returned = true
            } finally {
                reconcileSecrets {
                    val row = dao.getById(stored.id)
                    val confirmed = if (returned) committed else !stored.sameSaveIntent(intent)
                    if (confirmed && row != null && row.sameSaveIntent(intent)) {
                        saved = row
                        reconcileEditedSecret(stored, row, oldSecret, targetSecret, effective)
                    }
                }
            }
            currentCoroutineContext().ensureActive()
            check(committed) { CHANGED_MESSAGE }
            val persisted = checkNotNull(saved) { "SSH connection saved; reconciliation incomplete. Return to the list and retry the edit." }
            report("editSshKernel saved pinKept=${samePlace && persisted.ssh?.hostKeySha256 != null}")
            persisted
        }

    private fun reviewedSshEndpoint(alias: String, endpoint: SshEndpoint): SshEndpoint {
        val validation = ProfileForm.validateSsh(
            alias, endpoint.host, endpoint.port.toString(), endpoint.user, endpoint.remotePort.toString(), emptySet(),
        )
        require(validation.ok) { "Review the SSH name, host, user and ports before saving." }
        return endpoint.copy(host = endpoint.host.trim(), user = endpoint.user.trim(), hostKeySha256 = null)
    }

    private suspend fun aliasOwnedByAnotherRow(alias: String, id: Long): Boolean {
        val owner = dao.getByAlias(alias)
        currentCoroutineContext().ensureActive()
        return owner != null && owner.id != id
    }

    private suspend fun insertAndReconcile(profile: Profile, beforeSecret: String?, effective: String?): Profile {
        var saved: Profile? = null
        try { saved = profile.copy(id = dao.insert(profile)) }
        finally {
            reconcileSecrets {
                val known = saved
                val row = if (known == null) dao.getByAlias(profile.alias) else dao.getById(known.id)
                if (row != null && row.sameSaveIntent(profile) && effective != null &&
                    !secrets.compareAndSetSecret(profile.alias, beforeSecret, effective))
                    error("Credential changed concurrently")
            }
        }
        currentCoroutineContext().ensureActive()
        return checkNotNull(saved)
    }

    private suspend fun reconcileEditedSecret(
        before: Profile, committed: Profile, oldSecret: String?, targetSecret: String?, effective: String?,
    ) {
        val current = dao.getById(committed.id) ?: return
        if (!current.sameSaveIntent(committed)) return
        if (effective != null && !secrets.compareAndSetSecret(committed.alias, targetSecret, effective))
            error("Credential changed concurrently")
        if (committed.alias != before.alias && dao.getByAlias(before.alias) == null &&
            !secrets.compareAndSetSecret(before.alias, oldSecret, null))
            error("Credential changed concurrently")
    }

    /** Compare save authority only, never concurrent recency/session/trust metadata. */
    private fun Profile.sameSaveIntent(other: Profile): Boolean = sameConnectionConfiguration(other.copy(id = id))

    /** A legacy URL editor carries a sanitized proposal, not a silently rewritten row. */
    private fun Profile.matchesEditingSnapshot(snapshot: Profile): Boolean =
        kind == KernelKind.REMOTE_URL && snapshot.kind == KernelKind.REMOTE_URL &&
            sameConnectionConfiguration(snapshot.copy(baseUrl = baseUrl)) &&
            (baseUrl == snapshot.baseUrl || ServerUrl.sanitizeForStorage(baseUrl) == snapshot.baseUrl)

    private fun urlAdmissionError(url: String): String? = ServerUrl.storageError(url)
        ?: LocalKernelCookie.RESERVED_HOST_MESSAGE.takeIf { LocalKernelCookie.isReservedUrl(url) }

    private fun changed(profile: Profile) = ProfileResult(LoginResult.Error(CHANGED_MESSAGE), profile)

    private fun unsafe(error: String, profile: Profile) = ProfileResult(
        LoginResult.Error("$error Edit this saved server URL before connecting."), profile,
    )

    private suspend fun reconcileSecrets(action: suspend () -> Unit) = withContext(NonCancellable) {
        try { withTimeout(SessionManager.SAVE_RECONCILIATION_MS) { action() } }
        catch (error: Exception) {
            report("credential save reconciliation incomplete (${error.javaClass.simpleName}); retry the server edit")
        }
    }

    sealed interface AddResult {
        data class Added(val profile: Profile, val login: LoginResult) : AddResult
        /** Stored without connecting; an SSH kernel connects on [activate]. */
        data class Saved(val profile: Profile) : AddResult
        data object DuplicateAlias : AddResult
    }

    /** The URL Room row, or a LOCAL/SSH source row bound to its live endpoint (never persisted). */
    data class ProfileResult(val login: LoginResult, val persisted: Profile)

    private companion object {
        const val TAG = "SessionController"
        const val CHANGED_MESSAGE = "This server changed or was deleted. Return to the list and review it again."
    }
}
