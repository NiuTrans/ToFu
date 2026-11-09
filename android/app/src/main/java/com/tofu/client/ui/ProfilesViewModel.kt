package com.tofu.client.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.DiagLog
import com.tofu.client.session.InteractiveSso
import com.tofu.client.session.KernelControllers
import com.tofu.client.session.KernelList
import com.tofu.client.session.KernelState
import com.tofu.client.session.LoginResult
import com.tofu.client.session.SecretVault
import com.tofu.client.session.ServerUrl
import com.tofu.client.session.SessionController
import com.tofu.client.session.SessionManager
import com.tofu.client.session.UpdateChecker
import com.tofu.client.session.UpdateStatus
import com.tofu.client.ssh.SshSetup
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

sealed interface Screen {
    data object List : Screen
    /** A deliberate form selection resets fields; a warm link only offers metadata. */
    data class AddEdit(val formId: Long) : Screen
    /** SSH fields/password are remounted for each deliberate selection, too. */
    data class AddEditSsh(val formId: Long) : Screen
    data class Web(val profile: Profile) : Screen
}

sealed interface UiStatus {
    data object Idle : UiStatus
    data class LoggingIn(val alias: String) : UiStatus
    data class Error(val message: String) : UiStatus
    data class BadCredentials(val profile: Profile) : UiStatus
}

/**
 * Activity/ViewModelStore-owned Kernels list, navigation and operation lifetime.
 * Authentication and repository mutation stay in SessionController/SessionManager;
 * this owner cancels obsolete screen intent and fences late UI publication.
 */
class ProfilesViewModel(
    private val dao: ProfileDao,
    private val secrets: SecretVault,
    private val controller: SessionController,
    private val kernels: KernelControllers,
    private val updateChecker: UpdateChecker? = null,
    private val diagnostics: DiagLog? = null,
    /** The retained graph is also used by each recreated Activity's WebView. */
    val session: SessionManager,
    private val clock: () -> Long = { System.nanoTime() / 1_000_000L },
    private val release: () -> Unit = {},
) : ViewModel() {
    private val ownedJob = checkNotNull(viewModelScope.coroutineContext[Job])
    init {
        // Completion waits for all children, including bounded save reconciliation.
        // No Activity or unowned cleanup coroutine is retained.
        ownedJob.invokeOnCompletion {
            try { release() }
            catch (error: Exception) { diagnostics?.record("E", "ProfileStorage", "graph release failed: ${error.javaClass.simpleName}") }
        }
    }

    private val _status = MutableStateFlow<UiStatus>(UiStatus.Idle)
    val status: StateFlow<UiStatus> = _status
    val profiles: StateFlow<List<Profile>> = dao.observeAll().catch { error ->
        if (error is CancellationException) throw error
        diagnostics?.record("E", "ProfileStorage", "read failed: ${error.javaClass.simpleName}")
        _status.value = UiStatus.Error("Saved servers could not be read. Retry or copy diagnostic logs.")
        emit(emptyList())
    }.stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    /** The real stored rows still own onboarding; only the list pins This phone. */
    val kernelList: StateFlow<List<Profile>> = profiles.map(KernelList::pinned)
        .stateIn(viewModelScope, SharingStarted.Eagerly, KernelList.pinned(emptyList()))
    private val _activeKernel = MutableStateFlow<Profile?>(null)
    val activeKernel: StateFlow<Profile?> = _activeKernel
    private val _screen = MutableStateFlow<Screen>(Screen.List)
    val screen: StateFlow<Screen> = _screen
    private val _update = MutableStateFlow<UpdateStatus?>(null)
    val update: StateFlow<UpdateStatus?> = _update
    private var loginJob: Job? = null
    private var loginGeneration = 0L
    private var formGeneration = 0L
    private var prefillRevision = 0L
    private var sshKeyJob: Job? = null
    private var sshKeyGeneration = 0L
    private var updateJob: Job? = null
    private var updateGeneration = 0L
    private var nextUpdateAt = Long.MIN_VALUE
    private var updateSucceeded = false
    private var checkedUrls = emptySet<String>()

    var editing: Profile? = null
        private set
    data class Prefill(val url: String?, val alias: String?, val revision: Long)
    /** Accepted initializer and pending external intent are separate authorities. */
    private val _addPrefill = MutableStateFlow<Prefill?>(null)
    val addPrefill: StateFlow<Prefill?> = _addPrefill
    private val _pendingPrefill = MutableStateFlow<Prefill?>(null)
    val pendingPrefill: StateFlow<Prefill?> = _pendingPrefill
    /** Nonsecret memory-only fields survive recreation. No password field exists. */
    data class FormDraft(
        val alias: String, val url: String, val auth: AuthType, val authTouched: Boolean,
        val projectPath: String, val manageEnabled: Boolean, val dirty: Boolean,
    )
    var formDraft: FormDraft? = null
        private set

    private val ssh: SshSetup? get() = kernels.of(KernelKind.SSH) as? SshSetup
    private val _sshPublicKey = MutableStateFlow<String?>(null)
    val sshPublicKey: StateFlow<String?> = _sshPublicKey
    /** Public fingerprint offered by the saved SSH endpoint, never imported from a link. */
    var hostKeyToTrust: String? = null
        private set

    private fun currentFormId(): Long? = when (val current = _screen.value) {
        is Screen.AddEdit -> current.formId
        is Screen.AddEditSsh -> current.formId
        else -> null
    }

    fun retainFormDraft(draft: FormDraft, formId: Long? = null) {
        val current = _screen.value as? Screen.AddEdit ?: return
        if (formId != null && current.formId != formId) return
        formDraft = draft.copy(url = ServerUrl.sanitizeForDraft(draft.url))
    }

    fun dismissPrefill(formId: Long? = null, revision: Long? = null) {
        if (formId != null && currentFormId() != formId) return
        if (revision != null && _pendingPrefill.value?.revision != revision) return
        _pendingPrefill.value = null
    }

    fun rejectConnectLink() {
        formChanged()
        _status.value = UiStatus.Error(com.tofu.client.session.DeepLink.INVALID_LINK_MESSAGE)
    }

    /** A scan/paste result belongs to the exact URL form that requested it. */
    fun offerConnect(connect: com.tofu.client.session.DeepLink.Connect, formId: Long) {
        if ((_screen.value as? Screen.AddEdit)?.formId != formId) return
        startAdd(connect.url, connect.alias)
    }

    fun startAdd(prefillUrl: String? = null, prefillAlias: String? = null) {
        formChanged()
        if (prefillUrl != null) ServerUrl.storageError(prefillUrl)?.let {
            _status.value = UiStatus.Error(it); return
        }
        val label = prefillAlias ?: prefillUrl?.let(ServerUrl::displayLabel)
        val incoming = Prefill(prefillUrl, label, ++prefillRevision)
        val current = _screen.value
        // Manual Add is a deliberate selection. External URL metadata must not
        // replace an existing form or a live kernel; consent opens a URL review.
        if (prefillUrl != null && (current is Screen.AddEdit || current is Screen.AddEditSsh || current is Screen.Web)) {
            _pendingPrefill.value = incoming
        } else {
            openRemoteForm(incoming)
        }
    }

    /** Explicit consent fenced by both form identity and the exact offered link revision. */
    fun acceptPrefill(formId: Long? = null, revision: Long? = null) {
        if (formId != null && currentFormId() != formId) return
        val accepted = _pendingPrefill.value ?: return
        if (revision != null && accepted.revision != revision) return
        formChanged()
        openRemoteForm(accepted)
    }

    private fun clearFormState() {
        editing = null
        formDraft = null
        _pendingPrefill.value = null
        _addPrefill.value = null
        hostKeyToTrust = null
        sshKeyGeneration++
        sshKeyJob?.cancel()
        sshKeyJob = null
    }

    private fun openRemoteForm(prefill: Prefill) {
        clearFormState()
        _addPrefill.value = prefill
        _screen.value = Screen.AddEdit(++formGeneration)
    }

    fun startAddSsh() {
        formChanged()
        clearFormState()
        _screen.value = Screen.AddEditSsh(++formGeneration)
        loadSshPublicKey()
    }

    /** This phone has no editable fields. Only URL rows are sanitized into a URL editor. */
    fun startEdit(profile: Profile) {
        if (profile.kind == KernelKind.LOCAL) return
        formChanged()
        clearFormState()
        if (profile.kind == KernelKind.SSH) {
            editing = profile
            _screen.value = Screen.AddEditSsh(++formGeneration)
            loadSshPublicKey()
            return
        }
        val safeUrl = ServerUrl.sanitizeForStorage(profile.baseUrl)
        editing = profile.copy(baseUrl = safeUrl.orEmpty())
        _screen.value = Screen.AddEdit(++formGeneration)
        if (safeUrl != profile.baseUrl) _status.value = UiStatus.Error(
            "Credentials were removed from this saved URL. Review it and save before connecting; use the password field or interactive sign-in.",
        )
    }

    fun kernelState(profile: Profile): StateFlow<KernelState> = kernels.state(profile)

    /** Back, selection, deletion and remote-form field edits cancel obsolete login intent. */
    fun formChanged(formId: Long? = null) {
        if (formId != null && currentFormId() != formId) return
        loginGeneration++
        loginJob?.cancel()
        loginJob = null
        _status.value = UiStatus.Idle
    }

    fun backToList(formId: Long) {
        if (currentFormId() == formId) backToList()
    }

    fun backToList() {
        formChanged()
        clearFormState()
        _screen.value = Screen.List
    }

    fun secretStoredFor(alias: String): Boolean = secrets.secretFor(alias) != null
    suspend fun reusableSecretHost(url: String, excludeAlias: String?): String? {
        if (controller.findSharedSecret(url, excludeAlias) == null) return null
        return ServerUrl.parse(url)?.host
    }

    fun activate(profile: Profile) = login(profile.alias) { generation ->
        currentCoroutineContext().ensureActive()
        // Reserve This phone's foreground lifetime at the tap, before readiness.
        // Other carriers keep their current front until activation is verified.
        if (profile.kind == KernelKind.LOCAL) kernels.of(KernelKind.LOCAL).frontChanged(profile)
        val result = controller.activate(profile)
        handleActivation(generation, result)
    }

    private suspend fun handleActivation(generation: Long, result: SessionController.ProfileResult) {
        currentCoroutineContext().ensureActive()
        if (generation != loginGeneration) return
        val untrusted = if (result.persisted.kind == KernelKind.SSH && !InteractiveSso.shouldOpenWebView(result.login))
            ssh?.hostKeyToTrust(result.persisted) else null
        if (untrusted != null) {
            val current = currentForResult(generation, result.persisted) ?: return
            clearFormState()
            editing = current
            hostKeyToTrust = untrusted
            _screen.value = Screen.AddEditSsh(++formGeneration)
            _status.value = UiStatus.Idle
            loadSshPublicKey()
        } else {
            handleLogin(generation, result.login, result.persisted)
        }
    }

    /** Pin exactly the reviewed saved host's key, then activate within the same owned job. */
    fun trustHostKey(formId: Long? = null) {
        val form = _screen.value as? Screen.AddEditSsh ?: return
        if (formId != null && form.formId != formId) return
        val kernel = editing?.takeIf { it.kind == KernelKind.SSH } ?: return
        val fingerprint = hostKeyToTrust ?: return
        hostKeyToTrust = null
        login(kernel.alias) { generation ->
            val current = currentForResult(generation, kernel) ?: return@login
            val trusted = ssh?.trustHostKey(current, fingerprint) == true
            currentCoroutineContext().ensureActive()
            if (generation != loginGeneration) return@login
            if (trusted) handleActivation(generation, controller.activate(current))
            else _status.value = UiStatus.Error(
                "The kernel changed before its host key was pinned. Connect again to check the key.",
            )
        }
    }

    private fun loadSshPublicKey() {
        val carrier = ssh ?: return
        val form = _screen.value as? Screen.AddEditSsh ?: return
        if (_sshPublicKey.value != null) return
        val generation = ++sshKeyGeneration
        sshKeyJob?.cancel()
        sshKeyJob = viewModelScope.launch(start = CoroutineStart.LAZY) {
            try {
                val key = carrier.publicKeyLine()
                currentCoroutineContext().ensureActive()
                if (generation == sshKeyGeneration && currentFormId() == form.formId) _sshPublicKey.value = key
            } catch (error: CancellationException) { throw error }
              catch (error: Exception) {
                if (generation == sshKeyGeneration && currentFormId() == form.formId) failure("SshKey", error)
            }
        }.also { it.start() }
    }

    fun submitAdd(alias: String, url: String, auth: AuthType, secret: String, projectPath: String = "", formId: Long? = null) {
        if (formId != null && (_screen.value as? Screen.AddEdit)?.formId != formId) return
        login(alias) { generation ->
            when (val result = controller.addProfile(alias, url, auth, secret, projectPath)) {
                is SessionController.AddResult.DuplicateAlias -> if (generation == loginGeneration)
                    _status.value = UiStatus.Error("A server with this name already exists")
                is SessionController.AddResult.Added -> handleLogin(generation, result.login, result.profile)
                is SessionController.AddResult.Saved -> finishSave(generation)
            }
        }
    }

    /** SSH saving does not connect; passwords exist only in this cancellable call, not draft state. */
    fun submitSsh(alias: String, endpoint: SshEndpoint, password: String = "", formId: Long? = null) {
        if (formId != null && (_screen.value as? Screen.AddEditSsh)?.formId != formId) return
        val current = editing
        if (current != null && current.kind != KernelKind.SSH) return
        login(alias, showLogin = false) { generation ->
            if (current == null) {
                when (controller.addSshKernel(alias, endpoint, password)) {
                    is SessionController.AddResult.DuplicateAlias -> if (generation == loginGeneration)
                        _status.value = UiStatus.Error("A server with this name already exists")
                    is SessionController.AddResult.Added, is SessionController.AddResult.Saved -> finishSave(generation)
                }
            } else {
                controller.editSshKernel(current, alias, endpoint, password)
                finishSave(generation)
            }
        }
    }

    fun submitEdit(current: Profile, alias: String, url: String, auth: AuthType, secret: String, projectPath: String = "", formId: Long? = null) {
        if (formId != null && (_screen.value as? Screen.AddEdit)?.formId != formId) return
        if (current.kind != KernelKind.REMOTE_URL) return
        login(alias) { generation ->
            val result = controller.editProfile(current, alias, url, auth, secret, projectPath)
            handleLogin(generation, result.login, result.persisted)
        }
    }

    private suspend fun finishSave(generation: Long) {
        currentCoroutineContext().ensureActive()
        if (generation == loginGeneration) backToList()
    }

    fun deleteProfile(profile: Profile) {
        formChanged()
        val generation = loginGeneration
        viewModelScope.launch {
            try {
                controller.deleteProfile(profile)
                currentCoroutineContext().ensureActive()
                if (_activeKernel.value?.id == profile.id) _activeKernel.value = null
            } catch (error: CancellationException) { throw error }
              catch (error: Exception) { if (generation == loginGeneration) failure("DeleteProfile", error) }
        }
    }

    private fun login(alias: String, showLogin: Boolean = true, action: suspend (Long) -> Unit) {
        formChanged()
        val generation = loginGeneration
        if (showLogin) _status.value = UiStatus.LoggingIn(alias)
        loginJob = viewModelScope.launch(start = CoroutineStart.LAZY) {
            try { action(generation) }
            catch (error: CancellationException) { throw error }
            catch (error: Exception) { if (generation == loginGeneration) failure("Profiles", error) }
        }.also { it.start() }
    }

    /** Compare saved connection authority, not the runtime endpoint or concurrent recency/cookie stamps. */
    private suspend fun currentForResult(generation: Long, profile: Profile): Profile? {
        currentCoroutineContext().ensureActive()
        if (generation != loginGeneration) return null
        val current = if (profile.kind == KernelKind.LOCAL && profile.id == 0L) profile else dao.getById(profile.id)
        currentCoroutineContext().ensureActive()
        if (generation != loginGeneration) return null
        if (current == null || current.kind != profile.kind || current.alias != profile.alias ||
            current.authType != profile.authType || current.projectPath != profile.projectPath || current.ssh != profile.ssh ||
            (profile.kind == KernelKind.REMOTE_URL && current.baseUrl != profile.baseUrl)) {
            _status.value = UiStatus.Error("This server changed or was deleted. Return to the list and select it again.")
            return null
        }
        return if (profile.kind == KernelKind.REMOTE_URL) current
            else current.copy(baseUrl = profile.baseUrl, cookieHost = profile.cookieHost)
    }

    private suspend fun handleLogin(generation: Long, result: LoginResult, profile: Profile) {
        val current = currentForResult(generation, profile) ?: return
        if (InteractiveSso.shouldOpenWebView(result)) {
            clearFormState()
            _activeKernel.value = current
            _screen.value = Screen.Web(InteractiveSso.profileForWebView(current, result))
            _status.value = UiStatus.Idle
            if (result is LoginResult.Success) scheduleUpdate(restored = true)
            return
        }
        diagnostics?.record("E", "ProfileLogin", "result=${result::class.simpleName}")
        _status.value = when (result) {
            is LoginResult.BadCredentials -> UiStatus.BadCredentials(current)
            is LoginResult.Error -> UiStatus.Error(result.message)
            is LoginResult.Incompatible -> UiStatus.Error(result.message)
            is LoginResult.Success, is LoginResult.NeedsInteractiveSso -> UiStatus.Idle
        }
    }

    fun checkForUpdate() = scheduleUpdate(restored = false)
    fun onNetworkRestored() = scheduleUpdate(restored = true)
    private fun remoteProfiles() = profiles.value.filter { it.kind == KernelKind.REMOTE_URL }

    private fun scheduleUpdate(restored: Boolean) {
        val checker = updateChecker ?: return
        val list = remoteProfiles()
        val urls = list.map { it.baseUrl }.toSet()
        val changed = urls != checkedUrls
        if (changed) {
            updateGeneration++
            updateJob?.cancel()
            checkedUrls = urls
            _update.value = null
            updateSucceeded = false
        }
        if (list.isEmpty() || (updateJob?.isActive == true && !changed)) return
        if (!changed && clock() < nextUpdateAt && !(restored && !updateSucceeded)) return
        val generation = ++updateGeneration
        updateJob = viewModelScope.launch(start = CoroutineStart.LAZY) {
            try {
                val status = checker.check(list)
                currentCoroutineContext().ensureActive()
                if (generation != updateGeneration || remoteProfiles().map { it.baseUrl }.toSet() != urls) return@launch
                updateSucceeded = status != null
                _update.value = status
                nextUpdateAt = clock() + if (updateSucceeded) SUCCESS_COOLDOWN_MS else UPDATE_RETRY_INTERVAL_MS
            } catch (error: CancellationException) { throw error }
              catch (error: Exception) {
                if (generation == updateGeneration) {
                    updateSucceeded = false
                    _update.value = null
                    nextUpdateAt = clock() + UPDATE_RETRY_INTERVAL_MS
                    diagnostics?.record("W", "Update", "check failed: ${error.javaClass.simpleName}")
                }
            }
        }.also { it.start() }
    }

    internal suspend fun awaitUpdateCheck() { updateJob?.join() }
    internal suspend fun awaitLogin() { loginJob?.join() }

    private fun failure(owner: String, error: Exception) {
        // Library messages can contain an offending URL or secret: record only type.
        diagnostics?.record("E", owner, "operation failed: ${error.javaClass.simpleName}")
        _status.value = UiStatus.Error(if (error is IllegalArgumentException)
            "Check the server URL: remove credentials and use a full http(s):// address."
            else "The operation failed. Retry or copy diagnostic logs.")
    }

    override fun onCleared() {
        loginGeneration++
        loginJob?.cancel()
        sshKeyGeneration++
        sshKeyJob?.cancel()
        updateGeneration++
        updateJob?.cancel()
        super.onCleared()
    }

    internal companion object {
        const val UPDATE_RETRY_INTERVAL_MS = 30_000L
        private const val SUCCESS_COOLDOWN_MS = 6 * 60 * 60 * 1000L
    }
}
