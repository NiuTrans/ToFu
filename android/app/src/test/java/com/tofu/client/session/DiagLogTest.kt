package com.tofu.client.session

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Guards the native diagnostic ring: ordering, bounded capacity, clipped
 * strings and level normalization. These are the properties that make the
 * "Report a problem" tail trustworthy even after a long session.
 */
class DiagLogTest {

    @Test
    fun records_in_order_and_snapshots_oldest_first() {
        val log = DiagLog(clock = { 0L })
        log.record("I", "A", "first")
        log.record("W", "B", "second")

        val events = log.snapshot()
        assertEquals(listOf("first", "second"), events.map { it.message })
        assertEquals(listOf("I", "W"), events.map { it.level })
    }

    @Test
    fun evicts_oldest_when_capacity_is_exceeded() {
        val log = DiagLog(capacity = 3, clock = { 0L })
        (1..5).forEach { log.record("I", "T", "m$it") }

        assertEquals(3, log.size())
        assertEquals(listOf("m3", "m4", "m5"), log.snapshot().map { it.message })
    }

    @Test
    fun clips_tag_and_message_at_capture_time() {
        val log = DiagLog(clock = { 0L })
        log.record("I", "T".repeat(500), "M".repeat(9000))

        val event = log.snapshot().single()
        assertEquals(DiagLog.MAX_TAG_CHARS, event.tag.length)
        assertEquals(DiagLog.MAX_MESSAGE_CHARS, event.message.length)
    }

    @Test
    fun normalizes_unknown_levels_to_info() {
        assertEquals("I", DiagLog.normalizeLevel(""))
        assertEquals("I", DiagLog.normalizeLevel("verbose"))
        assertEquals("E", DiagLog.normalizeLevel("error"))
        assertEquals("W", DiagLog.normalizeLevel("w"))
        assertEquals("D", DiagLog.normalizeLevel("DEBUG"))
    }

    @Test
    fun uses_injected_clock_for_timestamps() {
        var now = 100L
        val log = DiagLog(clock = { now })
        log.record("I", "T", "a")
        now = 250L
        log.record("I", "T", "b")

        assertEquals(listOf(100L, 250L), log.snapshot().map { it.atMs })
    }

    @Test
    fun clear_empties_the_ring() {
        val log = DiagLog(clock = { 0L })
        log.record("I", "T", "a")
        log.clear()
        assertTrue(log.snapshot().isEmpty())
    }
    @Test fun secrets_are_redacted_before_memory_capture() {
        val log = DiagLog()
        log.record("E", "Auth", "token=never-store-this")
        assertEquals("token=***", log.snapshot().single().message)
    }

}
