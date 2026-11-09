package com.tofu.client

import android.content.Intent
import android.os.Bundle
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import androidx.activity.compose.BackHandler
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.key
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.repeatOnLifecycle
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import androidx.room.Room
import com.tofu.client.data.OnboardingStore
import com.tofu.client.data.ProfileDatabase
import com.tofu.client.session.DeepLink
import com.tofu.client.session.DiagLog
import com.tofu.client.session.KernelControllers
import com.tofu.client.session.SecretStore
import com.tofu.client.session.SessionController
import com.tofu.client.session.SessionManager
import com.tofu.client.session.SupervisorClient
import com.tofu.client.session.UpdateProbe
import com.tofu.client.session.UrlKernelController
import com.tofu.client.ui.StartupFailureScreen
import com.tofu.client.ui.copyDiagnostics
import com.tofu.client.session.UpdateChecker
import com.tofu.client.ui.AddEditScreen
import com.tofu.client.ui.OnboardingScreen
import com.tofu.client.ui.ProfileListScreen
import com.tofu.client.ui.ProfilesViewModel
import com.tofu.client.ui.SshKernelScreen
import com.tofu.client.ui.UiStatus
import com.tofu.client.ui.Screen
import com.tofu.client.ui.WebScreen
import com.tofu.client.ui.openUrl
import com.tofu.client.ui.theme.TofuTheme

/**
 * Single-Activity host. Builds the dependency graph (Room DAO + encrypted
 * secret store + SessionManager + SessionController + ViewModel) and routes
 * between first-run onboarding, the profile list, the add/edit form, and the
 * WebView host based on the ViewModel's screen state. The SPA is the renderer —
 * this stays thin.
 *
 * It also owns the two Activity-level entry points the shell adds on top of the
 * SPA: inbound `tofu://` deep links (which pre-fill the add form) and the
 * cooldown-bounded update check.
 */
class MainActivity : ComponentActivity() {

    private lateinit var diag: DiagLog
    private lateinit var store: OnboardingStore
    private lateinit var vm: ProfilesViewModel
    private val session: SessionManager get() = vm.session
    private var networkCallback: ConnectivityManager.NetworkCallback? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Draw behind the system bars; TofuTheme sets the bar icon contrast and
        // each screen applies its own window insets.
        enableEdgeToEdge()

        diag = (application as TofuApplication).diagnostics
        diag.record("I", "Activity", "create restored=${savedInstanceState != null}")
        try {
            val appContext = applicationContext
            store = OnboardingStore(appContext)
            val diagnostics = diag
            // The factory runs only for a NEW ViewModelStore entry. Rotation
            // reuses its graph, navigation and owned cancellable operations.
            val factory = object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T {
                    require(modelClass == ProfilesViewModel::class.java)
                    val database = Room.databaseBuilder(
                        appContext, ProfileDatabase::class.java, "tofu-profiles.db",
                    ).addMigrations(*ProfileDatabase.ALL_MIGRATIONS).build()
                    var sessionOwner: SessionManager? = null
                    var updateOwner: UpdateProbe? = null
                    fun releaseGraph() {
                        try { updateOwner?.close() }
                        finally { try { sessionOwner?.close() } finally { database.close() } }
                    }
                    try {
                        val secrets = SecretStore(appContext)
                        val dao = database.profileDao()
                        val session = SessionManager(dao, secrets, appVersionCode = BuildConfig.VERSION_CODE, diagnostics = diagnostics)
                        sessionOwner = session
                        val kernels = KernelControllers(
                            remoteUrl = UrlKernelController(SupervisorClient(diagnostics = diagnostics), login = session::login),
                            local = com.tofu.client.session.localKernelController(appContext, diagnostics),
                            ssh = com.tofu.client.ssh.SshKernelController.forApp(appContext, dao, secrets),
                        )
                        val probe = UpdateProbe(diagnostics = diagnostics)
                        updateOwner = probe
                        val controller = SessionController(dao, secrets, session, kernels, report = { diagnostics.record("I", "Profiles", it) })
                        return ProfilesViewModel(
                            dao, secrets, controller, kernels, session = session,
                            updateChecker = UpdateChecker(BuildConfig.VERSION_NAME, probe::advertisedCancellable),
                            diagnostics = diagnostics, release = ::releaseGraph,
                        ) as T
                    } catch (error: Exception) {
                        try { releaseGraph() }
                        catch (closing: Exception) { diagnostics.record("E", "Startup", "graph cleanup failed: ${closing.javaClass.simpleName}") }
                        throw error
                    }
                }
            }
            vm = ViewModelProvider(this, factory)[ProfilesViewModel::class.java]
            // A cold launch straight from a deep link (e.g. a scanned/clicked
            // `tofu://connect?url=…`) opens the add form pre-filled.
            if (savedInstanceState == null) handleDeepLink(intent)
        } catch (error: Exception) {
            diag.exception("Startup", error)
            setContent { TofuTheme { StartupFailureScreen(diag) } }
            return
        }

        setContent {
            TofuTheme {
                Surface(Modifier.fillMaxSize()) {
                    val context = LocalContext.current
                    val screen by vm.screen.collectAsState()
                    val profiles by vm.profiles.collectAsState()
                    val kernelList by vm.kernelList.collectAsState()
                    val activeKernel by vm.activeKernel.collectAsState()
                    val status by vm.status.collectAsState()
                    val update by vm.update.collectAsState()
                    val prefill by vm.addPrefill.collectAsState()
                    val pendingPrefill by vm.pendingPrefill.collectAsState()
                    BackHandler(enabled = screen is Screen.AddEdit || screen is Screen.AddEditSsh ||
                        (screen is Screen.List && status is UiStatus.LoggingIn)) { vm.backToList() }
                    // One timer only while the list is visible; the ViewModel
                    // gates network work by failure/success cooldown.
                    LaunchedEffect(screen, profiles.map { it.baseUrl }) {
                        if (screen is Screen.List) lifecycle.repeatOnLifecycle(Lifecycle.State.STARTED) {
                            while (isActive) {
                                vm.checkForUpdate()
                                delay(ProfilesViewModel.UPDATE_RETRY_INTERVAL_MS)
                            }
                        }
                    }
                    LaunchedEffect(screen) { diag.record("I", "Screen", screen::class.simpleName.orEmpty()) }
                    var onboardingDone by remember { mutableStateOf(store.isDone()) }

                    // Completing setup once (any profile added) permanently
                    // retires first-run onboarding, even if the list is later
                    // emptied by deletions.
                    LaunchedEffect(profiles.isNotEmpty()) {
                        if (profiles.isNotEmpty() && !onboardingDone) {
                            store.markDone()
                            onboardingDone = true
                        }
                    }

                    // Remembered callbacks: a fresh method reference/lambda per
                    // composition is an unstable parameter, which makes the list
                    // (and every row) recompose on each parent recomposition.
                    val onActivate = remember { vm::activate }
                    val onEdit = remember { vm::startEdit }
                    val onDelete = remember { vm::deleteProfile }
                    val onAdd = remember { { vm.startAdd() } }
                    val onAddSsh = remember { vm::startAddSsh }
                    val kernelState = remember { vm::kernelState }

                    when (val s = screen) {
                        is Screen.List -> if (profiles.isEmpty() && !onboardingDone && status !is UiStatus.Error) {
                            OnboardingScreen(
                                onConnect = { url, alias -> vm.startAdd(url, alias) },
                                onManual = { vm.startAdd() },
                                onDiagnostics = { copyDiagnostics(context, diag) },
                                onSkip = {
                                    store.markDone()
                                    onboardingDone = true
                                },
                            )
                        } else {
                            ProfileListScreen(
                                profiles = kernelList,
                                status = status,
                                onActivate = onActivate,
                                onEdit = onEdit,
                                onDelete = onDelete,
                                onAdd = onAdd,
                                onAddSsh = onAddSsh,
                                session = session,
                                diag = diag,
                                kernelState = kernelState,
                                activeKernel = activeKernel,
                                update = update,
                                onUpdate = { openUrl(context, it) },
                                onCheckUpdate = vm::checkForUpdate,
                            )
                        }
                        is Screen.AddEdit -> key(s.formId) {
                            val editing = vm.editing
                            AddEditScreen(
                                editing = editing,
                                diagnostics = diag,
                                status = status,
                                existingAliases = profiles.map { it.alias }.toSet(),
                                secretAlreadyStored =
                                    editing != null && vm.secretStoredFor(editing.alias),
                                onCancel = { vm.backToList(s.formId) },
                                onSubmit = { alias, url, auth, secret, projectPath ->
                                    if (editing == null) vm.submitAdd(alias, url, auth, secret, projectPath, s.formId)
                                    else vm.submitEdit(editing, alias, url, auth, secret, projectPath, s.formId)
                                },
                                reusableHostLookup = { url, excludeAlias ->
                                    vm.reusableSecretHost(url, excludeAlias)
                                },
                                prefillUrl = prefill?.url,
                                prefillAlias = prefill?.alias,
                                pendingPrefill = pendingPrefill,
                                initialDraft = vm.formDraft,
                                onDraftChanged = { vm.retainFormDraft(it, s.formId) },
                                onPrefillAccepted = { revision -> vm.acceptPrefill(s.formId, revision) },
                                onPrefillDismissed = { revision -> vm.dismissPrefill(s.formId, revision) },
                                onFormChanged = { vm.formChanged(s.formId) },
                                onConnectOffered = { connect -> vm.offerConnect(connect, s.formId) },
                            )
                        }
                        is Screen.AddEditSsh -> key(s.formId) {
                            SshKernelScreen(
                                editing = vm.editing,
                                existingAliases = profiles.map { it.alias }.toSet(),
                                onCancel = { vm.backToList(s.formId) },
                                onSubmit = { alias, endpoint, password -> vm.submitSsh(alias, endpoint, password, s.formId) },
                                status = status,
                                publicKey = vm.sshPublicKey.collectAsState().value,
                                passwordStored = vm.editing?.let { vm.secretStoredFor(it.alias) } == true,
                                hostKeyToTrust = vm.hostKeyToTrust,
                                onTrust = { vm.trustHostKey(s.formId) },
                                kernelState = kernelState,
                            )
                        }
                        is Screen.Web -> WebScreen(
                            profile = s.profile,
                            session = session,
                            diag = diag,
                            scope = lifecycleScope,
                            onBack = vm::backToList,
                        )
                    }
                    // A remote connect link cannot silently displace an SSH
                    // form or the live kernel. Keep it as metadata until the
                    // user deliberately selects the new URL review form.
                    if (screen !is Screen.AddEdit) pendingPrefill?.let { incoming ->
                        val formId = (screen as? Screen.AddEditSsh)?.formId
                        AlertDialog(
                            onDismissRequest = { vm.dismissPrefill(formId = formId, revision = incoming.revision) },
                            title = { Text(stringResource(R.string.prefill_confirm_title)) },
                            text = { Text(stringResource(R.string.prefill_confirm_message)) },
                            confirmButton = { TextButton(onClick = { vm.acceptPrefill(formId = formId, revision = incoming.revision) }) {
                                Text(stringResource(R.string.prefill_use_link))
                            } },
                            dismissButton = { TextButton(onClick = { vm.dismissPrefill(formId = formId, revision = incoming.revision) }) {
                                Text(stringResource(R.string.prefill_keep_form))
                            } },
                        )
                    }
                }
            }
        }
    }

    override fun onStart() {
        super.onStart()
        if (::diag.isInitialized) diag.record("I", "Activity", "foreground")
        if (!::vm.isInitialized) return
        vm.checkForUpdate()
        val manager = getSystemService(ConnectivityManager::class.java) ?: return
        val callback = object : ConnectivityManager.NetworkCallback() {
            private var validated = false
            override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) {
                val available = capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)
                val owner = this
                if (available && !validated) lifecycleScope.launch {
                    if (networkCallback === owner && lifecycle.currentState.isAtLeast(Lifecycle.State.STARTED))
                        vm.onNetworkRestored()
                }
                validated = available
            }
            override fun onLost(network: Network) { validated = false }
        }
        try { manager.registerDefaultNetworkCallback(callback); networkCallback = callback }
        catch (error: Exception) { diag.record("W", "UpdateNetwork", "observer unavailable: ${error.javaClass.simpleName}") }
    }

    override fun onStop() {
        networkCallback?.let { callback ->
            try { getSystemService(ConnectivityManager::class.java)?.unregisterNetworkCallback(callback) }
            catch (error: Exception) { diag.record("W", "UpdateNetwork", "observer release failed: ${error.javaClass.simpleName}") }
        }
        networkCallback = null
        if (::diag.isInitialized) diag.record("I", "Activity", "background")
        super.onStop()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleDeepLink(intent)
    }

    /** Pre-fill the add form when the app is opened via a `tofu://` link. */
    private fun handleDeepLink(intent: Intent?) {
        val payload = intent?.data?.toString() ?: return
        // Consume the external URI; never keep a rejected credential-bearing
        // payload as the Activity's recreation initializer.
        intent.data = null
        if (!::vm.isInitialized) { diag.record("W", "DeepLink", "ignored while startup failed"); return }
        val connect = DeepLink.parse(payload) ?: run {
            diag.record("W", "DeepLink", "invalid or credential-bearing connect link")
            vm.rejectConnectLink()
            return
        }
        diag.record("I", "DeepLink", "connect ${com.tofu.client.session.ClientReport.safeUrl(connect.url)}")
        vm.startAdd(connect.url, connect.alias)
    }
}
