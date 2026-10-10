package com.tofu.client.session

import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pins the multi-server aggregation of the update check (no network). */
class UpdateCheckerTest {

    private fun profile(baseUrl: String) = Profile(
        id = 1, alias = "p", baseUrl = baseUrl, authType = AuthType.NONE,
    )

    @Test
    fun picks_the_newest_advertised_version_across_servers() = runTest {
        val advertised = mapOf(
            "https://a.test/" to AdvertisedClient("0.1.15", "ua"),
            "https://b.test/" to AdvertisedClient("0.1.18", "ub"),
        )
        val checker = UpdateChecker("0.1.17") { advertised[it] }

        val status = checker.check(
            listOf(profile("https://a.test/"), profile("https://b.test/")),
        )!!

        assertTrue(status.available)
        assertEquals("0.1.18", status.latest)
        assertEquals("ub", status.downloadUrl)
    }

    @Test
    fun up_to_date_servers_report_not_available() = runTest {
        val checker = UpdateChecker("0.1.18") { AdvertisedClient("0.1.18", "u") }
        val status = checker.check(listOf(profile("https://a.test/")))!!
        assertEquals(false, status.available)
    }

    @Test
    fun no_profiles_returns_null() = runTest {
        assertNull(UpdateChecker("0.1.17") { null }.check(emptyList()))
    }

    @Test
    fun a_throwing_probe_is_ignored() = runTest {
        val checker = UpdateChecker("0.1.17") { url ->
            if (url.contains("bad")) error("boom") else AdvertisedClient("0.1.20", "u")
        }
        val status = checker.check(
            listOf(profile("https://bad.test/"), profile("https://good.test/")),
        )!!
        assertTrue(status.available)
        assertEquals("0.1.20", status.latest)
    }
}
