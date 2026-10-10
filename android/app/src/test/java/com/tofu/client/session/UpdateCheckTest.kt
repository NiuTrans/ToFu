package com.tofu.client.session

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the in-app update check: numeric (not lexicographic) version compare,
 * tolerant field extraction from `/api/health`, and the availability verdict.
 */
class UpdateCheckTest {

    private val health = """
        {"ok":true,"build":"abc","bootId":"x",
         "mobile_client_url":"https://example.com/tofu-android.apk",
         "mobile_client_version":"0.1.18"}
    """.trimIndent()

    @Test
    fun version_compare_is_numeric_not_lexicographic() {
        assertTrue(UpdateCheck.compareVersions("0.1.18", "0.1.9") > 0)
        assertTrue(UpdateCheck.compareVersions("0.1.9", "0.1.18") < 0)
        assertEquals(0, UpdateCheck.compareVersions("0.1.17", "0.1.17"))
        // Leading v and trailing pre-release segments are ignored.
        assertTrue(UpdateCheck.compareVersions("v0.2.0", "0.1.99") > 0)
        assertEquals(0, UpdateCheck.compareVersions("0.2.0-rc1", "0.2.0"))
        // Missing segments default to zero.
        assertEquals(0, UpdateCheck.compareVersions("0.2", "0.2.0"))
        assertTrue(UpdateCheck.compareVersions("1.0", "0.9.9") > 0)
    }

    @Test
    fun parses_advertised_fields_from_health() {
        val advertised = UpdateCheck.parseAdvertised(health)
        assertEquals("0.1.18", advertised?.version)
        assertEquals("https://example.com/tofu-android.apk", advertised?.downloadUrl)
    }

    @Test
    fun missing_or_blank_version_yields_null() {
        assertNull(UpdateCheck.parseAdvertised(null))
        assertNull(UpdateCheck.parseAdvertised(""))
        assertNull(UpdateCheck.parseAdvertised("{\"ok\":true}"))
        assertNull(UpdateCheck.parseAdvertised("{\"mobile_client_version\":\"\"}"))
        assertNull(UpdateCheck.parseAdvertised("not json at all"))
    }

    @Test
    fun evaluate_reports_available_only_when_newer() {
        val newer = UpdateCheck.evaluate("0.1.17", AdvertisedClient("0.1.18", "u"))
        assertTrue(newer.available)
        assertEquals("0.1.18", newer.latest)
        assertEquals("u", newer.downloadUrl)

        val same = UpdateCheck.evaluate("0.1.17", AdvertisedClient("0.1.17", "u"))
        assertFalse(same.available)

        val older = UpdateCheck.evaluate("0.1.17", AdvertisedClient("0.1.16", "u"))
        assertFalse(older.available)
    }

    @Test
    fun evaluate_without_advertisement_is_quiet() {
        val status = UpdateCheck.evaluate("0.1.17", null)
        assertFalse(status.available)
        assertEquals("0.1.17", status.installed)
        assertNull(status.downloadUrl)
    }

    @Test
    fun esc_sequences_in_values_are_decoded() {
        val advertised = UpdateCheck.parseAdvertised(
            "{\"mobile_client_version\":\"0.1.18\",\"mobile_client_url\":\"https:\\/\\/x\\/a.apk\"}")
        assertEquals("https://x/a.apk", advertised?.downloadUrl)
    }
}
