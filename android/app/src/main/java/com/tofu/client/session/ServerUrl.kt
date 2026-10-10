package com.tofu.client.session

import com.tofu.client.data.AuthType
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * URL authority for native requests: preserves scheme, IPv6, ports and the
 * code-server proxy prefix. Entry points: parse, endpoint, sameOrigin.
 * Dependencies: OkHttp canonical URL parsing and the persisted AuthType enum.
 */
data class ServerUrl(
    val raw: String,
    val httpUrl: HttpUrl,
) {
    val host: String get() = httpUrl.host

    val scheme: String get() = httpUrl.scheme

    /** Origin root, e.g. `https://<host>`. */
    val origin: String get() = httpUrl.newBuilder()
        .encodedPath("/").query(null).fragment(null).build().toString().trimEnd('/')

    /** code-server login endpoint, retaining the reverse-proxy prefix. */
    val loginUrl: String get() = httpUrl.newBuilder()
        .encodedPath(codeServerRootPath + "login").query(null).fragment(null).build().toString()

    /** Retain any reverse-proxy prefix before /proxy or /absproxy. */
    val codeServerRootPath: String get() {
        val proxy = Regex("""/(?:absproxy|proxy)/\d+(?:/|$)""").find(httpUrl.encodedPath)
        return proxy?.let { httpUrl.encodedPath.substring(0, it.range.first) + "/" } ?: "/"
    }

    fun sameOrigin(other: HttpUrl): Boolean =
        httpUrl.scheme == other.scheme && host == other.host && httpUrl.port == other.port

    fun endpoint(relativePath: String): String = httpUrl.newBuilder()
        .encodedPath(httpUrl.encodedPath.trimEnd('/') + "/" + relativePath.trimStart('/'))
        .query(null).fragment(null).build().toString()

    /**
     * MLP codelab instance id parsed from a `<uuid>-vscode-<idc>.…` host, or
     * null for non-MLP hosts. Used to recognise "same logical server, new URL".
     */
    val instanceUuid: String?
        get() = UUID_HOST.find(host)?.groupValues?.getOrNull(1)

    companion object {
        // <uuid>-vscode-<idc>.<domain>  →  capture the uuid
        private val UUID_HOST =
            Regex("""^([0-9a-fA-F-]{8,})-vscode-[^.]+\.""")

        /** Parse, returning null if the string is not a valid absolute http(s) URL. */
        fun parse(raw: String): ServerUrl? {
            val u = raw.trim().toHttpUrlOrNull() ?: return null
            if (u.username.isNotEmpty() || u.password.isNotEmpty()) return null
            return ServerUrl(raw = raw, httpUrl = u)
        }

        // Proxy-shaped URLs select password discovery as a default, not as
        // proof of the gateway protocol. Unknown gates use browser interaction.
        private val PROXY_PATH = Regex("""/(?:absproxy|proxy)/\d+(/|$)""")

        /**
         * Pick the sensible default [AuthType] for a freshly-typed server URL,
         * so a new profile works out-of-the-box without the user touching the
         * auth picker:
         *  - code-server proxy URL (`…/proxy/15000/`) → [AuthType.CODE_SERVER_PASSWORD]
         *  - anything else (bare host, unparseable-so-far) → [AuthType.NONE]
         *
         * Pure and deterministic — unit-tested off-device.
         */
        fun defaultAuthType(rawUrl: String): AuthType =
            if (parse(rawUrl)?.let { PROXY_PATH.containsMatchIn(it.httpUrl.encodedPath) } == true) AuthType.CODE_SERVER_PASSWORD
            else AuthType.NONE

        /**
         * A short, human-scannable label for a server URL, for list rows.
         *
         * An MLP sandbox URL is ~90 characters of which the only distinguishing
         * part is the UUID prefix, so rendering the raw string made every row
         * in the list look identical (and truncate to the same "https://abc12…").
         * We compress `<uuid>-vscode-<idc>.<domain>/proxy/<port>/` to
         * `abc12345 · dc1 : 15000` — the three fields that actually differ
         * between a user's sandboxes.
         *
         * Non-MLP hosts fall back to `host:port/path`, and an unparseable
         * string is returned as typed (so a half-typed URL still shows).
         * Pure and deterministic — unit-tested off-device.
         */
        fun displayLabel(rawUrl: String): String {
            val u = parse(rawUrl) ?: return rawUrl.trim()
            val port = PROXY_PORT.find(u.httpUrl.encodedPath)?.groupValues?.getOrNull(1)
            val mlp = MLP_HOST.find(u.host)
            if (mlp != null) {
                val uuidHead = mlp.groupValues[1].take(8)
                val idc = mlp.groupValues[2]
                return buildString {
                    append(uuidHead)
                    append(" · ")
                    append(idc)
                    if (port != null) append(" : ").append(port)
                }
            }
            val hostPort = u.httpUrl.host +
                if (u.httpUrl.port != defaultPortFor(u.httpUrl.scheme)) ":${u.httpUrl.port}" else ""
            return if (port != null) "$hostPort : $port" else hostPort
        }

        private val MLP_HOST =
            Regex("""^([0-9a-fA-F-]{8,})-vscode-([^.]+)\.""")
        private val PROXY_PORT = Regex("""/proxy/(\d+)""")

        private fun defaultPortFor(scheme: String) = if (scheme == "https") 443 else 80
    }
}
