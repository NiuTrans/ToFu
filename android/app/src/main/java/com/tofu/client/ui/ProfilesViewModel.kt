package com.tofu.client.ui

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.session.InteractiveSso
import com.tofu.client.session.LoginResult
import com.tofu.client.session.SecretVault
import com.tofu.client.session.SessionController
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/** What the host UI is currently showing. */
sealed interface Screen {
    data object List : Screen
    data object AddEdit : Screen
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
 * Holds the reactive profile list + navigation/status state, and delegates all
 * mutations to [SessionController]. Kept dumb: no session logic lives here (it's
 * all in the tested controller), so the ViewModel is just wiring + state.
 */
class ProfilesViewModel(
    private val dao: ProfileDao,
    private val secrets: SecretVault,
    private val controller: SessionController,
) : ViewModel() {

    val profiles: StateFlow<List<Profile>> =
        dao.observeAll().stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    private val _screen = MutableStateFlow<Screen>(Screen.List)
    val screen: StateFlow<Screen> = _screen

    private val _status = MutableStateFlow<UiStatus>(UiStatus.Idle)
    val status: StateFlow<UiStatus> = _status

    /** The profile currently being edited (null = adding a new one). */
    var editing: Profile? = null
        private set

    /**
     * One-time upgrade migration, run once at app open: fix persisted proxy
     * profiles stuck on the stale NONE default (see
     * [SessionController.migrateProxyAuthDefaults]). Idempotent — safe every
     * launch. The profiles Flow re-emits automatically after the DAO updates.
     */
    fun migrateOnLaunch() {
        viewModelScope.launch { controller.migrateProxyAuthDefaults() }
    }

    fun startAdd() { editing = null; _screen.value = Screen.AddEdit }
    fun startEdit(p: Profile) { editing = p; _screen.value = Screen.AddEdit }
    fun backToList() { _screen.value = Screen.List; _status.value = UiStatus.Idle }

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
        viewModelScope.launch {
            // Navigate with the row the controller actually read back, not the
            // list-rendered snapshot we were handed: that snapshot lags any
            // write the Flow hasn't re-emitted yet, and for SSO it is what
            // WebScreen holds for the entire session.
            val r = controller.activate(profile)
            handleLogin(r.login, r.persisted)
        }
    }

    fun submitAdd(alias: String, url: String, auth: AuthType, secret: String, projectPath: String = "") {
        _status.value = UiStatus.LoggingIn(alias)
        viewModelScope.launch {
            when (val r = controller.addProfile(alias, url, auth, secret, projectPath)) {
                is SessionController.AddResult.DuplicateAlias ->
                    _status.value = UiStatus.Error("A server named \"$alias\" already exists")
                is SessionController.AddResult.Added -> handleLogin(r.login, r.profile)
            }
        }
    }

    fun submitEdit(current: Profile, alias: String, url: String, auth: AuthType, secret: String, projectPath: String = "") {
        _status.value = UiStatus.LoggingIn(alias)
        viewModelScope.launch {
            // Navigate with the row the controller ACTUALLY wrote, never a
            // locally-rebuilt copy: the host-change path nulls cookieHost, and
            // for SSO this object is what WebScreen holds for the whole session.
            val r = controller.editProfile(current, alias, url, auth, secret, projectPath)
            handleLogin(r.login, r.persisted)
        }
    }

    fun deleteProfile(profile: Profile) {
        viewModelScope.launch { controller.deleteProfile(profile) }
    }

    private fun handleLogin(result: LoginResult, profile: Profile) {
        // SSO must NAVIGATE, not just set a status. Its whole design is "the
        // WebView completes the sign-in once, then we persist the jar" — so
        // leaving the user on the list behind an "opening…" label meant Open
        // did nothing at all, while the card copy told them Open was the fix.
        if (InteractiveSso.shouldOpenWebView(result)) {
            _screen.value = Screen.Web(profile)
            _status.value = UiStatus.Idle
            return
        }
        _status.value = when (result) {
            is LoginResult.BadCredentials -> UiStatus.BadCredentials(profile)
            is LoginResult.NoCredential -> UiStatus.BadCredentials(profile)
            is LoginResult.Error -> UiStatus.Error(result.message)
            // Both navigate above; unreachable here.
            is LoginResult.Success, is LoginResult.NeedsInteractiveSso -> UiStatus.Idle
        }
    }
}
