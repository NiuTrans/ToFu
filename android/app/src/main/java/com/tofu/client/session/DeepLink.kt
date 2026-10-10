package com.tofu.client.session

import java.net.URLDecoder

/**
 * Pure parser for the three onboarding payloads the app accepts: a scanned QR
 * code, a pasted string, or an inbound `tofu://` deep link.
 *
 * Recognizes either an explicit `tofu://connect?url=…&name=…` intent or a bare
 * `http(s)://` server URL. Anything else yields null, so a scanned
 * non-Tofu QR ({Wi-Fi password, a random link}) is a clean no-op rather than a
 * half-typed profile. Pure Kotlin, so the parse rules are unit-tested.
 */
object DeepLink {

    /** A server to offer the add form: the URL, plus an optional suggested name. */
    data class Connect(val url: String, val alias: String?)

    fun parse(payload: String?): Connect? {
        val raw = payload?.trim().orEmpty()
        if (raw.isEmpty()) return null
        if (raw.startsWith(SCHEME, ignoreCase = true)) return parseConnect(raw)
        if (isHttpUrl(raw)) return Connect(raw, null)
        return null
    }

    fun isHttpUrl(value: String): Boolean =
        (value.startsWith("http://") || value.startsWith("https://")) &&
            ServerUrl.parse(value) != null

    private fun parseConnect(raw: String): Connect? {
        val afterScheme = raw.substringAfter("://", "")
        val query = afterScheme.substringAfter('?', "")
        val params = parseQuery(query)
        val url = params["url"]?.takeIf { isHttpUrl(it) } ?: return null
        return Connect(url, params["name"]?.takeIf { it.isNotBlank() })
    }

    private fun parseQuery(query: String): Map<String, String> {
        if (query.isBlank()) return emptyMap()
        val out = mutableMapOf<String, String>()
        for (pair in query.split('&')) {
            if (pair.isEmpty()) continue
            val separator = pair.indexOf('=')
            val key = if (separator >= 0) pair.substring(0, separator) else pair
            val value = if (separator >= 0) pair.substring(separator + 1) else ""
            out[decode(key)] = decode(value)
        }
        return out
    }

    @Suppress("DEPRECATION")
    private fun decode(value: String): String = try {
        URLDecoder.decode(value, "UTF-8")
    } catch (e: Exception) {
        value
    }

    private const val SCHEME = "tofu://"
}
