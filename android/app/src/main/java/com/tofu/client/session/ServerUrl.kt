package com.tofu.client.session

import com.tofu.client.data.AuthType
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull

/**
 * Parsed view of a server base URL, e.g.
 * `https://<uuid>-vscode-dc1.codelab.example.com/proxy/15000/`.
 *
 * The feasibility spike established two facts this class encodes:
 *  - the session cookie is `Domain`-pinned to the FULL host, so [host] is the
 *    identity that a cached jar is bound to;
 *  - the code-server login lives at the code-server ROOT (`…/login`), which for
 *    a `/proxy/PORT/` deploy is the origin root, NOT under the proxy subpath —
 *    the observed unauth redirect was `./../../login` climbing out of the
 *    subpath. [loginUrl] therefore posts to `<scheme>://<host>/login`.
 */
data class ServerUrl(
    val raw: String,
    val httpUrl: HttpUrl,
) {
    val host: String get() = httpUrl.host

    val scheme: String get() = httpUrl.scheme

    /** Origin root, e.g. `https://<host>`. */
    val origin: String get() = "$scheme://$host"

    /** code-server login endpoint (origin root + /login). */
    val loginUrl: String get() = "$origin/login"

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
            return ServerUrl(raw = raw, httpUrl = u)
        }

        // A code-server proxy path: `/proxy/<port>/…`. Its presence means the
        // whole origin sits behind code-server's `--auth password` gate, so the
        // app must replay the stored password (CODE_SERVER_PASSWORD). A bare
        // `host:port` URL (no proxy subpath) is a directly-exposed Tofu with no
        // such gate, so NONE (skip the handshake, let Tofu's own `open` mode /
        // the WebView handle any auth) is the right zero-config default.
        private val PROXY_PATH = Regex("""/proxy/\d+(/|$)""")

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
            if (PROXY_PATH.containsMatchIn(rawUrl.trim())) AuthType.CODE_SERVER_PASSWORD
            else AuthType.NONE

        /**
         * Predicate for the upgrade migration: is this a persisted profile
         * whose URL is a code-server proxy form (`/proxy/<port>/`) but whose
         * stored [current] auth is the stale NONE default? Such a row is a
         * pre-v0.1.13 profile stuck on the old bare-Tofu default — it can never
         * headless-login (login short-circuits NONE without sending the
         * password, dumping the user on the code-server login page and leaving
         * Start greyed forever). It must be flipped to CODE_SERVER_PASSWORD.
         *
         * Bare-host NONE rows return false (left untouched). NOTE: an
         * explicitly user-chosen NONE on a proxy URL is INDISTINGUISHABLE from
         * the stale default in the schema (both are just `authType=NONE`), so
         * it is also flipped — acceptable because a deliberate NONE on a
         * password-gated proxy is a non-functional config anyway.
         *
         * Pure and idempotent (after the flip, current != NONE → false).
         */
        fun needsProxyAuthFix(rawUrl: String, current: AuthType): Boolean =
            current == AuthType.NONE &&
                defaultAuthType(rawUrl) == AuthType.CODE_SERVER_PASSWORD


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
