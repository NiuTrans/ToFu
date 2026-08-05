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
}
