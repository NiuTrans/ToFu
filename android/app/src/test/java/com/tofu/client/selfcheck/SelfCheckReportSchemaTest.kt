package com.tofu.client.selfcheck

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** The report's versioned schema: the stable top-level shape and the fields each step owns. */
class SelfCheckReportSchemaTest {
    private val redactor = SelfCheckRedactor()

    @Test
    fun schema_version_and_top_level_keys() {
        val json = ReportJson.of(SelfCheckReport("abc123", RunStatus.RUNNING, 1_700_000_000_000L, presetMinutes = 5)).render(redactor)
        assertTrue(json.contains("\"selfcheckVersion\": 1"))
        for (key in listOf("reportId", "status", "startedAt", "finishedAt", "presetMinutes", "summary", "facts", "exec", "kernel", "stress", "exitReasons")) {
            assertTrue("missing $key", json.contains("\"$key\":"))
        }
        assertTrue(json.endsWith("}\n"))
    }

    @Test
    fun device_facts_cover_the_required_measurements() {
        val json = ReportJson.of(SelfCheckReport("abc", RunStatus.FINISHED, 0L, presetMinutes = 5, facts = deviceFacts())).render(redactor)
        for (key in listOf(
            "manufacturer", "model", "device", "release", "sdkInt", "securityPatch", "pageSizeBytes", "supportedAbis",
            "kernelRelease", "versionName", "targetSdk", "freeStorageBytes", "totalRamBytes", "ignoringBatteryOptimizations",
            "standbyBucket", "postNotificationsGranted", "rootfsVersion", "payloadVersion", "nativeLibraries", "sha256",
            "monitorFlagGlobal", "maxPhantomProcesses", "monitorFlagProperty",
        )) {
            assertTrue("missing fact $key", json.contains("\"$key\""))
        }
        assertTrue(json.contains("\"readable\": false"))
        assertTrue(json.contains("\"pageSizeBytes\": 4096"))
    }

    @Test
    fun exec_checks_record_status_stderr_and_duration() {
        val check = ExecResult(
            id = ExecChecks.PROOT_SH,
            argv = listOf("/bin/sh", "-c", "echo ok"),
            expectation = "exit 0 and stdout ok",
            pass = true,
            outcome = ExecOutcome.EXITED,
            exitStatus = 0,
            durationMs = 42,
            stdout = "ok",
            stderr = "warning: something",
        )
        val json = ReportJson.of(report(exec = ExecSection(status = StepStatus.DONE, prootCommand = listOf("/data/lib/libproot.so"), checks = listOf(check)))).render(redactor)
        for (fragment in listOf("\"exitStatus\": 0", "\"durationMs\": 42", "\"stderr\": \"warning: something\"", "\"pass\": true", "\"outcome\": \"exited\"")) {
            assertTrue(fragment, json.contains(fragment))
        }
    }

    @Test
    fun stress_records_death_timeline_and_cleanup() {
        val phase = StressPhase(
            phase = StressPhase.FOREGROUND,
            status = StepStatus.DONE,
            endReason = ChildStress.COMPLETED,
            processes = listOf(
                StressProcess(kind = "proot", pid = 9000, spawnedAtMs = 0, diedAtMs = 3_000, signal = 9, deathSource = "proot-wait"),
                StressProcess(kind = "sleeper", pid = 9002, spawnedAtMs = 12, diedAtMs = 3_100, deathSource = StressPhase.CLEANUP),
                StressProcess(kind = "busy", pid = 9034, spawnedAtMs = 40),
            ),
            cleanup = CleanupResult(signalled = 2, leftovers = 0, durationMs = 120),
        )
        val section = StressSection(status = StepStatus.DONE, plan = ChildStress.plan(5), phases = listOf(phase))
        val json = ReportJson.of(report(stress = section)).render(redactor)
        assertTrue(json.contains("\"phantomProcessCap\": 32"))
        assertTrue(json.contains("\"signalName\": \"SIGKILL\""))
        assertTrue(json.contains("\"deathSource\": \"cleanup\""))
        assertTrue(json.contains("\"leftovers\": 0"))
        assertTrue(json.contains("\"deaths\": 1"))
        assertTrue(json.contains("\"survivors\": 2"))
        // A cleanup death is not a system kill.
        assertEquals(1, phase.deaths.size)
        assertEquals(2, phase.survivors)
    }

    @Test
    fun exit_reason_names_come_from_aosp_constants() {
        val record = ExitReasonRecord(1_700_000_000_000L, 1234, "com.tofu.client", 9, 0, 400, 0, 0, "excessive cpu 8000 during 5000")
        val json = ReportJson.of(report(exitReasons = listOf(record))).render(redactor)
        assertTrue(json.contains("\"reasonName\": \"excessive_resource_usage\""))
        assertTrue(json.contains("\"importanceName\": \"cached\""))
    }

    @Test
    fun summary_maps_onto_the_s1_questions() {
        val section = ExecSection(
            status = StepStatus.DONE,
            checks = listOf(
                ExecResult(ExecChecks.PROOT_TRUE, listOf("/bin/true"), "exit 0", pass = true, outcome = ExecOutcome.EXITED, exitStatus = 0),
                ExecResult(ExecChecks.DIRECT_EXEC, listOf("/x/true"), "denied", pass = true, outcome = ExecOutcome.START_FAILED, classification = ExecChecks.DENIED),
            ),
        )
        val json = ReportJson.of(report(facts = deviceFacts(), exec = section)).render(redactor)
        assertTrue(json.contains("\"execUnderProot\": \"pass\""))
        assertTrue(json.contains("\"directExecDenied\": true"))
    }

    @Test
    fun json_escapes_and_raw_embedding() {
        val inner = ReportJson.of(report()).render(redactor)
        val wrapped = ReportJson.interrupted("id2", 10L, 5L, 1L, emptyList(), inner).render(redactor)
        assertTrue(wrapped.contains("\"interruptedRun\": {"))
        assertTrue(wrapped.contains("\"selfcheckVersion\": 1"))
        assertFalse(wrapped.contains("\\\"selfcheckVersion\\\""))
        val quoted = obj("s" to "a\"b\\c\nd\te").render(redactor)
        assertTrue(quoted.contains("a\\\"b\\\\c\\nd\\te"))
    }

    private fun report(
        facts: DeviceFacts? = null,
        exec: ExecSection = ExecSection(),
        stress: StressSection = StressSection(),
        exitReasons: List<ExitReasonRecord> = emptyList(),
    ) = SelfCheckReport("abc", RunStatus.FINISHED, 0L, presetMinutes = 5, facts = facts, exec = exec, stress = stress, exitReasons = exitReasons)
}
