package com.tofu.client.session

import com.tofu.client.data.AuthType
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the Tofu↔gateway 401 discrimination (mirror of the desktop agent's
 * lib/desktop_agent/_probe.py contract).
 *
 * The bug class this guards: behind a VS Code port-forwarding proxy, the edge
 * 401s EVERY unauthenticated request — so a client that treats "401" as
 * "wrong URL" (or "200" as "reachable") silently misleads the user about
 * which side is broken.
 */
class TofuProbeTest {

    // ── isTofuErrorEnvelope ───────────────────────────────────────────────

    @Test
    fun `tofu envelope has ok false and an error OBJECT`() {
        assertTrue(
            TofuProbe.isTofuErrorEnvelope(
                """{"ok":false,"error":{"code":"unauthorized","message":"bad key"}}""",
            ),
        )
        // Whitespace-tolerant.
        assertTrue(TofuProbe.isTofuErrorEnvelope("""{ "ok" : false, "error" : { } }"""))
    }

    @Test
    fun `gateway string error is NOT the tofu envelope`() {
        // The measured SSO edge shape — error as a STRING.
        assertFalse(TofuProbe.isTofuErrorEnvelope("""{"error":"Unauthorized"}"""))
        assertFalse(TofuProbe.isTofuErrorEnvelope(null))
        assertFalse(TofuProbe.isTofuErrorEnvelope("not json"))
        assertFalse(TofuProbe.isTofuErrorEnvelope("""{"ok":true,"error":{"x":1}}"""))
    }

    // ── hasBootId ─────────────────────────────────────────────────────────

    @Test
    fun `bootId is the positive tofu signal`() {
        assertTrue(TofuProbe.hasBootId("""{"ok":true,"bootId":"b123","pid":42}"""))
        assertFalse(TofuProbe.hasBootId("""{"ok":true}"""))
        assertFalse(TofuProbe.hasBootId("<html>gateway landing</html>"))
        assertFalse(TofuProbe.hasBootId(null))
    }

    // ── classify ──────────────────────────────────────────────────────────

    @Test
    fun `200 with bootId is tofu`() {
        assertEquals(
            TofuProbe.Verdict.TOFU,
            TofuProbe.classify(200, """{"ok":true,"bootId":"x"}"""),
        )
    }

    @Test
    fun `200 without bootId is a masquerading landing page`() {
        assertEquals(TofuProbe.Verdict.NOT_TOFU, TofuProbe.classify(200, "<html></html>"))
        assertEquals(TofuProbe.Verdict.NOT_TOFU, TofuProbe.classify(200, """{"ok":true}"""))
    }

    @Test
    fun `401 splits on the envelope — the core discrimination`() {
        // NEUTER CHECK: drop isTofuErrorEnvelope from classify (always GATEWAY)
        // and the TOFU_AUTH case flips — the app would blame the URL when the
        // server itself is answering.
        assertEquals(
            TofuProbe.Verdict.TOFU_AUTH,
            TofuProbe.classify(401, """{"ok":false,"error":{"code":"unauthorized"}}"""),
        )
        assertEquals(
            TofuProbe.Verdict.GATEWAY,
            TofuProbe.classify(401, """{"error":"Unauthorized"}"""),
        )
        assertEquals(TofuProbe.Verdict.GATEWAY, TofuProbe.classify(403, ""))
    }

    @Test
    fun `transport and non-edge 5xx are unreachable`() {
        assertEquals(TofuProbe.Verdict.UNREACHABLE, TofuProbe.classify(0, null))
        // 500 means SOMETHING answered and failed — not a cold proxy edge.
        assertEquals(TofuProbe.Verdict.UNREACHABLE, TofuProbe.classify(500, ""))
        assertEquals(TofuProbe.Verdict.NOT_TOFU, TofuProbe.classify(418, "teapot"))
    }

    @Test
    fun `502 503 504 are a waking sandbox, not a wrong address`() {
        // Behind the vscode proxy these statuses mean "nothing listens yet".
        // NEUTER: map them to UNREACHABLE and the guidance tells the user to
        // "check the network and the address" — chasing a non-existent typo
        // while the sandbox simply boots.
        assertEquals(TofuProbe.Verdict.WAKING, TofuProbe.classify(502, ""))
        assertEquals(TofuProbe.Verdict.WAKING, TofuProbe.classify(503, "Bad Gateway"))
        assertEquals(TofuProbe.Verdict.WAKING, TofuProbe.classify(504, null))
        assertTrue(TofuProbe.isProblem(TofuProbe.Verdict.WAKING, AuthType.NONE, false))
        assertTrue(
            TofuProbe.guidance(TofuProbe.Verdict.WAKING, AuthType.NONE, false)
                .contains("waking"),
        )
    }

    // ── isProblem / guidance: honest per-auth reading ─────────────────────

    @Test
    fun `gateway 401 is expected only when a password will answer it`() {
        // With a saved password, a /proxy/ URL's gate is the NORMAL path.
        assertFalse(
            TofuProbe.isProblem(
                TofuProbe.Verdict.GATEWAY, AuthType.CODE_SERVER_PASSWORD, hasSecret = true,
            ),
        )
        // Without one it is a dead end the user must fix NOW, not at open time.
        assertTrue(
            TofuProbe.isProblem(
                TofuProbe.Verdict.GATEWAY, AuthType.CODE_SERVER_PASSWORD, hasSecret = false,
            ),
        )
        // NONE auth on a gated URL can never work.
        assertTrue(TofuProbe.isProblem(TofuProbe.Verdict.GATEWAY, AuthType.NONE, false))
        // SSO completes interactively — not a blocker.
        assertFalse(TofuProbe.isProblem(TofuProbe.Verdict.GATEWAY, AuthType.INTERACTIVE_SSO, false))
        // Reaching Tofu is never a problem.
        assertFalse(TofuProbe.isProblem(TofuProbe.Verdict.TOFU, AuthType.NONE, false))
        assertTrue(TofuProbe.isProblem(TofuProbe.Verdict.NOT_TOFU, AuthType.NONE, false))
        assertTrue(TofuProbe.isProblem(TofuProbe.Verdict.UNREACHABLE, AuthType.NONE, false))
    }

    @Test
    fun `guidance names the actual side at fault`() {
        assertTrue(
            TofuProbe.guidance(TofuProbe.Verdict.GATEWAY, AuthType.NONE, false)
                .contains("switch the auth mode"),
        )
        assertTrue(
            TofuProbe.guidance(TofuProbe.Verdict.GATEWAY, AuthType.CODE_SERVER_PASSWORD, false)
                .contains("enter the password"),
        )
        assertTrue(
            TofuProbe.guidance(TofuProbe.Verdict.GATEWAY, AuthType.CODE_SERVER_PASSWORD, true)
                .contains("expected"),
        )
        assertTrue(
            TofuProbe.guidance(TofuProbe.Verdict.NOT_TOFU, AuthType.NONE, false)
                .contains("isn't Tofu"),
        )
        // Every verdict has non-empty guidance.
        TofuProbe.Verdict.values().forEach { v ->
            AuthType.values().forEach { a ->
                assertTrue(
                    "empty guidance for $v/$a",
                    TofuProbe.guidance(v, a, hasSecret = false).isNotBlank(),
                )
            }
        }
    }
}
