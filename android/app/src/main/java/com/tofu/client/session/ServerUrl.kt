package com.tofu.client.session

import com.tofu.client.data.AuthType
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.net.URLDecoder
import java.util.Locale

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

        const val CREDENTIALS_ERROR = "Server URL contains credentials. Remove sign-in tokens and use the password field or interactive sign-in."
        const val INVALID_URL_ERROR = "Use a full http(s):// server URL without credentials."
        const val UNSAFE_LABEL = "Server URL needs editing"
        private const val MAX_STORED_URL_CHARS = 8192
        private const val MAX_DECODE_PASSES = 4
        private val HTTP_SCHEME = Regex("^https?://", RegexOption.IGNORE_CASE)
        private val SECRET_KEYS = setOf(
            "password", "passwd", "pwd", "pass", "token", "accesstoken", "refreshtoken", "idtoken",
            "authtoken", "authorization", "auth", "bearer", "apikey", "key", "secret", "clientsecret",
            "credential", "credentials", "code", "oauthcode", "ticket", "signature", "sig",
            "session", "sessionid", "sessiontoken", "cookie", "cookies", "codeserversession", "ssotoken",
            "jwt", "samlresponse", "csrf", "authcode", "authorizationcode", "accesskey", "privatekey",
        )
        private val SECRET_SUFFIXES = listOf(
            "token", "secret", "password", "passwd", "credential", "credentials", "signature",
            "apikey", "session", "sessionid", "cookie", "cookies",
        )
        private val PARAMETER = Regex("(?:^|(?<=[?&#;=]))([^=?&#;]+)=")
        private val JSON_FIELD = Regex("\"([^\"]+)\"\\s*:")
        private val EMBEDDED_USERINFO = Regex("(?:https?:/*|//+)[^/?#]*@", RegexOption.IGNORE_CASE)
        private val UNESCAPED_PERCENT = Regex("%(?![0-9a-fA-F]{2})")

        /**
         * Runtime gateway URLs may carry transient SSO parameters. This stricter
         * admission boundary applies to profiles, prefills and native probes.
         * Failure copy is constant; it never echoes the offending address.
         */
        fun storageError(raw: String): String? {
            val value = raw.trim()
            if (value.length > MAX_STORED_URL_CHARS || !HTTP_SCHEME.containsMatchIn(value)) return INVALID_URL_ERROR
            if (hasUrlControls(value)) return INVALID_URL_ERROR
            val url = value.toHttpUrlOrNull() ?: return INVALID_URL_ERROR
            if (url.username.isNotEmpty() || url.password.isNotEmpty() ||
                hasCredentialParameters(url.encodedPath) || hasCredentialParameters(url.encodedQuery) ||
                hasCredentialParameters(url.encodedFragment)) return CREDENTIALS_ERROR
            return null
        }

        /** Offer a legacy address for explicit review/save; never mutate Room on read or activation. */
        fun sanitizeForStorage(raw: String): String? {
            if (storageError(raw) == null) return raw.trim()
            if (raw.length > MAX_STORED_URL_CHARS) return null
            val url = raw.trim().toHttpUrlOrNull() ?: return null
            fun clean(fields: String?): String? = fields?.split('&', ';')
                ?.filterNot(::hasCredentialParameters)?.joinToString("&")?.ifEmpty { null }
            val fragment = url.encodedFragment
            val cleanedFragment = if (fragment != null && '?' in fragment) {
                val route = fragment.substringBefore('?')
                if (hasCredentialParameters(route)) return null
                clean(fragment.substringAfter('?'))?.let { "$route?$it" } ?: route.ifEmpty { null }
            } else clean(fragment)
            val candidate = url.newBuilder().username("").password("")
                .encodedQuery(clean(url.encodedQuery)).encodedFragment(cleanedFragment).build().toString()
            return candidate.takeIf { storageError(it) == null }
        }

        /** Keep noncredential partial typing in memory-only recreation drafts, never in Room. */
        fun sanitizeForDraft(raw: String): String = sanitizeForStorage(raw) ?: if (
            raw.length <= MAX_STORED_URL_CHARS && !hasCredentialParameters(raw)
        ) raw else ""

        private fun hasUrlControls(value: String): Boolean = value.any { it.code <= 0x1f || it.code == 0x7f }

        private fun secretKey(value: String): Boolean {
            val key = value.lowercase(Locale.ROOT).filter { it.isLetterOrDigit() }
            return key in SECRET_KEYS || SECRET_SUFFIXES.any(key::endsWith)
        }

        private fun hasCredentialParameters(component: String?): Boolean {
            var value = component ?: return false
            repeat(MAX_DECODE_PASSES + 1) { pass ->
                // Browser special-scheme parsing treats backslashes and missing/
                // redundant authority slashes as userinfo too. Normalize for
                // detection only; transient gateway navigation parsing is unchanged.
                if (hasUrlControls(value) || EMBEDDED_USERINFO.containsMatchIn(value.replace('\\', '/')) ||
                    PARAMETER.findAll(value).any { secretKey(it.groupValues[1]) } ||
                    JSON_FIELD.findAll(value.replace("\\\"", "\"")).any { secretKey(it.groupValues[1]) }) return true
                // Literal '%' in an ordinary label is not another encoding pass.
                val decoded = URLDecoder.decode(value.replace(UNESCAPED_PERCENT, "%25"), "UTF-8")
                if (decoded == value) return false
                if (pass == MAX_DECODE_PASSES) return true // Ambiguous deeper nesting fails closed, bounded.
                value = decoded
            }
            return false
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
         * Non-MLP hosts fall back to `host:port`. Invalid input gets a constant
         * label, never the raw string (which could contain a credential).
         * Pure and deterministic — unit-tested off-device.
         */
        fun displayLabel(rawUrl: String): String {
            val u = parse(rawUrl) ?: return UNSAFE_LABEL
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
