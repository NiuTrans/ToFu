package com.tofu.client.session

/** One captured client-side diagnostic event. Plain data — no Android types. */
data class DiagEvent(
    /** Wall-clock capture time (ms since epoch); used only for ordering. */
    val atMs: Long,
    /** Single-letter log level: D/I/W/E. */
    val level: String,
    val tag: String,
    val message: String,
)

/**
 * A bounded, thread-safe, in-memory ring of recent client events.
 *
 * Why this exists: the SPA's own collector (`__tofuCollectDiagnostics`) is
 * unavailable precisely when the page is wedged — the failure we most need
 * evidence for. The native shell observes events the web layer cannot report
 * on (WebView console output before the app scripts run, main-frame load
 * failures, re-auth outcomes, lifecycle), so it keeps its own tail and merges
 * it into the support report. The buffer is bounded so a chatty console can
 * never grow the process without limit, and every string is clipped at capture
 * time so one pathological line cannot dominate the report.
 *
 * Pure Kotlin (no `android.*`), so it is exercised by the fast JDK test tier.
 * Redacts before capture. An optional private journal retains the tail across
 * process death; exporting a report still requires an explicit user action.
 */
class DiagLog(
    private val capacity: Int = DEFAULT_CAPACITY,
    /** Time source seam; defaults to the wall clock. */
    private val clock: () -> Long = { System.currentTimeMillis() },
    private val journal: DiagnosticJournal? = null,
) {
    private val lock = Any()
    private val events = ArrayDeque<DiagEvent>()
    @Volatile var evictedCount: Long = 0
        private set

    init {
        require(capacity > 0)
        journal?.load()?.takeLast(capacity)?.forEach { events.addLast(it) }
    }

    /** Record one event, evicting the oldest when the ring is full. */
    fun record(level: String, tag: String, message: String) {
        val event = DiagEvent(
            atMs = clock(),
            level = normalizeLevel(level),
            tag = ClientReport.redact(tag).take(MAX_TAG_CHARS),
            message = ClientReport.redact(message.take(MAX_MESSAGE_CHARS)).take(MAX_MESSAGE_CHARS),
        )
        synchronized(lock) {
            events.addLast(event)
            while (events.size > capacity) { events.removeFirst(); evictedCount++ }
            journal?.append(event)
        }
    }

    /** A stable oldest→newest copy of the current tail. */
    fun snapshot(): List<DiagEvent> = synchronized(lock) {
        val recent = events.toList()
        journal?.failure?.let { recent + DiagEvent(clock(), "W", "DiagJournal", ClientReport.redact(it).take(MAX_MESSAGE_CHARS)) } ?: recent
    }

    /** Capture bounded exception type, cause and stack; never feed request bodies here. */
    fun exception(tag: String, error: Throwable) {
        val detail = buildString {
            var current: Throwable? = error
            repeat(3) {
                val cause = current ?: return@repeat
                append(cause.javaClass.simpleName).append(": ").append(cause.message.orEmpty().take(MAX_MESSAGE_CHARS)).append('\n')
                cause.stackTrace.take(12).forEach { append("  at ").append(it).append('\n') }
                current = cause.cause
            }
        }
        record("E", tag, detail)
    }

    /** Explicit crash/background checkpoint; in-memory copying needs no I/O. */
    fun flush() = journal?.flush()

    fun size(): Int = synchronized(lock) { events.size }

    fun clear() = synchronized(lock) { events.clear() }

    companion object {
        const val DEFAULT_CAPACITY = 200
        const val MAX_TAG_CHARS = 64
        const val MAX_MESSAGE_CHARS = 2000

        /** Coerce any supplied level to a single D/I/W/E letter. */
        internal fun normalizeLevel(level: String): String {
            val head = level.trim().take(1).uppercase()
            return if (head == "D" || head == "I" || head == "W" || head == "E") head else "I"
        }
    }
}
