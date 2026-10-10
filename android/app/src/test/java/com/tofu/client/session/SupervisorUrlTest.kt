package com.tofu.client.session

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Pure JVM tests for the supervisor URL derivation (no device needed). */
class SupervisorUrlTest {

    @Test
    fun derives_supervisor_base_from_proxy_url() {
        val su = SupervisorUrl.fromServerUrl(
            "https://abc12345-vscode-dc1.codelab.example.com/proxy/15000/",
        )!!
        assertEquals(
            "https://abc12345-vscode-dc1.codelab.example.com/proxy/15001", su.base,
        )
        assertEquals(
            "https://abc12345-vscode-dc1.codelab.example.com", su.origin,
        )
    }

    @Test
    fun derives_from_bare_host() {
        val su = SupervisorUrl.fromServerUrl("http://localhost:15000/")!!
        assertEquals("http://localhost:15000/proxy/15001", su.base)
        assertEquals("http://localhost:15000", su.origin)
    }

    @Test
    fun derives_sibling_of_non_default_tofu_port() {
        // A deployment not on 15000 must NOT be hardcoded to 15001: the
        // supervisor rides one port up from whatever the URL itself names.
        // NEUTER CHECK: restore the hardcoded `$SUPERVISOR_PORT` in
        // fromServerUrl and this fails (base would end /15001, not /15006).
        val su = SupervisorUrl.fromServerUrl(
            "https://abc12345-vscode-dc1.codelab.example.com/proxy/15005/",
        )!!
        assertEquals(
            "https://abc12345-vscode-dc1.codelab.example.com/proxy/15006", su.base,
        )
        // A trailing path after the port segment doesn't confuse the parse.
        val nested = SupervisorUrl.fromServerUrl("https://h.example/proxy/18080/tofu/")!!
        assertEquals("https://h.example/proxy/18081", nested.base)
    }

    @Test
    fun falls_back_to_default_when_no_proxy_segment() {
        // No `/proxy/<port>` in the path → the conventional default sibling.
        val su = SupervisorUrl.fromServerUrl("https://h.example/tofu/")!!
        assertEquals("https://h.example/proxy/${SupervisorUrl.SUPERVISOR_PORT}", su.base)
    }

    @Test
    fun invalid_url_returns_null() {
        assertNull(SupervisorUrl.fromServerUrl("not a url"))
        assertNull(SupervisorUrl.fromServerUrl(""))
    }

    @Test
    fun status_endpoint_encodes_project_path_query() {
        val su = SupervisorUrl("https://h.example/proxy/15001", "https://h.example")
        val url = SupervisorUrl.endpoint(su, SupervisorUrl.STATUS, "/home/dev/chatui")
        assertEquals(
            "https://h.example/proxy/15001/status?projectPath=%2Fhome%2Fdev%2Fchatui", url,
        )
    }

    @Test
    fun post_endpoint_has_no_query() {
        val su = SupervisorUrl("https://h.example/proxy/15001", "https://h.example")
        assertEquals(
            "https://h.example/proxy/15001/start",
            SupervisorUrl.endpoint(su, SupervisorUrl.START, null),
        )
        assertEquals(
            "https://h.example/proxy/15001/stop",
            SupervisorUrl.endpoint(su, SupervisorUrl.STOP, null),
        )
    }

    // ── explainFailure: opaque HTTP status → actionable guidance ──────────

    @Test
    fun explain_5xx_points_at_supervisor_not_running() {
        // The reported "HTTP 500 on Start" case: a 5xx is the code-server proxy
        // saying nothing listens on /proxy/15001 — i.e. supervisor.py is down.
        // NEUTER CHECK: make the `code in 500..599` branch fall through to
        // `else -> rawMessage` and this fails — the user would see a bare
        // "HTTP 500" with no remedy instead of the install instruction.
        val msg = SupervisorUrl.explainFailure(500, "HTTP 500")
        assertTrue("must name the daemon: $msg", msg.contains("supervisor.py"))
        assertTrue("must give the fix: $msg", msg.contains("supervisor.sh install"))
        assertTrue("must cite the port: $msg", msg.contains(SupervisorUrl.SUPERVISOR_PORT))
        // A different 5xx (502/503 from the proxy) maps the same way.
        assertTrue(SupervisorUrl.explainFailure(503, "x").contains("supervisor.py"))
    }

    @Test
    fun explain_403_points_at_allowlist() {
        val msg = SupervisorUrl.explainFailure(403, "projectPath not in allow-list")
        assertTrue(msg.contains("TOFU_SUPERVISOR_PROJECTS"))
    }

    @Test
    fun explain_404_and_401_are_specific() {
        assertTrue(SupervisorUrl.explainFailure(404, "x").contains("older version"))
        // 401 must NOT claim "expired" (the user may never have logged in) and
        // must steer them to Open — the fix for the reported misleading copy.
        val msg401 = SupervisorUrl.explainFailure(401, "x")
        assertTrue("401 must point at Open: $msg401", msg401.contains("Open"))
        assertTrue("401 must not say expired: $msg401", !msg401.contains("expired"))
    }

    @Test
    fun explain_network_error_uses_raw_message() {
        // code==0 is our sentinel for a transport failure (no HTTP response).
        val msg = SupervisorUrl.explainFailure(0, "timeout")
        assertTrue("must surface the cause: $msg", msg.contains("timeout"))
    }

    @Test
    fun explain_unknown_code_passes_through_raw() {
        assertEquals("weird", SupervisorUrl.explainFailure(418, "weird"))
    }
}
