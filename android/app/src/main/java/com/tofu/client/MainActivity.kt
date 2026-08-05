package com.tofu.client

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.lifecycleScope
import androidx.room.Room
import com.tofu.client.data.ProfileDatabase
import com.tofu.client.session.SecretStore
import com.tofu.client.session.SessionController
import com.tofu.client.session.SessionManager
import com.tofu.client.ui.AddEditScreen
import com.tofu.client.ui.ProfileListScreen
import com.tofu.client.ui.ProfilesViewModel
import com.tofu.client.ui.Screen
import com.tofu.client.ui.WebScreen
import com.tofu.client.ui.theme.TofuTheme

/**
 * Single-Activity host. Builds the dependency graph (Room DAO + encrypted
 * secret store + SessionManager + SessionController + ViewModel) and routes
 * between the profile list, the add/edit form, and the WebView host based on
 * the ViewModel's screen state. The SPA is the renderer — this stays thin.
 */
class MainActivity : ComponentActivity() {

    private lateinit var db: ProfileDatabase
    private lateinit var secrets: SecretStore
    private lateinit var session: SessionManager
    private lateinit var vm: ProfilesViewModel

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        // Draw behind the system bars; TofuTheme sets the bar icon contrast and
        // each screen applies its own window insets.
        enableEdgeToEdge()

        db = Room.databaseBuilder(
            applicationContext, ProfileDatabase::class.java, "tofu-profiles.db",
        ).addMigrations(ProfileDatabase.MIGRATION_1_2).build()
        secrets = SecretStore(applicationContext)
        val dao = db.profileDao()
        session = SessionManager(dao, secrets)
        val controller = SessionController(dao, secrets, session)
        vm = ProfilesViewModel(dao, secrets, controller)
        // One-time upgrade fix: flip persisted /proxy/ profiles stuck on the
        // stale NONE default to CODE_SERVER_PASSWORD so they headless-login.
        vm.migrateOnLaunch()

        setContent {
            TofuTheme {
                Surface(Modifier.fillMaxSize()) {
                    val screen by vm.screen.collectAsState()
                    val profiles by vm.profiles.collectAsState()
                    val status by vm.status.collectAsState()

                    when (val s = screen) {
                        is Screen.List -> ProfileListScreen(
                            profiles = profiles,
                            status = status,
                            onActivate = vm::activate,
                            onEdit = vm::startEdit,
                            onDelete = vm::deleteProfile,
                            onAdd = vm::startAdd,
                            session = session,
                        )
                        is Screen.AddEdit -> {
                            val editing = vm.editing
                            AddEditScreen(
                                editing = editing,
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
                            )
                        }
                        is Screen.Web -> WebScreen(
                            profile = s.profile,
                            session = session,
                            scope = lifecycleScope,
                            onBack = vm::backToList,
                        )
                    }
                }
            }
        }
    }
}
