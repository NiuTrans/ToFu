package com.tofu.client.api

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class ApiV4GeneratedTest {

    private fun metadata(
        apiMajor: Int = 4,
        minAndroidBuild: Int = 17,
    ): ApiMetaResponse = ApiMetaResponse(
        data = ApiMeta(
            apiMajor = apiMajor,
            schemaVersion = 28,
            serverBuild = "0.17.0",
            minDesktopBuild = "0.16.0",
            minAndroidBuild = minAndroidBuild,
        ),
        meta = ResponseMeta(
            requestId = "android-live-minimum",
            serverTimeMs = 1,
        ),
    )

    @Test
    fun `Android compatibility uses the live server minimum`() {
        val value = metadata()
        assertEquals(value, requireAndroidApiCompatibility(value, 17))
        assertThrows(IllegalArgumentException::class.java) {
            requireAndroidApiCompatibility(value, 16)
        }
    }

    @Test
    fun `Android compatibility rejects the wrong API major`() {
        assertThrows(IllegalArgumentException::class.java) {
            requireAndroidApiCompatibility(metadata(apiMajor = 3), 17)
        }
    }

    @Test
    fun `desktop comparator rejects ambiguous build strings`() {
        assertThrows(IllegalArgumentException::class.java) {
            desktopBuildIsCompatible("current", "0.16.0")
        }
    }
}
