package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.data.ProfileDao
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.test.runTest
import okhttp3.Cookie
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the re-provision invariant: when a profile's URL HOST changes,
 * [SessionManager.updateUrlAndReauth] must purge the OLD host's Domain-pinned
 * jar BEFORE re-login; when the host is unchanged it must NOT purge.
 */
class SessionManagerReauthTest {

    // ── Fakes ─────────────────────────────────────────────────────────────
    private class FakeCookieSink : CookieSink {
        val purged = mutableListOf<String>()
        val injected = mutableListOf<String>()
        override fun inject(origin: String, cookies: List<Cookie>) { injected += origin }
        override fun purgeHost(host: String) { purged += host }
        override fun cookieHeader(origin: String): String? = null
    }

    private class FakeSecrets(private val secret: String?) : SecretLookup {
        override fun secretFor(alias: String): String? = secret
    }

    private class FakeDao(private var current: Profile) : ProfileDao {
        val updates = mutableListOf<Profile>()
        override fun observeAll(): Flow<List<Profile>> = flowOf(listOf(current))
        override suspend fun getById(id: Long): Profile? = current
        override suspend fun getAllOnce(): List<Profile> = listOf(current)
        override suspend fun getByAlias(alias: String): Profile? = current
        override suspend fun insert(profile: Profile): Long = 1
        override suspend fun update(profile: Profile): Int { current = profile; updates += profile; return 1 }
        override suspend fun deleteById(id: Long) {}
        override suspend fun setCookieHost(id: Long, host: String?): Int {
            current = current.copy(cookieHost = host); return 1
        }
        override suspend fun touchLastUsed(id: Long, timestamp: Long): Int {
            current = current.copy(lastUsedAt = timestamp); return 1
        }
        override suspend fun setAuthType(id: Long, authType: AuthType): Int {
            current = current.copy(authType = authType); return 1
        }
    }

    private fun profile(url: String, cookieHost: String?) = Profile(
        id = 1,
        alias = "dc1 tofu",
        instanceUuid = "00000000-0000-4000-8000-000000000001",
        baseUrl = url,
        authType = AuthType.CODE_SERVER_PASSWORD,
        cookieHost = cookieHost,
    )

    private val oldHost = "00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com"
    private val newHost = "aaaa1111-2222-3333-4444-bbbbccccdddd-vscode-dc1.codelab.example.com"

    @Test
    fun host_change_purges_old_jar_before_relogin() = runTest {
        val cookies = FakeCookieSink()
        // No secret → login short-circuits to NoCredential (we're asserting the
        // PURGE side-effect, not the network login itself).
        val mgr = SessionManager(
            dao = FakeDao(profile("https://$oldHost/proxy/15000/", cookieHost = oldHost)),
            secrets = FakeSecrets(null),
            cookies = cookies,
        )

        mgr.updateUrlAndReauth(
            profile("https://$oldHost/proxy/15000/", cookieHost = oldHost),
            newUrl = "https://$newHost/proxy/15000/",
        )

        // NEUTER CHECK: delete the `cookies.purgeHost(oldHost)` call in
        // updateUrlAndReauth and this fails — a dead-host jar would survive and
        // be replayed against the new host.
        assertEquals(listOf(oldHost), cookies.purged)
    }

    @Test
    fun same_host_does_not_purge() = runTest {
        val cookies = FakeCookieSink()
        val mgr = SessionManager(
            dao = FakeDao(profile("https://$oldHost/proxy/15000/", cookieHost = oldHost)),
            secrets = FakeSecrets(null),
            cookies = cookies,
        )

        // Same host, only the proxy PORT changed → jar is still valid, no purge.
        mgr.updateUrlAndReauth(
            profile("https://$oldHost/proxy/15000/", cookieHost = oldHost),
            newUrl = "https://$oldHost/proxy/16000/",
        )

        assertTrue("must not purge on same-host edit: ${cookies.purged}", cookies.purged.isEmpty())
    }
}
