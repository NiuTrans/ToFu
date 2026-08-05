package com.tofu.client.session

import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.net.URLEncoder

/**
 * Pure (Android-free) derivation of the supervisor's base URL from a profile's
 * Tofu server URL, and endpoint building. Unit-testable without a device.
 *
 * The supervisor is proxied by the SAME code-server as Tofu, one port up
 * (Tofu 15000 → supervisor 15001). So a server URL like
 * `https://<host>/proxy/15000/` maps to `https://<host>/proxy/15001` and the
 * control endpoints hang off that (`…/proxy/15001/status`, `/start`, `/stop`).
 *
 * When the Tofu port cannot be found in the path (a non-proxy deployment), we
 * fall back to the origin root + the default supervisor port path
 * `/proxy/<TOFU_SUPERVISOR_PORT>` so the same shape still works behind a proxy.
 */
data class SupervisorUrl(
    /** e.g. `https://<host>/proxy/15001` (no trailing slash). */
    val base: String,
    /** origin root `https://<host>` — used to look up the session cookie. */
    val origin: String,
) {
    companion object {
        const val TOFU_PORT = "15000"
        const val SUPERVISOR_PORT = "15001"

        const val STATUS = "status"
        const val START = "start"
        const val STOP = "stop"

        /**
         * Derive the supervisor base from a Tofu server URL, or null if the URL
         * is not a valid absolute http(s) URL.
         */
        fun fromServerUrl(serverUrl: String): SupervisorUrl? {
            val u = serverUrl.trim().toHttpUrlOrNull() ?: return null
            val origin = "${u.scheme}://${u.host}" +
                if (u.port != HttpUrlDefaultPort(u.scheme)) ":${u.port}" else ""
            // The supervisor is proxied one port up from Tofu off the origin
            // root: `…/proxy/15000/` (or any deployment) → `…/proxy/15001`.
            return SupervisorUrl(base = "$origin/proxy/$SUPERVISOR_PORT", origin = origin)
        }

        /**
         * Build a full endpoint URL. For GET /status the projectPath is passed
         * as a query param; for POST it is sent in the body (pass null here).
         */
        fun endpoint(su: SupervisorUrl, name: String, projectPathForQuery: String?): String {
            val root = "${su.base}/$name"
            return if (projectPathForQuery != null) {
                val enc = URLEncoder.encode(projectPathForQuery, "UTF-8")
                "$root?projectPath=$enc"
            } else {
                root
            }
        }

        private fun HttpUrlDefaultPort(scheme: String): Int =
            if (scheme == "https") 443 else 80

        /**
         * Turn an opaque supervisor call failure (HTTP status + raw message)
         * into an ACTIONABLE, human-readable explanation the app can show.
         *
         * The most common failure is a 5xx from the code-server proxy: that
         * means nothing is listening on the `…/proxy/$SUPERVISOR_PORT` port,
         * i.e. the always-on `supervisor.py` daemon is NOT running on the host.
         * supervisor.py itself never emits 5xx (only 200/403/404), so a 5xx is
         * definitionally "the daemon isn't up", not a supervisor bug — and the
         * fix lives on the host, so we tell the user exactly what to run.
         *
         * Kept pure (no Android types) so it is unit-testable off-device.
         */
        fun explainFailure(code: Int, rawMessage: String): String = when {
            code in 500..599 ->
                "The start/stop daemon isn't responding (HTTP $code). This almost " +
                "always means supervisor.py (proxied on port $SUPERVISOR_PORT) is " +
                "not running on the host — a 5xx comes from the proxy when nothing " +
                "is listening there, not from the supervisor itself. On the host, " +
                "start it once with:  ./supervisor.sh install  (systemd, keeps it " +
                "always-on). Until it runs, Start/Stop can't work."
            code == 404 ->
                "Supervisor endpoint not found (HTTP 404). The daemon may be an " +
                "older version, or nothing is serving the /proxy/$SUPERVISOR_PORT " +
                "path. Check that supervisor.py is running on the host."
            code == 403 ->
                "This project path isn't allow-listed on the host. Add its absolute " +
                "path to TOFU_SUPERVISOR_PROJECTS and restart the supervisor " +
                "(./supervisor.sh install), then try again."
            code == 401 ->
                "Not signed in to this server yet — tap Open first to log in " +
                "with your saved password, then retry Start/Stop."
            code == 0 ->
                "Couldn't reach the supervisor: $rawMessage. Check the server URL " +
                "and that the supervisor daemon is running on the host."
            else -> rawMessage
        }
    }
}
