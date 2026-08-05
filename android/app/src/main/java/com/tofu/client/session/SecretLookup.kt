package com.tofu.client.session

/** Read-side seam over the secret store so session logic is testable with a fake. */
interface SecretLookup {
    fun secretFor(alias: String): String?
}
