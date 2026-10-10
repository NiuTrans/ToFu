package com.tofu.client.session

import java.io.File
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/** Restart, privacy, corruption and disk bounds are observable support contracts. */
class DiagnosticJournalTest {
    @get:Rule val temp = TemporaryFolder()

    @Test fun restart_recovers_redacted_failure_and_its_original_timestamp() {
        val dir = temp.newFolder()
        DiagnosticJournal(dir).use { journal ->
            val log = DiagLog(clock = { 1234 }, journal = journal)
            log.exception("Auth", IllegalStateException("password=private-value"))
            log.flush()
        }
        DiagnosticJournal(dir).use { journal ->
            val log = DiagLog(journal = journal)
            val error = log.snapshot().single()
            assertEquals(1234L, error.atMs)
            assertTrue(error.message.contains("IllegalStateException"))
            assertTrue(error.message.contains("DiagnosticJournalTest"))
            assertFalse(error.message.contains("private-value"))
        }
    }

    @Test fun journal_rotates_without_unbounded_persistent_growth() {
        val dir = temp.newFolder()
        DiagnosticJournal(dir, capacity = 300, segmentBytes = 16 * 1024).use { journal ->
            repeat(180) { journal.append(DiagEvent(it.toLong(), "E", "Net", "failure#$it " + "文".repeat(800))) }
            journal.flush()
            val files = dir.listFiles()!!
            assertTrue(files.size <= 2)
            assertTrue(files.all { it.length() <= 16 * 1024 })
            val restored = journal.load()
            assertTrue(restored.last().message.startsWith("failure#179 "))
        }
    }

    @Test fun torn_lines_do_not_hide_intact_events() {
        val dir = temp.newFolder()
        DiagnosticJournal(dir).use { journal -> journal.append(DiagEvent(1, "E", "Net", "valid")); journal.flush() }
        File(dir, "current.log").appendText("broken\n99\tI\t%%%\t%%%\n")
        DiagnosticJournal(dir).use { journal -> assertEquals(listOf("valid"), journal.load().map { it.message }) }
    }

    @Test fun unwritable_journal_keeps_memory_evidence_and_reports_its_own_failure() {
        val file = temp.newFile()
        DiagnosticJournal(file).use { journal ->
            val log = DiagLog(journal = journal)
            log.record("E", "Auth", "original failure")
            log.flush()
            val events = log.snapshot()
            assertTrue(events.any { it.message == "original failure" })
            assertTrue(events.any { it.tag == "DiagJournal" })
        }
    }
}
