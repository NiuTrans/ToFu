package com.tofu.client

import app.cash.paparazzi.DeviceConfig
import app.cash.paparazzi.Paparazzi
import androidx.activity.compose.LocalActivityResultRegistryOwner
import androidx.activity.result.ActivityResultRegistry
import androidx.activity.result.ActivityResultRegistryOwner
import androidx.activity.result.contract.ActivityResultContract
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.ui.Modifier
import androidx.core.app.ActivityOptionsCompat
import com.android.resources.NightMode
import com.android.resources.ScreenOrientation
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import com.tofu.client.session.CookieSink
import com.tofu.client.session.DiagLog
import com.tofu.client.session.SecretLookup
import com.tofu.client.session.SecretVault
import com.tofu.client.session.SessionManager
import com.tofu.client.session.UpdateStatus
import com.tofu.client.ui.AddEditScreen
import com.tofu.client.ui.OnboardingScreen
import com.tofu.client.ui.ProfileListScreen
import com.tofu.client.ui.UiStatus
import com.tofu.client.ui.theme.TofuTheme
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import okhttp3.Cookie
import okhttp3.OkHttpClient
import org.junit.Rule
import org.junit.Test

/**
 * JVM (layoutlib) screenshots of the native shell, recorded at phone and tablet
 * device configs so phone/tablet layout differences are visible without a
 * device. Run `./gradlew :app:recordPaparazziDebug`; PNGs land in
 * `app/src/test/snapshots/images/`. Dev-only — not part of the release APK.
 */
class ScreenshotTest {

    @get:Rule
    val paparazzi = Paparazzi(
        deviceConfig = DeviceConfig.PIXEL_5,
        showSystemUi = true,
    )

    private val host = "a1b2c3d4-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com"
    private val baseUrl = "https://$host/proxy/15000/"

    private val profiles = listOf(
        Profile(
            id = 1, alias = "上海沙盒", baseUrl = baseUrl,
            authType = AuthType.CODE_SERVER_PASSWORD,
            cookieHost = host, projectPath = "/home/dev/tofu", lastUsedAt = 2,
        ),
        Profile(
            id = 2, alias = "Staging", baseUrl = "https://staging.example.com",
            authType = AuthType.NONE, lastUsedAt = 1,
        ),
        Profile(
            id = 3, alias = "Local NAS", baseUrl = "http://mynas.local:8080",
            authType = AuthType.NONE, lastUsedAt = 0,
        ),
    )

    private fun renderList(device: DeviceConfig, name: String) {
        paparazzi.unsafeUpdateConfig(deviceConfig = device)
        paparazzi.snapshot(name = name) {
            ShellContent {
                ProfileListScreen(
                    profiles = profiles,
                    status = UiStatus.Idle,
                    onActivate = {}, onEdit = {}, onDelete = {}, onAdd = {},
                    session = fakeSession(),
                    diag = DiagLog(),
                    update = UpdateStatus(
                        available = true,
                        installed = "0.1.17",
                        latest = "0.1.18",
                        downloadUrl = "https://example.com/tofu-android.apk",
                    ),
                    onUpdate = {},
                    onCheckUpdate = {},
                )
            }
        }
    }

    @Test
    fun list_phone_zh() =
        renderList(DeviceConfig.PIXEL_5.copy(locale = "zh-rCN"), "list-phone-zh")

    @Test
    fun list_tablet_zh() =
        renderList(DeviceConfig.NEXUS_10.copy(locale = "zh-rCN"), "list-tablet-zh")

    @Test
    fun list_phone_en() =
        renderList(DeviceConfig.PIXEL_5, "list-phone-en")

    @Test
    fun list_phone_dark_zh() = renderList(
        DeviceConfig.PIXEL_5.copy(locale = "zh-rCN", nightMode = NightMode.NIGHT),
        "list-phone-dark-zh",
    )

    @Test
    fun add_phone_zh() =
        renderAdd(DeviceConfig.PIXEL_5.copy(locale = "zh-rCN"), "add-phone-zh")

    @Test
    fun add_tablet_zh() =
        renderAdd(DeviceConfig.NEXUS_10.copy(locale = "zh-rCN"), "add-tablet-zh")

    @Test
    fun onboarding_phone_zh() =
        renderOnboarding(DeviceConfig.PIXEL_5.copy(locale = "zh-rCN"), "onboard-phone-zh")

    @Test
    fun onboarding_tablet_zh() =
        renderOnboarding(DeviceConfig.NEXUS_10.copy(locale = "zh-rCN"), "onboard-tablet-zh")

    @Test
    fun list_small_phone_zh() =
        renderList(DeviceConfig.NEXUS_5.copy(locale = "zh-rCN"), "list-small-phone-zh")

    @Test
    fun list_large_phone_zh() =
        renderList(DeviceConfig.PIXEL_6_PRO.copy(locale = "zh-rCN"), "list-large-phone-zh")

    @Test
    fun list_tablet_portrait_zh() = renderList(
        DeviceConfig.NEXUS_10.copy(
            locale = "zh-rCN", orientation = ScreenOrientation.PORTRAIT,
        ),
        "list-tablet-portrait-zh",
    )

    @Test
    fun onboarding_tablet_portrait_zh() = renderOnboarding(
        DeviceConfig.NEXUS_10.copy(
            locale = "zh-rCN", orientation = ScreenOrientation.PORTRAIT,
        ),
        "onboard-tablet-portrait-zh",
    )

    private fun renderAdd(device: DeviceConfig, name: String) {
        paparazzi.unsafeUpdateConfig(deviceConfig = device)
        paparazzi.snapshot(name = name) {
            ShellContent {
                AddEditScreen(
                    editing = null,
                    existingAliases = emptySet(),
                    secretAlreadyStored = false,
                    onCancel = {},
                    onSubmit = { _, _, _, _, _ -> },
                    reusableHostLookup = { _, _ -> null },
                    prefillUrl = baseUrl,
                    prefillAlias = "上海沙盒",
                )
            }
        }
    }

    private fun renderOnboarding(device: DeviceConfig, name: String) {
        paparazzi.unsafeUpdateConfig(deviceConfig = device)
        paparazzi.snapshot(name = name) {
            ShellContent {
                OnboardingScreen(
                    onConnect = { _, _ -> },
                    onManual = {},
                    onSkip = {},
                )
            }
        }
    }

    /** Paparazzi does not provide an ActivityResultRegistry owner, which the QR
     *  scanner hooks require — supply a no-op one so the screens render. */
    private val registryOwner = object : ActivityResultRegistryOwner {
        override val activityResultRegistry: ActivityResultRegistry =
            object : ActivityResultRegistry() {
                override fun <I, O> onLaunch(
                    requestCode: Int,
                    contract: ActivityResultContract<I, O>,
                    input: I,
                    options: ActivityOptionsCompat?,
                ) {}
            }
    }

    @Composable
    private fun ShellContent(content: @Composable () -> Unit) {
        CompositionLocalProvider(
            LocalActivityResultRegistryOwner provides registryOwner,
        ) {
            // Mirror MainActivity: TofuTheme wraps a full-bleed Surface, which
            // is what paints the window background behind screens that don't
            // use a Scaffold (e.g. OnboardingScreen).
            TofuTheme {
                Surface(Modifier.fillMaxSize()) { content() }
            }
        }
    }

    // ── Fakes (ProfileListScreen only needs a constructible SessionManager;
    //     unmanaged/offline cards never call it). ──
    private fun fakeSession(): SessionManager = SessionManager(
        NoopDao(), NoopSecrets(), NoopCookies(), OkHttpClient(),
    )

    private class NoopDao : ProfileDao {
        override fun observeAll(): Flow<List<Profile>> = flowOf(emptyList())
        override suspend fun getById(id: Long): Profile? = null
        override suspend fun getByAlias(alias: String): Profile? = null
        override suspend fun getAllOnce(): List<Profile> = emptyList()
        override suspend fun insert(profile: Profile): Long = 0
        override suspend fun update(profile: Profile): Int = 0
        override suspend fun deleteById(id: Long) {}
        override suspend fun setCookieHost(id: Long, host: String?): Int = 0
        override suspend fun setCookieHostIfUrlMatches(id: Long, expectedUrl: String, host: String): Int {
            val row = getById(id) ?: return 0
            return if (row.baseUrl == expectedUrl) setCookieHost(id, host) else 0
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int = 0
        override suspend fun setAuthType(id: Long, authType: AuthType): Int = 0
    }

    private class NoopSecrets : SecretLookup, SecretVault {
        override fun secretFor(alias: String): String? = null
        override fun putSecret(alias: String, secret: String) {}
        override fun removeSecret(alias: String) {}
    }

    private class NoopCookies : CookieSink {
        override fun inject(origin: String, cookies: List<Cookie>) {}
        override fun purgeHost(host: String) {}
        override fun cookieHeader(origin: String): String? = null
    }
}
