package com.tofu.client.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.tofu.client.data.AuthType
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.InteractiveSso
import com.tofu.client.session.KernelControllers
import com.tofu.client.session.KernelList
import com.tofu.client.session.KernelState
import com.tofu.client.session.LoginResult
import com.tofu.client.session.SecretVault
import com.tofu.client.session.SessionController
import com.tofu.client.session.UpdateChecker
import com.tofu.client.session.UpdateStatus
import com.tofu.client.ssh.SshSetup
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.CancellationException
import com.tofu.client.session.DiagLog

/** What the host UI is currently showing. */
sealed interface Screen {
    data object List : Screen
    data object AddEdit : Screen
    /** Add or edit an SSH kernel; [ProfilesViewModel.editing] null = add. */
    data object AddEditSsh : Screen
    data class Web(val profile: Profile) : Screen
}

/** A transient status the UI surfaces (login progress / errors / SSO hand-off). */
sealed interface UiStatus {
    data object Idle : UiStatus
    data class LoggingIn(val alias: String) : UiStatus
    data class Error(val message: String) : UiStatus
    data class BadCredentials(val profile: Profile) : UiStatus
}

/**
 * Holds the reactive Kernels list + navigation/status state, and delegates all
 * mutations to [SessionController]. Kept dumb: no session logic lives here (it's
 * all in the tested controller), so the ViewModel is just wiring + state.
 */
class ProfilesViewModel(
    private val dao: ProfileDao,
    private val secrets: SecretVault,
    private val controller: SessionController,
    private val kernels: KernelControllers,
    /** Optional so unit tests / previews can omit it; null disables the check. */
    private val updateChecker: UpdateChecker? = null,
    private val diagnostics: DiagLog? = null,
) : ViewModel() {

    private val _status = MutableStateFlow<UiStatus>(UiStatus.Idle)
    val status: StateFlow<UiStatus> = _status

    val profiles: StateFlow<List<Profile>> = dao.observeAll().catch { error ->
        if (error is CancellationException) throw error
        diagnostics?.exception("ProfileStorage", error)
        _status.value = UiStatus.Error(error.message ?: error.javaClass.simpleName)
        emit(emptyList())
    }.stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    /** What the Kernels list shows: [KernelList.pinned] over the stored rows. */
    val kernelList: StateFlow<List<Profile>> = profiles.map(KernelList::pinned)
        .stateIn(viewModelScope, SharingStarted.Eagerly, KernelList.pinned(emptyList()))

    /**
     * The kernel in front: the one last opened in the WebView (bound to its live
     * endpoint for LOCAL and SSH), until it is deleted. Null before any open.
     */
    private val _activeKernel = MutableStateFlow<Profile?>(null)
    val activeKernel: StateFlow<Profile?> = _activeKernel

    private val _screen = MutableStateFlow<Screen>(Screen.List)
    val screen: StateFlow<Screen> = _screen

    /** The newest client version any configured server advertises, or null. */
    private val _update = MutableStateFlow<UpdateStatus?>(null)
    val update: StateFlow<UpdateStatus?> = _update
    private var updateChecked = false

    /** The profile currently being edited (null = adding a new one). */
    var editing: Profile? = null
        private set

    /**
     * The SSH carrier's own affordances (this phone's key, host-key trust);
     * null when the SSH slot holds a controller without them.
     */
    private val ssh: SshSetup? get() = kernels.of(KernelKind.SSH) as? SshSetup

    /** This phone's SSH public key, loaded (and first generated) when the SSH form opens. */
    private val _sshPublicKey = MutableStateFlow<String?>(null)
    val sshPublicKey: StateFlow<String?> = _sshPublicKey

    /**
     * The key an SSH kernel's host presented on a first connect, which the SSH
     * form asks the user to check and trust; null otherwise. Like [editing],
     * it belongs to the form being shown.
     */
    var hostKeyToTrust: String? = null
        private set

    /** Fields a scanned QR / pasted URL / deep link pre-fills the add form with. */
    data class Prefill(val url: String?, val alias: String?)

    var addPrefill: Prefill? = null
        private set

    /**
     * One-shot update check across every configured server. Idempotent and
     * cheap to call on each list composition; it does nothing until at least one
     * profile exists, and only ever runs once per process.
     */
    fun checkForUpdate() {
        val checker = updateChecker ?: return
        val list = profiles.value
        if (updateChecked || list.isEmpty()) return
        updateChecked = true
        operation("Update") { _update.value = checker.check(list) }
    }



    fun startAdd(prefillUrl: String? = null, prefillAlias: String? = null) {
        editing = null
        addPrefill = Prefill(prefillUrl, prefillAlias)
        _screen.value = Screen.AddEdit
    }

    fun startAddSsh() {
        editing = null
        addPrefill = null
        hostKeyToTrust = null
        loadSshPublicKey()
        _screen.value = Screen.AddEditSsh
    }

    /** "This phone" has no editable fields, so editing it does nothing. */
    fun startEdit(p: Profile) {
        val form = when (p.kind) {
            KernelKind.REMOTE_URL -> Screen.AddEdit
            KernelKind.SSH -> Screen.AddEditSsh
            KernelKind.LOCAL -> return
        }
        editing = p
        addPrefill = null
        hostKeyToTrust = null
        if (p.kind == KernelKind.SSH) loadSshPublicKey()
        _screen.value = form
    }

    fun kernelState(profile: Profile): StateFlow<KernelState> = kernels.state(profile)

    fun backToList() { _screen.value = Screen.List; _status.value = UiStatus.Idle; hostKeyToTrust = null }

    fun secretStoredFor(alias: String): Boolean = secrets.secretFor(alias) != null

    /**
     * If [url] shares a host with another profile that already has a stored
     * password, return that host (for a proactive "password will be reused"
     * hint). [excludeAlias] skips the profile being edited. Null = nothing to
     * reuse. Delegates to the tested [SessionController.findSharedSecret].
     */
    suspend fun reusableSecretHost(url: String, excludeAlias: String?): String? {
        if (controller.findSharedSecret(url, excludeAlias) == null) return null
        return com.tofu.client.session.ServerUrl.parse(url)?.host
    }

    fun activate(profile: Profile) {
        _status.value = UiStatus.LoggingIn(profile.alias)
        operation("Profiles") {
            // Navigate with the row the controller actually read back, not the
            // list-rendered snapshot we were handed: that snapshot lags any
            // write the Flow hasn't re-emitted yet, and for SSO it is what
            // WebScreen holds for the entire session.
            val r = controller.activate(profile)
            val untrusted = if (r.persisted.kind == KernelKind.SSH) ssh?.hostKeyToTrust(r.persisted) else null
            if (untrusted != null) askToTrust(r.persisted, untrusted) else handleLogin(r.login, r.persisted)
        }
    }

    /** A first connect stopped at [kernel]'s unpinned host key: show it on the SSH form. */
    private fun askToTrust(kernel: Profile, fingerprint: String) {
        editing = kernel
        addPrefill = null
        hostKeyToTrust = fingerprint
        loadSshPublicKey()
        _status.value = UiStatus.Idle
        _screen.value = Screen.AddEditSsh
    }

    /** The user checked the host key the SSH form shows: pin exactly that key, then connect. */
    fun trustHostKey() {
        val kernel = editing ?: return
        val fingerprint = hostKeyToTrust ?: return
        operation("Profiles") {
            hostKeyToTrust = null
            if (ssh?.trustHostKey(kernel, fingerprint) == true) {
                activate(kernel)
            } else {
                _status.value = UiStatus.Error("The kernel changed before its host key was pinned. Connect again to check the key.")
            }
        }
    }

    private fun loadSshPublicKey() {
        val carrier = ssh ?: return
        if (_sshPublicKey.value == null) operation("SshKey") { _sshPublicKey.value = carrier.publicKeyLine() }
    }

    fun submitAdd(alias: String, url: String, auth: AuthType, secret: String, projectPath: String = "") {
        _status.value = UiStatus.LoggingIn(alias)
        operation("Profiles") {
            when (val r = controller.addProfile(alias, url, auth, secret, projectPath)) {
                is SessionController.AddResult.DuplicateAlias ->
                    _status.value = UiStatus.Error("A server named \"$alias\" already exists")
                is SessionController.AddResult.Added -> handleLogin(r.login, r.profile)
                is SessionController.AddResult.Saved -> backToList()
            }
        }
    }

    /**
     * Save the SSH kernel being added or [editing], with the SSH account's
     * [password] when one is typed. Saving never connects: the list shows the
     * kernel's state, and tapping it connects.
     */
    fun submitSsh(alias: String, endpoint: SshEndpoint, password: String = "") {
        val current = editing
        operation("Profiles") {
            if (current == null) {
                when (controller.addSshKernel(alias, endpoint, password)) {
                    is SessionController.AddResult.DuplicateAlias ->
                        _status.value = UiStatus.Error("A server named \"$alias\" already exists")
                    is SessionController.AddResult.Added, is SessionController.AddResult.Saved -> backToList()
                }
            } else {
                controller.editSshKernel(current, alias, endpoint, password)
                backToList()
            }
        }
    }

    fun submitEdit(current: Profile, alias: String, url: String, auth: AuthType, secret: String, projectPath: String = "") {
        _status.value = UiStatus.LoggingIn(alias)
        operation("Profiles") {
            // Navigate with the row the controller ACTUALLY wrote, never a
            // locally-rebuilt copy: the host-change path nulls cookieHost, and
            // for SSO this object is what WebScreen holds for the whole session.
            val r = controller.editProfile(current, alias, url, auth, secret, projectPath)
            handleLogin(r.login, r.persisted)
        }
    }

    fun deleteProfile(profile: Profile) {
        operation("DeleteProfile") {
            controller.deleteProfile(profile)
            if (_activeKernel.value?.id == profile.id) _activeKernel.value = null
        }
    }

    private fun operation(name: String, action: suspend () -> Unit) {
        viewModelScope.launch {
            try { action() }
            catch (error: CancellationException) { throw error }
            catch (error: Exception) {
                diagnostics?.exception(name, error)
                _status.value = UiStatus.Error(error.message ?: error.javaClass.simpleName)
            }
        }
    }

    private fun handleLogin(result: LoginResult, profile: Profile) {
        // SSO must NAVIGATE, not just set a status. Its whole design is "the
        // WebView completes the sign-in once, then we persist the jar" — so
        // leaving the user on the list behind an "opening…" label meant Open
        // did nothing at all, while the card copy told them Open was the fix.
        if (InteractiveSso.shouldOpenWebView(result)) {
            _activeKernel.value = profile
            _screen.value = Screen.Web(InteractiveSso.profileForWebView(profile, result))
            _status.value = UiStatus.Idle
            return
        }
        diagnostics?.record("E", "ProfileLogin", "profile=${profile.alias} result=$result")
        _status.value = when (result) {
            is LoginResult.BadCredentials -> UiStatus.BadCredentials(profile)
            is LoginResult.Error -> UiStatus.Error(result.message)
            is LoginResult.Incompatible -> UiStatus.Error(result.message)
            // Both navigate above; unreachable here.
            is LoginResult.Success, is LoginResult.NeedsInteractiveSso -> UiStatus.Idle
        }
    }
}
