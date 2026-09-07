package com.tofu.client.session

import com.tofu.client.api.ApiV4Contract

/**
 * The GET /api/v4/meta preflight, as pure decisions.
 *
 * After a headless login establishes the gateway session, the app asks the
 * server which API major and minimum Android build it requires, and refuses
 * to enter the WebView on a DEFINITIVE mismatch — a stale app talking to a
 * newer server otherwise fails later inside the SPA as unexplained 426/404s.
 *
 * The posture is fail-OPEN on partial knowledge: a 404 (a server that
 * predates the meta endpoint), a transport failure, or an unparseable body
 * never blocks — the WebView load surfaces real problems on its own. Only a
 * 200 carrying a contradicting apiMajor, or a minAndroidBuild above this
 * build, blocks.
 *
 * org.json is not on the pure-JVM test classpath, so — exactly like
 * [TofuProbe] — fields are read with anchored regexes; the meta payload is
 * flat and backend-owned (contracts/api_v4.yaml).
 */
object ApiMetaGate {

    /**
     * Absolute meta URL under the profile's base path. Resolving the absolute
     * [ApiV4Contract.META_PATH] against the origin would DROP the
     * `/proxy/<port>/` prefix of a vscode code-server deploy, so the path is
     * appended to the base as typed instead.
     */
    fun metaUrl(baseUrl: String): String =
        baseUrl.trimEnd('/') + "/" + ApiV4Contract.META_PATH.trimStart('/')

    /**
     * null = compatible or unknown (never blocks on partial knowledge).
     * Non-null = the user-facing reason this build refuses to proceed.
     */
    fun incompatibilityReason(status: Int, body: String?, appVersionCode: Int): String? {
        if (status != 200 || body.isNullOrBlank()) return null
        val apiMajor = intField(body, "apiMajor") ?: return null
        if (apiMajor != ApiV4Contract.API_MAJOR) {
            return "server speaks API v$apiMajor but this app requires " +
                "v${ApiV4Contract.API_MAJOR} — update the app"
        }
        val minAndroid = intField(body, "minAndroidBuild") ?: return null
        // Int.MAX_VALUE = "build unknown" (pure-tier tests): never blocks.
        if (appVersionCode != Int.MAX_VALUE && minAndroid > appVersionCode) {
            return "server requires app build $minAndroid or newer " +
                "(this is $appVersionCode) — update the app"
        }
        return null
    }

    private fun intField(body: String, field: String): Int? =
        Regex("\"$field\"\\s*:\\s*(\\d+)")
            .find(body)?.groupValues?.getOrNull(1)?.toIntOrNull()
}
