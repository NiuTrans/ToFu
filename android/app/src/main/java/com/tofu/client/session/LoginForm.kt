package com.tofu.client.session

import okhttp3.HttpUrl

/**
 * Pure (Android-free) discovery of the code-server login POST target.
 *
 * Gap-1 hardening: we must NOT assume the login endpoint is the origin-root
 * `/login`. That is verified-correct for the MLP sandbox, but a code-server
 * deployed behind a path prefix serves a login page whose `<form action>` is
 * relative (e.g. `./login` under `/some/prefix/`). Posting to the assumed
 * origin-root would silently fail auth there.
 *
 * [resolveAction] parses the first `<form … action="…">` out of the fetched
 * login page and resolves it against the page URL. The caller falls back to
 * origin-root only when no form/action is found.
 */
object LoginForm {

    // <form ... action="X" ...>  — captures the quote char and the value.
    private val FORM_ACTION = Regex(
        """<form\b[^>]*\baction\s*=\s*(["'])(.*?)\1""",
        setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL),
    )

    /**
     * Resolve the login POST URL from [html] served at [pageUrl].
     *
     * @return the resolved absolute URL, or null if no `<form action>` is found
     *         (caller should fall back to the origin-root `/login`).
     *
     * An empty `action=""` legitimately means "post to this page" and resolves
     * to [pageUrl] itself.
     */
    fun resolveAction(html: String, pageUrl: HttpUrl): HttpUrl? {
        val m = FORM_ACTION.find(html) ?: return null
        val action = m.groupValues[2].trim()
        return pageUrl.resolve(action)   // handles absolute, root-relative, and ./relative
    }
}
