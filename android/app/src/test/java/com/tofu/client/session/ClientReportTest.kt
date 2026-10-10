package com.tofu.client.session

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the support-report wire payload: field emission, JSON escaping,
 * bounds, and — most importantly — secret redaction. A report is evidence the
 * user hands over, so a leaked token must not survive in it.
 */
class ClientReportTest {

    private val app = AppReportInfo(
        versionName = "0.1.17", versionCode = 17,
        packageName = "com.tofu.client", debug = false,
    )
    private val device = DeviceReportInfo(
        sdkInt = 34, release = "14", model = "Pixel 7",
        manufacturer = "Google", abi = "arm64-v8a",
    )
    private val profile = ProfileReportInfo(
        alias = "Shanghai sandbox",
        host = "00000000-vscode-dc1.codelab.example.com",
        authType = "CODE_SERVER_PASSWORD", managed = true, signedIn = true,
    )

    private fun build(
        note: String = "",
        events: List<DiagEvent> = emptyList(),
        web: String? = null,
    ): String = ClientReport.build(
        reportId = "abc123",
        createdAtMs = 1_700_000_000_000L,
        note = note,
        app = app,
        device = device,
        profile = profile,
        events = events,
        webDiagnostics = web,
    )

    @Test
    fun emits_identity_and_metadata() {
        val json = build()
        assertTrue(json.contains("\"schema\":\"${ClientReport.SCHEMA}\""))
        assertTrue(json.contains("\"reportId\":\"abc123\""))
        assertTrue(json.contains("\"createdAt\":\"2023-11-14T22:13:20Z\""))
        assertTrue(json.contains("\"package\":\"com.tofu.client\""))
        assertTrue(json.contains("\"sdkInt\":34"))
        assertTrue(json.contains("\"alias\":\"Shanghai sandbox\""))
        assertTrue(json.contains("\"signedIn\":true"))
    }

    @Test
    fun null_sections_render_as_json_null() {
        val json = ClientReport.build(
            reportId = "x", createdAtMs = 0L, note = "", app = null,
            device = null, profile = null, events = emptyList(),
            webDiagnostics = null,
        )
        assertTrue(json.contains("\"app\":null"))
        assertTrue(json.contains("\"device\":null"))
        assertTrue(json.contains("\"profile\":null"))
        assertTrue(json.contains("\"web\":null"))
        assertTrue(json.contains("\"events\":[]"))
    }

    @Test
    fun redacts_password_token_bearer_and_session_cookie() {
        val events = listOf(
            DiagEvent(0, "I", "T", "password=hunter2"),
            DiagEvent(1, "I", "T", "api_key: sk-abcdef123456"),
            DiagEvent(2, "I", "T", "Authorization: Bearer abc.def.ghi"),
            DiagEvent(3, "I", "T", "Cookie: code-server-session=deadbeef"),
        )
        val json = build(note = "token=leaked", events = events, web = "secret: s3cr3t")
        assertFalse("password leaked: $json", json.contains("hunter2"))
        assertFalse("api key leaked: $json", json.contains("sk-abcdef123456"))
        assertFalse("bearer leaked: $json", json.contains("abc.def.ghi"))
        assertFalse("session leaked: $json", json.contains("deadbeef"))
        assertFalse("note token leaked: $json", json.contains("leaked"))
        assertFalse("web secret leaked: $json", json.contains("s3cr3t"))
        assertTrue(json.contains("***"))
    }

    @Test
    fun redaction_is_idempotent() {
        val once = ClientReport.redact("password=hunter2 Bearer abc.def")
        assertEquals(once, ClientReport.redact(once))
        assertFalse(once.contains("hunter2"))
    }

    @Test
    fun escaping_handles_quotes_newlines_and_control_chars() {
        // Tab is mid-string on purpose: build() trims the note, so a trailing
        // tab would be stripped before escaping and prove nothing.
        val json = build(note = "line1\nline2\t\"quoted\" \u0001end")
        assertTrue(json.contains("line1\\nline2"))
        assertTrue(json.contains("\\\"quoted\\\""))
        assertTrue(json.contains("\\t"))
        assertTrue(json.contains("\\u0001"))
    }

    @Test
    fun clips_note_and_marks_truncated() {
        val json = build(note = "n".repeat(ClientReport.MAX_NOTE_CHARS + 50))
        assertTrue(json.contains("\"truncated\":true"))
        // The stored note is exactly the cap; count the run of n's.
        val noteField = Regex("\"note\":\"(n*)\"").find(json)!!.groupValues[1]
        assertEquals(ClientReport.MAX_NOTE_CHARS, noteField.length)
    }

    @Test
    fun keeps_only_the_newest_events_when_over_cap() {
        val total = ClientReport.MAX_EVENTS + 25
        val events = (1..total).map {
            DiagEvent(it.toLong(), "I", "T", "event$it")
        }
        val json = build(events = events)
        assertTrue(json.contains("\"truncated\":true"))
        assertFalse(json.contains("\"event1\""))
        assertFalse(json.contains("\"event25\""))
        assertTrue(json.contains("\"event26\""))
        assertTrue(json.contains("\"event$total\""))
        // Exactly the cap remains.
        assertEquals(
            ClientReport.MAX_EVENTS,
            Regex("\"msg\":").findAll(json).count(),
        )
    }

    @Test
    fun clips_web_blob_and_marks_truncated() {
        val json = build(web = "w".repeat(ClientReport.MAX_WEB_CHARS + 10))
        assertTrue(json.contains("\"truncated\":true"))
        val webField = Regex("\"web\":\"(w*)\"").find(json)!!.groupValues[1]
        assertEquals(ClientReport.MAX_WEB_CHARS, webField.length)
    }

    @Test
    fun generated_report_ids_are_short_and_unique() {
        val a = ClientReport.newReportId()
        val b = ClientReport.newReportId()
        assertEquals(12, a.length)
        assertTrue(a != b)
    }
    @Test fun quoted_json_secrets_remain_valid_and_headers_and_sso_urls_are_masked() {
        assertEquals("""{"password":"***","access_token":"***","status":401}""", ClientReport.redact("""{"password":"phrase with spaces","access_token":"secret-token","status":401}"""))
        val redacted = ClientReport.redact("Cookie: gateway=one; tofu_session=two\nhttps://user:pass@idp.example:8443/auth?code=three&state=four#access_token=five")
        for (value in listOf("one", "two", "three", "four", "five", "user:pass")) assertFalse(redacted.contains(value))
        assertTrue(redacted.contains("https://idp.example:8443/auth"))
        val oauth = ClientReport.redact("""{"client_secret":"one-private","accessToken":"two-private","private_key":"three-private"}""")
        for (value in listOf("one-private", "two-private", "three-private")) assertFalse(oauth.contains(value))
    }

    @Test fun unicode_and_json_escaping_cannot_overflow_clipboard_report_budget() {
        val events = (1..300).map { DiagEvent(it.toLong(), "E", "Net", "文\"".repeat(2000)) }
        val report = build(events = events, web = "文".repeat(ClientReport.MAX_WEB_CHARS))
        assertTrue(report.toByteArray(Charsets.UTF_8).size <= ClientReport.MAX_REPORT_BYTES)
        assertTrue(report.contains("\"truncated\":true"))
        assertTrue(report.contains("\"t\":300"))
    }

    @Test fun escaped_json_and_long_quoted_values_are_redacted_without_recursive_matching() {
        val escaped = """{"nested":"{\"password\":\"private-one, private-two\"}"}"""
        val hidden = ClientReport.redact(escaped)
        assertFalse(hidden.contains("private-one"))
        assertFalse(hidden.contains("private-two"))
        val large = """{"password":"""" + "private-long".repeat(50_000) + "\"}"
        assertFalse(ClientReport.redact(large).contains("private-long"))
    }

}
