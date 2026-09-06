package com.tofu.client.session

import com.tofu.client.api.ApiV4Contract
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pins the v4 meta preflight decisions: fail-CLOSED only on a definitive
 * mismatch (wrong API major, minAndroidBuild above us), fail-OPEN on every
 * partial-knowledge shape (404, transport failure, unparseable body).
 */
class ApiMetaGateTest {

    @Test
    fun metaUrl_keeps_the_vscode_proxy_prefix() {
        // NEUTER: resolving the absolute META_PATH against the origin would
        // drop /proxy/15000/ and 404 every code-server deploy's preflight.
        assertEquals(
            "https://h.example.com/proxy/15000/api/v4/meta",
            ApiMetaGate.metaUrl("https://h.example.com/proxy/15000/"),
        )
        assertEquals(
            "https://h.example.com/proxy/15000/api/v4/meta",
            ApiMetaGate.metaUrl("https://h.example.com/proxy/15000"),
        )
    }

    @Test
    fun missing_or_partial_meta_never_blocks() {
        // 404: a server that predates the meta endpoint.
        assertNull(ApiMetaGate.incompatibilityReason(404, null, 17))
        // Transport-level oddity: no body to judge by.
        assertNull(ApiMetaGate.incompatibilityReason(200, null, 17))
        assertNull(ApiMetaGate.incompatibilityReason(200, "", 17))
        // Unparseable / wrong shape.
        assertNull(ApiMetaGate.incompatibilityReason(200, "not json", 17))
        assertNull(ApiMetaGate.incompatibilityReason(200, """{"data":{}}""", 17))
        // Compatible envelope: matching major, no min build.
        assertNull(
            ApiMetaGate.incompatibilityReason(
                200, """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR}}}""", 17,
            ),
        )
    }

    @Test
    fun api_major_mismatch_blocks() {
        val wrong = ApiV4Contract.API_MAJOR + 1
        val reason = ApiMetaGate.incompatibilityReason(
            200, """{"data":{"apiMajor":$wrong,"minAndroidBuild":1}}""", 17,
        )
        assertTrue("must refuse a wrong API major: $reason", reason != null)
        assertTrue(reason!!.contains("API v$wrong"))
        assertTrue(reason.contains("update the app"))
    }

    @Test
    fun min_android_build_above_this_build_blocks() {
        val reason = ApiMetaGate.incompatibilityReason(
            200,
            """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":18}}""",
            17,
        )
        assertTrue("must refuse when the server floors above us: $reason", reason != null)
        assertTrue(reason!!.contains("18"))
        // Equal and below are fine.
        assertNull(
            ApiMetaGate.incompatibilityReason(
                200,
                """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":17}}""",
                17,
            ),
        )
        assertNull(
            ApiMetaGate.incompatibilityReason(
                200,
                """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":3}}""",
                17,
            ),
        )
    }

    @Test
    fun unknown_build_never_blocks() {
        // Int.MAX_VALUE = "build unknown" (pure-tier / unversioned runs).
        assertNull(
            ApiMetaGate.incompatibilityReason(
                200,
                """{"data":{"apiMajor":${ApiV4Contract.API_MAJOR},"minAndroidBuild":999}}""",
                Int.MAX_VALUE,
            ),
        )
    }
}
