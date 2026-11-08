package com.tofu.client.selfcheck

import com.tofu.client.runtime.KernelSecretValue
import com.tofu.client.session.ClientReport
import com.tofu.client.session.DiagLog

/**
 * Redaction for everything a self-check report carries. A report never holds
 * the local kernel's secret, tokens, cookies, account names, Wi-Fi names or
 * file contents:
 *
 * - **Structurally:** the collectors read no accounts, Wi-Fi state, cookies or
 *   files' contents; HTTP probes keep status codes and timings, never bodies
 *   or headers; captured process output is bounded excerpts of known commands.
 * - **At serialization:** every string value passes through [scrub]: the
 *   install secret (its exact value when known, and anything shaped like
 *   `tofu_admin_<hex>`, so a clipped secret is caught too), then the client
 *   report's redaction ([ClientReport.redact]: bearer tokens, cookie and
 *   authorization headers, `key=value` secrets, URL queries), then the
 *   diagnostic event bound [DiagLog.MAX_MESSAGE_CHARS]. Scrubbing runs before
 *   clipping, so a clip can never cut a secret into an unrecognisable prefix.
 */
class SelfCheckRedactor(private val secret: KernelSecretValue? = null) {
    fun scrub(text: String): String {
        var out = secret?.scrub(text) ?: text
        out = SECRET_SHAPED.replace(out, KernelSecretValue.REDACTED)
        out = ClientReport.redact(out)
        return if (out.length <= DiagLog.MAX_MESSAGE_CHARS) out else out.take(DiagLog.MAX_MESSAGE_CHARS) + CLIPPED
    }

    companion object {
        private val SECRET_SHAPED = Regex("${KernelSecretValue.PREFIX}[0-9A-Fa-f]+")
        const val CLIPPED = "…"
    }
}
