package com.tofu.client.session

/**
 * Read+write seam over the encrypted secret store, so credential-mutating
 * orchestration ([SessionController]) is unit-testable with a fake. The
 * production impl is [SecretStore]; [SessionManager] only needs the read side
 * ([SecretLookup]), which this extends.
 */
interface SecretVault : SecretLookup {
    fun putSecret(alias: String, secret: String)
    fun removeSecret(alias: String)

    /**
     * Reconcile only the observed value (or an already-installed identical
     * value). Implementations must serialize this with their other mutations;
     * the production store shares its lock across instances of the same file.
     */
    fun compareAndSetSecret(alias: String, expected: String?, value: String?): Boolean = synchronized(this) {
        replaceObservedSecret(alias, expected, value)
    }
}

/**
 * One file's access owner, shared by every wrapper of that file. Android's
 * encrypted preferences supply these read/write ports; the pure tests exercise
 * the same CAS/ordinary-mutation serialization without platform substitutes.
 */
internal class SerializedSecretVault(
    private val fileLock: Any,
    private val read: (String) -> String?,
    private val write: (String, String) -> Unit,
    private val remove: (String) -> Unit,
) : SecretVault {
    override fun secretFor(alias: String): String? = synchronized(fileLock) { read(alias) }
    override fun putSecret(alias: String, secret: String) = synchronized(fileLock) { write(alias, secret) }
    override fun removeSecret(alias: String) = synchronized(fileLock) { remove(alias) }
    override fun compareAndSetSecret(alias: String, expected: String?, value: String?): Boolean = synchronized(fileLock) {
        replaceObservedSecret(alias, expected, value)
    }
}

private fun SecretVault.replaceObservedSecret(alias: String, expected: String?, value: String?): Boolean {
    val current = secretFor(alias)
    if (current != expected && current != value) return false
    if (current != value) {
        if (value == null) removeSecret(alias) else putSecret(alias, value)
    }
    return true
}
