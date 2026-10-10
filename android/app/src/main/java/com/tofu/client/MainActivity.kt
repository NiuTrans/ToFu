package com.tofu.client

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.lifecycleScope
import androidx.room.Room
import com.tofu.client.data.OnboardingStore
import com.tofu.client.data.ProfileDatabase
import com.tofu.client.session.DeepLink
import com.tofu.client.session.DiagLog
import com.tofu.client.session.SecretStore
import com.tofu.client.session.SessionController
import com.tofu.client.session.SessionManager
import com.tofu.client.session.UpdateProbe
import com.tofu.client.ui.StartupFailureScreen
import com.tofu.client.ui.copyDiagnostics
import com.tofu.client.session.UpdateChecker
import com.tofu.client.ui.AddEditScreen
import com.tofu.client.ui.OnboardingScreen
import com.tofu.client.ui.ProfileListScreen
import com.tofu.client.ui.ProfilesViewModel
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
 * one-shot update check.
 */
class MainActivity : ComponentActivity() {

    private lateinit var diag: DiagLog
    private lateinit var db: ProfileDatabase
    private lateinit var secrets: SecretStore
    private lateinit var session: SessionManager
    private lateinit var store: OnboardingStore
    private lateinit var vm: ProfilesViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Draw behind the system bars; TofuTheme sets the bar icon contrast and
        // each screen applies its own window insets.
        enableEdgeToEdge()

        diag = (application as TofuApplication).diagnostics
        diag.record("I", "Activity", "create restored=${savedInstanceState != null}")
        try {
            db = Room.databaseBuilder(
                applicationContext, ProfileDatabase::class.java, "tofu-profiles.db",
            ).addMigrations(ProfileDatabase.MIGRATION_1_2).build()
            secrets = SecretStore(applicationContext)
            store = OnboardingStore(applicationContext)
            // One process-wide diagnostic ring, shared by every screen so a report
            // from the list carries the events leading up to it (logins, failures)
            // and the WebView's own console output.
            val dao = db.profileDao()
            session = SessionManager(dao, secrets, appVersionCode = BuildConfig.VERSION_CODE, diagnostics = diag)
            val controller = SessionController(dao, secrets, session, report = { diag.record("I", "Profiles", it) })
            vm = ProfilesViewModel(
                dao, secrets, controller,
                updateChecker = UpdateChecker(BuildConfig.VERSION_NAME, UpdateProbe(diagnostics = diag)::advertised),
                diagnostics = diag,
            )
            // A cold launch straight from a deep link (e.g. a scanned/clicked
            // `tofu://connect?url=…`) opens the add form pre-filled.
            handleDeepLink(intent)
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
                    val status by vm.status.collectAsState()
                    val update by vm.update.collectAsState()
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
                                profiles = profiles,
                                status = status,
                                onActivate = onActivate,
                                onEdit = onEdit,
                                onDelete = onDelete,
                                onAdd = onAdd,
                                session = session,
                                diag = diag,
                                update = update,
                                onUpdate = { openUrl(context, it) },
                                onCheckUpdate = vm::checkForUpdate,
                            )
                        }
                        is Screen.AddEdit -> {
                            val editing = vm.editing
                            AddEditScreen(
                                editing = editing,
                                diagnostics = diag,
                                status = status,
                                existingAliases = profiles.map { it.alias }.toSet(),
                                secretAlreadyStored =
                                    editing != null && vm.secretStoredFor(editing.alias),
                                onCancel = vm::backToList,
                                onSubmit = { alias, url, auth, secret, projectPath ->
                                    if (editing == null) vm.submitAdd(alias, url, auth, secret, projectPath)
                                    else vm.submitEdit(editing, alias, url, auth, secret, projectPath)
                                },
                                reusableHostLookup = { url, excludeAlias ->
                                    vm.reusableSecretHost(url, excludeAlias)
                                },
                                prefillUrl = vm.addPrefill?.url,
                                prefillAlias = vm.addPrefill?.alias,
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
                }
            }
        }
    }

    override fun onStart() { super.onStart(); if (::diag.isInitialized) diag.record("I", "Activity", "foreground") }
    override fun onStop() { if (::diag.isInitialized) diag.record("I", "Activity", "background"); super.onStop() }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleDeepLink(intent)
    }

    /** Pre-fill the add form when the app is opened via a `tofu://` link. */
    private fun handleDeepLink(intent: Intent?) {
        val payload = intent?.data?.toString() ?: return
        if (!::vm.isInitialized) { diag.record("W", "DeepLink", "ignored while startup failed"); return }
        val connect = DeepLink.parse(payload) ?: run { diag.record("W", "DeepLink", "invalid connect link"); return }
        diag.record("I", "DeepLink", "connect ${com.tofu.client.session.ClientReport.safeUrl(connect.url)}")
        vm.startAdd(connect.url, connect.alias)
    }
}
