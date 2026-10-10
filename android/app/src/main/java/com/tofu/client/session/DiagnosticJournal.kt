package com.tofu.client.session

import java.io.File
import java.io.IOException
import java.util.Base64
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * Private, redacted restart evidence for DiagLog. Two bounded segments; one
 * lazy serial writer with a bounded pending tail. Diagnostic I/O never blocks
 * authentication and never becomes a second user-state store.
 */
class DiagnosticJournal(
    private val directory: File,
    private val capacity: Int = DiagLog.DEFAULT_CAPACITY,
    private val segmentBytes: Int = 256 * 1024,
) : AutoCloseable {
    private val lock = Any()
    private val pending = ArrayDeque<DiagEvent>()
    private val writer = Executors.newSingleThreadExecutor { job -> Thread(job, "tofu-diagnostics").apply { isDaemon = true } }
    private var draining = false
    private var closed = false
    @Volatile var failure: String? = null
        private set
    private val current = File(directory, "current.log")
    private val previous = File(directory, "previous.log")

    init { require(capacity > 0); require(segmentBytes >= 16 * 1024) }

    /** Corrupt/oversized journal segments are reconstructible; never touch profiles or secrets. */
    fun load(): List<DiagEvent> {
        val tail = ArrayDeque<String>()
        for (file in listOf(previous, current)) try {
            if (!file.exists()) continue
            if (file.length() > segmentBytes) { failure = "Oversized diagnostic segment ignored"; continue }
            file.useLines { lines -> lines.forEach { line ->
                if (line.length <= 16 * 1024 && line.count { it == '\t' } == 3) tail.addLast(line)
                while (tail.size > capacity) tail.removeFirst()
            } }
        } catch (error: IOException) { failure = "Read ${error.javaClass.simpleName}: ${error.message}" }
        // Decode/redact only the retained tail; startup never parses thousands
        // of old records just to evict them again.
        return tail.mapNotNull(::decode)
    }

    fun append(event: DiagEvent) = synchronized(lock) {
        if (closed) return@synchronized
        // Defense in depth: direct callers cannot put raw secrets on disk.
        pending.addLast(event.copy(tag = ClientReport.redact(event.tag).take(DiagLog.MAX_TAG_CHARS), message = ClientReport.redact(event.message).take(DiagLog.MAX_MESSAGE_CHARS)))
        while (pending.size > capacity) pending.removeFirst()
        if (!draining) {
            draining = true
            writer.execute(::drain)
        }
    }

    private fun drain() {
        while (true) {
            val batch = synchronized(lock) {
                if (pending.isEmpty()) { draining = false; return }
                pending.toList().also { pending.clear() }
            }
            try {
                if (!directory.isDirectory && !directory.mkdirs()) throw IOException("Cannot create private diagnostic directory")
                var stream: java.io.FileOutputStream? = null
                try {
                    for (event in batch) {
                        val bytes = encode(event).toByteArray(Charsets.UTF_8)
                        if (current.length() + bytes.size > segmentBytes) {
                            stream?.close(); stream = null
                            java.nio.file.Files.move(current.toPath(), previous.toPath(), java.nio.file.StandardCopyOption.REPLACE_EXISTING)
                        }
                        if (stream == null) stream = java.io.FileOutputStream(current, true)
                        stream.write(bytes)
                    }
                } finally { stream?.close() }
            } catch (error: IOException) { failure = "Write ${error.javaClass.simpleName}: ${error.message}" }
        }
    }

    /** Queue barrier after the last enqueue; bounded wait is safe for a crash handler. */
    fun flush() {
        try {
            synchronized(lock) { if (closed) return; writer.submit {} }.get(2, TimeUnit.SECONDS)
        } catch (error: Exception) {
            if (error is InterruptedException) Thread.currentThread().interrupt()
            failure = "Checkpoint ${error.javaClass.simpleName}"
        }
    }

    override fun close() {
        synchronized(lock) { closed = true; writer.shutdown() }
        check(writer.awaitTermination(5, TimeUnit.SECONDS)) { "Diagnostic writer did not settle" }
    }

    private fun encode(e: DiagEvent): String {
        fun value(s: String) = Base64.getEncoder().encodeToString(s.toByteArray(Charsets.UTF_8))
        return "${e.atMs}\t${e.level}\t${value(e.tag)}\t${value(e.message)}\n"
    }

    private fun decode(line: String): DiagEvent? = try {
        val parts = line.split('\t', limit = 4)
        if (parts.size != 4 || line.length > 16 * 1024) null else {
            fun value(s: String) = String(Base64.getDecoder().decode(s), Charsets.UTF_8)
            DiagEvent(parts[0].toLong(), DiagLog.normalizeLevel(parts[1]), ClientReport.redact(value(parts[2])).take(DiagLog.MAX_TAG_CHARS), ClientReport.redact(value(parts[3])).take(DiagLog.MAX_MESSAGE_CHARS))
        }
    } catch (_: IllegalArgumentException) { null /* Torn or malformed diagnostic line; keep the remaining evidence. */ }
}
