package com.tofu.client.session

import org.junit.Assert.*
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

/** Reconciliation owns only the value it observed, not arbitrary later vault state. */
class SecretVaultTest {
    private class Vault : SecretVault {
        private val values = mutableMapOf<String, String>()
        override fun secretFor(alias: String) = synchronized(this) { values[alias] }
        override fun putSecret(alias: String, secret: String) = synchronized(this) { values[alias] = secret }
        override fun removeSecret(alias: String) { synchronized(this) { values.remove(alias) } }
    }

    @Test fun unchanged_values_can_be_installed_and_removed_idempotently() {
        val vault = Vault()
        assertTrue(vault.compareAndSetSecret("test", null, "fixture-new"))
        assertTrue(vault.compareAndSetSecret("test", null, "fixture-new"))
        assertTrue(vault.compareAndSetSecret("test", "fixture-new", null))
        assertNull(vault.secretFor("test"))
    }

    @Test fun a_foreign_value_is_neither_overwritten_nor_removed() {
        val vault = Vault()
        vault.putSecret("test", "fixture-foreign")
        assertFalse(vault.compareAndSetSecret("test", "fixture-old", "fixture-new"))
        assertFalse(vault.compareAndSetSecret("test", "fixture-old", null))
        assertEquals("fixture-foreign", vault.secretFor("test"))
    }

    private fun fileVault(lock: Any, values: MutableMap<String, String>) = SerializedSecretVault(
        lock, read = { values[it] }, write = { alias, value -> values[alias] = value }, remove = { values.remove(it); Unit },
    )

    @Test fun another_wrapper_of_the_same_file_cannot_have_its_newer_value_replaced_or_removed() {
        val lock = Any(); val values = mutableMapOf<String, String>()
        val first = fileVault(lock, values); val second = fileVault(lock, values)
        assertTrue(first.compareAndSetSecret("test", null, "fixture-old"))
        second.putSecret("test", "fixture-foreign")
        assertFalse(first.compareAndSetSecret("test", "fixture-old", "fixture-new"))
        assertFalse(first.compareAndSetSecret("test", "fixture-old", null))
        assertEquals("fixture-foreign", second.secretFor("test"))
    }

    @Test fun concurrent_reconciliation_across_same_file_wrappers_has_one_atomic_winner() {
        val lock = Any(); val values = mutableMapOf<String, String>()
        val first = fileVault(lock, values); val second = fileVault(lock, values)
        val start = CountDownLatch(1)
        val winners = AtomicInteger()
        val workers = listOf(first to "fixture-one", second to "fixture-two").map { (vault, value) ->
            Thread {
                check(start.await(5, TimeUnit.SECONDS))
                if (vault.compareAndSetSecret("test", null, value)) winners.incrementAndGet()
            }.apply { start() }
        }
        start.countDown()
        workers.forEach { it.join(5000); assertFalse(it.isAlive) }
        assertEquals(1, winners.get())
        assertEquals(first.secretFor("test"), second.secretFor("test"))
        assertTrue(first.secretFor("test") in listOf("fixture-one", "fixture-two"))
    }

    @Test fun competing_reconciliation_has_one_atomic_winner() {
        val vault = Vault()
        val start = CountDownLatch(1)
        val winners = AtomicInteger()
        val workers = listOf("fixture-one", "fixture-two").map { value ->
            Thread {
                check(start.await(5, TimeUnit.SECONDS))
                if (vault.compareAndSetSecret("test", null, value)) winners.incrementAndGet()
            }.apply { start() }
        }
        start.countDown()
        workers.forEach { it.join(5000); assertFalse(it.isAlive) }
        assertEquals(1, winners.get())
        assertTrue(vault.secretFor("test") in listOf("fixture-one", "fixture-two"))
    }
}
