package com.tofu.client.session

import java.net.URI
import java.net.URLDecoder
import java.util.Locale

/**
 * Android-free admission for QR, paste and inbound tofu://connect payloads.
 * Only credential-free server URLs become review-form prefills; invalid or
 * ambiguous input is never echoed in diagnostics or saved automatically.
 */
object DeepLink {
    const val INVALID_LINK_MESSAGE = "Use a server URL or tofu://connect link without sign-in credentials. Remove tokens; use the password field or interactive sign-in."
    private const val MAX_CONNECT_CHARS = 16_384
    data class Connect(val url: String, val alias: String?)

    fun parse(payload: String?): Connect? {
        val raw = payload?.trim().orEmpty()
        if (raw.isEmpty() || raw.length > MAX_CONNECT_CHARS) return null
        if (raw.startsWith("tofu://", ignoreCase = true)) return parseConnect(raw)
        return if (isHttpUrl(raw)) Connect(raw, null) else null
    }

    fun isHttpUrl(value: String): Boolean =
        (value.startsWith("http://", ignoreCase = true) || value.startsWith("https://", ignoreCase = true)) &&
            ServerUrl.storageError(value) == null

    private fun parseConnect(raw: String): Connect? {
        val uri = try { URI(raw) } catch (_: Exception) { return null }
        if (!uri.host.equals("connect", ignoreCase = true) || uri.rawUserInfo != null || uri.port != -1 ||
            uri.rawFragment != null || uri.rawPath.orEmpty() !in setOf("", "/")) return null
        val params = parseQuery(uri.rawQuery ?: return null) ?: return null
        val url = params["url"]?.takeIf(::isHttpUrl) ?: return null
        return Connect(url, params["name"]?.takeIf { it.isNotBlank() })
    }

    private fun parseQuery(query: String): Map<String, String>? {
        val out = mutableMapOf<String, String>()
        for (pair in query.split('&')) {
            if (pair.isEmpty()) continue
            val key = decode(pair.substringBefore('='))?.lowercase(Locale.ROOT) ?: return null
            if (key !in setOf("url", "name") || key in out) return null
            out[key] = decode(pair.substringAfter('=', "")) ?: return null
        }
        return out
    }

    private fun decode(value: String): String? = try {
        URLDecoder.decode(value, "UTF-8")
    } catch (_: IllegalArgumentException) { null }
}
