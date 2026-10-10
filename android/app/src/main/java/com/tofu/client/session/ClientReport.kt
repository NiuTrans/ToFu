package com.tofu.client.session

import java.time.Instant
import java.util.UUID

/** Immutable facts about the app build, assembled by the Android layer. */
data class AppReportInfo(
    val versionName: String,
    val versionCode: Int,
    val packageName: String,
    val debug: Boolean,
    val buildId: String = "",
)

/** A best-effort device descriptor; every field is already a plain string. */
data class DeviceReportInfo(
    val sdkInt: Int,
    val release: String,
    val model: String,
    val manufacturer: String,
    val abi: String,
)

/**
 * The server a report is about, or null for an app-level report. Carries only
 * non-secret identity: how to recognize the server, never how to authenticate
 * to it. The password lives in the vault and is deliberately absent here.
 */
data class ProfileReportInfo(
    val alias: String,
    val host: String,
    val authType: String,
    val managed: Boolean,
    val signedIn: Boolean,
)

/**
 * Builds the wire payload for the "Report a problem" flow.
 *
 * Produces a single JSON string the native shell can copy, share, or POST to
 * `POST /api/v1/mobile/client-report`. The builder is pure Kotlin (only
 * `java.time`/`java.util`), so it is covered by the fast JDK test tier, and it
 * is total: no input can make it throw.
 *
 * Two guarantees it owns:
 *  * **Bounded** — the note, event count and web blob are clipped so a huge
 *    console or paste can never produce an unbounded request.
 *  * **Redacted** — every free-text field is passed through [redact], so a
 *    token that leaked into a console line or a user's note is masked before
 *    it leaves the device. The vault password is never an input at all.
 */
object ClientReport {
    const val SCHEMA = "tofu.client-report/v1"
    const val MAX_NOTE_CHARS = 4000
    const val MAX_EVENTS = 300
    const val MAX_WEB_CHARS = 32 * 1024
    const val MAX_REPORT_BYTES = 192 * 1024
    private const val MAX_ID_CHARS = 64

    private val SECRET_PREFIX = Regex(
        """(?i)(\b(?:password|passwd|pwd|(?:access[_-]?|refresh[_-]?|id[_-]?|csrf[_-]?|auth[_-]?|session[_-]?)?token|(?:client[_-]?)?secret|private[_-]?key|api[_-]?key|authorization|cookie|set-cookie|tofu_session|code-server-session|sessionid|jsessionid)(?:\\?["'])?)(\s*[:=]\s*)""",
    )
    private val BEARER = Regex("""(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+""")
    private val HEADER_SECRET = Regex("""(?im)\b(cookie|set-cookie|authorization)\s*:\s*[^\r\n]+""")
    private val URL = Regex("""(?i)(?:https?|tofu)://[^\s"'<>]+""")

    /** A short, log-friendly report id (no dashes). */
    fun newReportId(): String =
        UUID.randomUUID().toString().replace("-", "").take(12)

    /**
     * Mask obvious secrets in free text. Conservative and idempotent: it only
     * rewrites a value that follows a secret-looking key or an auth scheme, so
     * ordinary prose survives.
     */
    internal fun redact(text: String): String {
        if (text.isEmpty()) return text
        var out = URL.replace(text) { safeUrl(it.value) }
        out = HEADER_SECRET.replace(out) { it.groupValues[1] + ": ***" }
        out = BEARER.replace(out, "Bearer ***")
        return maskSecretValues(out)
    }

    /** Scan quoted values explicitly: long/escaped console JSON must not recurse in a regex engine. */
    private fun maskSecretValues(text: String): String = buildString {
        var cursor = 0
        for (match in SECRET_PREFIX.findAll(text)) {
            if (match.range.first < cursor) continue
            val start = match.range.last + 1
            append(text, cursor, start)
            val escaped = text.startsWith("\\\"", start)
            val quote = when {
                escaped -> '"'
                start < text.length && text[start] in "\"'" -> text[start]
                else -> null
            }
            var end = start
            if (quote != null) {
                end += if (escaped) 2 else 1
                while (end < text.length) {
                    if (escaped && text[end] == '\\') {
                        var next = end
                        while (next < text.length && text[next] == '\\') next++
                        if (next < text.length && text[next] == quote && next - end == 1) { end = next + 1; break }
                        end = (next + 1).coerceAtMost(text.length)
                    } else if (!escaped && text[end] == '\\') {
                        end = (end + 2).coerceAtMost(text.length)
                    } else if (!escaped && text[end] == quote) { end++; break }
                    else end++
                }
                val marker = if (escaped) "\\\"" else quote.toString()
                append(marker).append("***").append(marker)
            } else {
                while (end < text.length && text[end] !in "\r\n,;&}") end++
                append("***")
            }
            cursor = end
        }
        append(text, cursor, text.length)
    }

    /** Keep endpoint identity without query/fragment/userinfo, which often carry SSO secrets. */
    fun safeUrl(raw: String): String {
        return try {
            val uri = java.net.URI(raw)
            val host = uri.host ?: return "[invalid URL]"
            "${uri.scheme}://$host${if (uri.port < 0) "" else ":${uri.port}"}${uri.rawPath.orEmpty()}"
        } catch (_: java.net.URISyntaxException) { "[invalid URL]" }
    }

    fun build(
        reportId: String,
        createdAtMs: Long,
        note: String,
        app: AppReportInfo?,
        device: DeviceReportInfo?,
        profile: ProfileReportInfo?,
        events: List<DiagEvent>,
        webDiagnostics: String?,
        captureTruncated: Boolean = false,
    ): String {
        val trimmedNote = note.trim()
        val clippedNote = redact(trimmedNote.take(MAX_NOTE_CHARS)).take(MAX_NOTE_CHARS)
        var keptEvents =
            if (events.size > MAX_EVENTS) events.takeLast(MAX_EVENTS) else events
        val webLength = webDiagnostics?.length ?: 0
        var clippedWeb = webDiagnostics?.take(MAX_WEB_CHARS)?.let(::redact)?.take(MAX_WEB_CHARS)
        var truncated = captureTruncated || trimmedNote.length > MAX_NOTE_CHARS ||
            events.size > MAX_EVENTS || webLength > MAX_WEB_CHARS

        val appJson = app?.let {
            obj(
                "versionName" to str(redact(it.versionName).take(128)),
                "versionCode" to num(it.versionCode),
                "package" to str(redact(it.packageName).take(128)),
                "debug" to bool(it.debug),
                "buildId" to str(redact(it.buildId).take(64)),
            )
        }
        val deviceJson = device?.let {
            obj(
                "sdkInt" to num(it.sdkInt),
                "release" to str(redact(it.release).take(256)),
                "model" to str(redact(it.model).take(256)),
                "manufacturer" to str(redact(it.manufacturer).take(256)),
                "abi" to str(redact(it.abi).take(256)),
            )
        }
        val profileJson = profile?.let {
            obj(
                "alias" to str(redact(it.alias).take(256)),
                "host" to str(redact(it.host).take(256)),
                "authType" to str(redact(it.authType).take(256)),
                "managed" to bool(it.managed),
                "signedIn" to bool(it.signedIn),
            )
        }

        fun payload(): String = obj(
            "schema" to str(SCHEMA),
            "reportId" to str(redact(reportId).take(MAX_ID_CHARS)),
            "createdAt" to str(isoUtc(createdAtMs)),
            "truncated" to bool(truncated),
            "note" to str(clippedNote),
            "app" to (appJson ?: "null"),
            "device" to (deviceJson ?: "null"),
            "profile" to (profileJson ?: "null"),
            "events" to arr(keptEvents.map(::eventJson)),
            "web" to (clippedWeb?.let(::str) ?: "null"),
        )
        var result = payload()
        // Clipboard and ACTION_SEND share Android's Binder transaction limit.
        // Enforce actual UTF-8 bytes, including escaping and Unicode, not chars.
        while (result.toByteArray(Charsets.UTF_8).size > MAX_REPORT_BYTES) {
            truncated = true
            if (clippedWeb != null && clippedWeb!!.length > 1024) {
                clippedWeb = clippedWeb!!.take(clippedWeb!!.length / 2)
            } else if (keptEvents.isNotEmpty()) {
                keptEvents = keptEvents.drop((keptEvents.size / 4).coerceAtLeast(1))
            } else { clippedWeb = null }
            result = payload()
        }
        return result
    }

    private fun eventJson(event: DiagEvent): String = obj(
        "t" to num(event.atMs),
        "lvl" to str(event.level),
        "tag" to str(redact(event.tag).take(DiagLog.MAX_TAG_CHARS)),
        "msg" to str(redact(event.message).take(DiagLog.MAX_MESSAGE_CHARS)),
    )

    private fun isoUtc(epochMs: Long): String =
        Instant.ofEpochMilli(epochMs).toString()

    private fun obj(vararg pairs: Pair<String, String>): String =
        pairs.joinToString(",", "{", "}") { (key, value) -> quote(key) + ":" + value }

    private fun arr(items: List<String>): String =
        items.joinToString(",", "[", "]")

    private fun str(value: String): String = quote(value)

    private fun num(value: Number): String = value.toString()

    private fun bool(value: Boolean): String = value.toString()

    private fun quote(value: String): String = buildString(value.length + 2) {
        append('"')
        for (ch in value) {
            when (ch) {
                '"' -> append("\\\"")
                '\\' -> append("\\\\")
                '\n' -> append("\\n")
                '\r' -> append("\\r")
                '\t' -> append("\\t")
                '\b' -> append("\\b")
                '\u000C' -> append("\\f")
                else -> if (ch < ' ') {
                    append("\\u")
                    append(ch.code.toString(16).padStart(4, '0'))
                } else {
                    append(ch)
                }
            }
        }
        append('"')
    }
}
