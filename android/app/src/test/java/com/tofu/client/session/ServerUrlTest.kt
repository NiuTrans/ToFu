package com.tofu.client.session

import com.tofu.client.data.AuthType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the two ServerUrl invariants that silently break "authenticate once":
 *  - loginUrl is the ORIGIN-ROOT /login (not under /proxy/PORT/) — matches the
 *    observed unauth redirect `./../../login` climbing out of the subpath;
 *  - instanceUuid is extracted from a `<uuid>-vscode-<idc>` host so we can detect
 *    "same logical server, new URL" on re-provision.
 */
class ServerUrlTest {

    private val sandbox =
        "https://00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com/proxy/15000/"

    @Test
    fun loginUrl_is_origin_root_not_under_proxy_subpath() {
        val s = ServerUrl.parse(sandbox)!!
        // NEUTER CHECK: if loginUrl were built as origin + httpUrl.encodedPath + "login"
        // it would contain "/proxy/15000/login" and this assertion would fail.
        assertEquals(
            "https://00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com/login",
            s.loginUrl,
        )
        assert(!s.loginUrl.contains("/proxy/")) { "login must NOT be under the proxy subpath" }
    }

    @Test
    fun origin_and_host_are_the_full_uuid_host() {
        val s = ServerUrl.parse(sandbox)!!
        assertEquals(
            "00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com",
            s.host,
        )
        assertEquals(
            "https://00000000-0000-4000-8000-000000000001-vscode-dc1.codelab.example.com",
            s.origin,
        )
    }

    @Test
    fun instanceUuid_extracted_from_mlp_host() {
        val s = ServerUrl.parse(sandbox)!!
        assertEquals("00000000-0000-4000-8000-000000000001", s.instanceUuid)
    }

    @Test
    fun instanceUuid_null_for_non_mlp_host() {
        val s = ServerUrl.parse("https://tofu.example.com/proxy/15000/")!!
        assertNull(s.instanceUuid)
    }

    @Test
    fun parse_rejects_non_absolute_url() {
        assertNull(ServerUrl.parse("not a url"))
        assertNull(ServerUrl.parse("ftp://host/x"))
    }

    // ── defaultAuthType: URL-aware zero-config auth default ───────────────

    @Test
    fun defaultAuthType_proxy_url_is_code_server_password() {
        // A `/proxy/<port>/` URL sits behind the code-server password gate, so
        // the app must replay the stored password rather than short-circuit.
        // NEUTER CHECK: make defaultAuthType always return AuthType.NONE and
        // this fails — reproducing the reported bug where a bare-NONE default
        // dumped the user on the code-server login page instead of auto-login.
        assertEquals(AuthType.CODE_SERVER_PASSWORD, ServerUrl.defaultAuthType(sandbox))
        assertEquals(
            AuthType.CODE_SERVER_PASSWORD,
            ServerUrl.defaultAuthType("https://tofu.example.com/proxy/15000/"),
        )
        // Trailing-slash-less proxy path still counts.
        assertEquals(
            AuthType.CODE_SERVER_PASSWORD,
            ServerUrl.defaultAuthType("https://h.example.com/proxy/8080"),
        )
    }

    @Test
    fun defaultAuthType_bare_host_is_none() {
        // Directly-exposed Tofu (no proxy subpath) → NONE, so bare single-user
        // deployments stay zero-config and skip the handshake.
        assertEquals(AuthType.NONE, ServerUrl.defaultAuthType("https://tofu.example.com/"))
        assertEquals(AuthType.NONE, ServerUrl.defaultAuthType("http://192.168.1.9:15000/"))
        assertEquals(AuthType.NONE, ServerUrl.defaultAuthType(""))
        // A non-numeric "proxy" segment is NOT the code-server shape.
        assertEquals(AuthType.NONE, ServerUrl.defaultAuthType("https://h.example.com/proxy/api/"))
    }

    // ── displayLabel: compress the 90-char sandbox URL for list rows ───────

    @Test
    fun displayLabel_compresses_mlp_sandbox_to_uuid_idc_port() {
        // NEUTER CHECK: return the raw URL and this fails — reproducing the
        // unreadable list where every row truncated to the same "https://abc12…".
        assertEquals("00000000 · dc1 : 15000", ServerUrl.displayLabel(sandbox))
    }

    @Test
    fun displayLabel_non_mlp_host_shows_host_and_proxy_port() {
        assertEquals(
            "tofu.example.com : 15000",
            ServerUrl.displayLabel("https://tofu.example.com/proxy/15000/"),
        )
    }

    @Test
    fun displayLabel_bare_host_keeps_explicit_port() {
        assertEquals(
            "192.168.1.9:15000",
            ServerUrl.displayLabel("http://192.168.1.9:15000/"),
        )
        // A default port is not noise worth showing.
        assertEquals("tofu.example.com", ServerUrl.displayLabel("https://tofu.example.com/"))
    }

    @Test
    fun displayLabel_returns_unparseable_input_as_typed() {
        // A half-typed URL must still render something, not vanish.
        assertEquals("https://", ServerUrl.displayLabel("  https://  "))
    }



    @Test fun port_ipv6_prefix_and_absproxy_are_retained() {
        val server = ServerUrl.parse("https://[::1]:8443/dev/absproxy/15000/?x=1#part")!!
        assertEquals("https://[::1]:8443", server.origin)
        assertEquals("https://[::1]:8443/dev/login", server.loginUrl)
        assertEquals("https://[::1]:8443/dev/absproxy/15000/api/health", server.endpoint("api/health"))
        assertEquals(AuthType.CODE_SERVER_PASSWORD, ServerUrl.defaultAuthType(server.raw))
        assertEquals(AuthType.NONE, ServerUrl.defaultAuthType("https://host.example/?next=/proxy/15000/"))
        org.junit.Assert.assertNull(ServerUrl.parse("https://user:password@host.example/"))
    }
}
