package com.tofu.client.selfcheck

import com.tofu.client.runtime.KernelSecretValue
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * No kernel secret, token, cookie, account name, Wi-Fi name or file content
 * may reach a report. Sensitive values are planted in every free-text input a
 * probe can produce; the assertions run over the serialized JSON.
 */
class SelfCheckRedactionTest {
    private val secret = KernelSecretValue(KernelSecretValue.PREFIX + "0123456789abcdef".repeat(4))
    private val redactor = SelfCheckRedactor(secret)
    private val secretText = KernelSecretValue.PREFIX + "0123456789abcdef".repeat(4)

    private fun report() = SelfCheckReport(
        reportId = "redact1",
        status = RunStatus.FINISHED,
        startedAtWallMs = 0L,
        presetMinutes = 5,
        facts = deviceFacts().copy(kernelRelease = "5.15 $secretText"),
        exec = ExecSection(
            status = StepStatus.DONE,
            prootCommand = listOf("/data/user/0/com.tofu.client/lib/libproot.so"),
            checks = listOf(
                ExecResult(
                    id = ExecChecks.PROOT_SH,
                    argv = listOf("/bin/sh", "-c", "echo $secretText"),
                    expectation = "token=ghp_1234567890abcdef should never appear",
                    pass = false,
                    outcome = ExecOutcome.EXITED,
                    exitStatus = 1,
                    stdout = "tofu_session=$secretText; Authorization: Bearer abcd1234efgh\nssid=MyHomeWifi",
                    stderr = "password=hunter2 account=alice@example.com\n$secretText",
                    error = "cookie: session=xyz123",
                ),
            ),
        ),
        kernel = KernelSection(
            status = StepStatus.DONE,
            outcome = KernelCheck.FAILED,
            failure = "ProtocolViolation(unexpected stdout record: {\"secret\":\"$secretText\"})",
            probes = listOf(HttpResult("/api/v4/meta", false, 200, 200, 3, null, serverBuild = "0.16.0")),
        ),
        exitReasons = listOf(ExitReasonRecord(0L, 1, "com.tofu.client", 9, 0, 400, 0, 0, "Caused by child process: token=abcd1234")),
    )

    @Test
    fun no_secret_token_cookie_or_account_reaches_the_json() {
        val json = ReportJson.of(report()).render(redactor)
        for (needle in listOf(secretText, "ghp_1234567890abcdef", "abcd1234efgh", "hunter2", "xyz123", "abcd1234")) {
            assertFalse("leaked $needle", json.contains(needle))
        }
        // A partial (clipped) secret is masked too.
        assertFalse(json.contains(KernelSecretValue.PREFIX))
    }

    @Test
    fun scrub_is_idempotent_and_bounds_excerpts() {
        val once = redactor.scrub("Bearer abcdef123456 and $secretText")
        assertFalse(once.contains("abcdef123456"))
        assertFalse(once.contains(secretText))
        assertTrue(once.length < 200)
        val long = "x".repeat(10_000)
        assertTrue(redactor.scrub(long).length <= 2001)
    }

    @Test
    fun the_report_model_has_no_secret_or_network_identity_fields() {
        val json = ReportJson.of(report()).render(redactor)
        for (key in listOf("\"secret\"", "\"cookie\"", "\"authorization\"", "\"ssid\"", "\"bssid\"", "\"account\"", "\"password\"", "\"body\"", "\"headers\"", "\"wifi\"")) {
            assertFalse("report carries $key", json.contains(key))
        }
    }

    @Test
    fun http_probes_keep_status_not_bodies() {
        val result = HttpResult("/api/v4/swarm/config", credential = true, expectedStatus = 200, status = 200, durationMs = 4, error = null)
        val json = ReportJson.of(SelfCheckReport("id", RunStatus.FINISHED, 0L, presetMinutes = 1, kernel = KernelSection(probes = listOf(result)))).render(redactor)
        assertTrue(json.contains("\"credential\": true"))
        assertFalse(json.contains("roles"))
        assertFalse(json.contains("available"))
    }

    @Test
    fun crash_and_anr_descriptions_are_dropped_at_the_source() {
        // systemDescription is Android-side; here the record simply carries none.
        val record = ExitReasonRecord(0L, 1, "com.tofu.client", 4, 0, 100, 0, 0, null)
        val json = ReportJson.of(SelfCheckReport("id", RunStatus.FINISHED, 0L, presetMinutes = 1, exitReasons = listOf(record))).render(redactor)
        assertTrue(json.contains("\"description\": null"))
    }

    @Test
    fun interrupted_run_keeps_only_the_already_redacted_previous_report() {
        val inner = ReportJson.of(report()).render(redactor)
        val wrapped = ReportJson.interrupted("id2", 10L, null, null, emptyList(), inner).render(redactor)
        assertFalse(wrapped.contains(secretText))
        assertTrue(wrapped.contains("\"status\": \"interrupted\""))
    }
}
