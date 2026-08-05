package com.tofu.client.session

import com.tofu.client.data.AuthType

/**
 * Mirror of the desktop agent's reachability probe (lib/desktop_agent/_probe.py)
 * — the contract that settles "does this URL actually reach Tofu?" at PASTE
 * time rather than after hours of silent retrying.
 *
 * The discrimination that matters behind a VS Code port-forwarding proxy: the
 * gateway edge refuses EVERY unauthenticated request with 401 — including
 * /api/health — so a bare 401 says nothing about whether the URL is right.
 * What separates the cases:
 *
 *  - Tofu's OWN refusal carries the api_error envelope
 *    ``{"ok":false,"error":{…}}`` — error is a JSON OBJECT;
 *  - the measured proxy edge answers ``{"error":"Unauthorized"}`` — error is a
 *    STRING — and a gateway landing page is 200 HTML with no bootId;
 *  - the positive proof of "this is Tofu" is 200 + a bootId in the health JSON.
 *
 * Pure and Android-free so the rules are unit-testable off-device (the JSON
 * checks are deliberately structural regexes — org.json is not on the pure-JVM
 * test classpath, and these three shapes are stable wire contracts).
 */
object TofuProbe {

    enum class Verdict {
        /** 200 + health JSON carries bootId — this IS Tofu. */
        TOFU,

        /** 401/403 with Tofu's api_error envelope — Tofu answered; its own gate refused. */
        TOFU_AUTH,

        /** 401/403 WITHOUT the Tofu envelope — the proxy edge bounced it before Tofu. */
        GATEWAY,

        /** 200 (or anything else) that is not Tofu's health JSON — landing page / wrong server. */
        NOT_TOFU,

        /** No usable HTTP response (transport failure / 5xx). */
        UNREACHABLE,
    }

    private val OK_FALSE = Regex(""""ok"\s*:\s*false""")
    private val ERROR_OBJECT = Regex(""""error"\s*:\s*\{""")
    private val BOOT_ID = Regex(""""bootId"\s*:\s*\"""")

    /**
     * Tofu's api_error envelope: ``{"ok":false,"error":{…}}`` (error is an
     * OBJECT). The gateway's form ``{"error":"Unauthorized"}`` (error as a
     * STRING) does NOT match — that asymmetry is the whole discrimination.
     */
    fun isTofuErrorEnvelope(body: String?): Boolean =
        body != null && OK_FALSE.containsMatchIn(body) && ERROR_OBJECT.containsMatchIn(body)

    /** The positive Tofu signal from /api/health: a bootId field. */
    fun hasBootId(body: String?): Boolean =
        body != null && BOOT_ID.containsMatchIn(body)

    /**
     * Classify an /api/health response. [status] 0 is the caller's sentinel
     * for "no HTTP response at all" (transport failure).
     */
    fun classify(status: Int, body: String?): Verdict = when {
        status == 0 -> Verdict.UNREACHABLE
        status in 500..599 -> Verdict.UNREACHABLE
        status == 401 || status == 403 ->
            if (isTofuErrorEnvelope(body)) Verdict.TOFU_AUTH else Verdict.GATEWAY
        status == 200 -> if (hasBootId(body)) Verdict.TOFU else Verdict.NOT_TOFU
        else -> Verdict.NOT_TOFU
    }

    /** True when [verdict] is something the user must fix (not just informational). */
    fun isProblem(verdict: Verdict, authType: AuthType, hasSecret: Boolean): Boolean =
        when (verdict) {
            Verdict.TOFU, Verdict.TOFU_AUTH -> false
            Verdict.GATEWAY -> when (authType) {
                AuthType.NONE -> true
                AuthType.CODE_SERVER_PASSWORD -> !hasSecret
                AuthType.INTERACTIVE_SSO -> false
            }
            Verdict.NOT_TOFU, Verdict.UNREACHABLE -> true
        }

    /**
     * The honest one-line explanation shown next to the URL field after a test.
     * [hasSecret] = a password is typed or reusable — only then is a gateway
     * 401 "expected" for a /proxy/ URL (the sign-in happens on open).
     */
    fun guidance(verdict: Verdict, authType: AuthType, hasSecret: Boolean): String = when (verdict) {
        Verdict.TOFU ->
            "Tofu answered — this URL is correct."
        Verdict.TOFU_AUTH ->
            "Tofu answered but requires sign-in — it will be handled on open."
        Verdict.GATEWAY -> when (authType) {
            AuthType.CODE_SERVER_PASSWORD ->
                if (hasSecret) {
                    "Behind the code-server gate (expected for /proxy/ URLs) — " +
                        "the saved password signs in on open."
                } else {
                    "Behind the code-server gate — enter the password, " +
                        "or this URL can't be reached."
                }
            AuthType.INTERACTIVE_SSO ->
                "Behind a sign-in gateway — you'll complete sign-in once in the app."
            AuthType.NONE ->
                "A gateway refused the request — this URL needs sign-in; " +
                    "switch the auth mode above."
        }
        Verdict.NOT_TOFU ->
            "Something answered, but it isn't Tofu — check the host and the /proxy/<port>/ prefix."
        Verdict.UNREACHABLE ->
            "No answer from this URL — check the network and the address."
    }
}
