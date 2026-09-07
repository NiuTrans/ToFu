package com.tofu.client.session

import okhttp3.HttpUrl.Companion.toHttpUrl
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Guards Gap-1: the login POST target is derived from the served `<form action>`
 * and resolved against the page URL — NOT assumed to be the origin-root /login.
 */
class LoginFormTest {

    /**
     * POISONED FIXTURE: a code-server behind a path prefix. The login page is
     * served at `/dev/abc/login` and its form action is the RELATIVE `./login`.
     * The correct POST target is `/dev/abc/login`, NOT the origin-root `/login`.
     */
    @Test
    fun resolves_relative_action_under_subpath_prefix() {
        val pageUrl = "https://host.example.com/dev/abc/login".toHttpUrl()
        val html = """
            <html><body>
              <form method="post" action="./login">
                <input type="password" name="password"/>
                <input type="hidden" name="base" value="."/>
              </form>
            </body></html>
        """.trimIndent()

        val resolved = LoginForm.resolveAction(html, pageUrl)

        // NEUTER CHECK: if login POSTed to the assumed origin-root, it would hit
        // "/login" and fail on this deployment. Correct resolution keeps the prefix.
        assertEquals("https://host.example.com/dev/abc/login", resolved.toString())
    }

    @Test
    fun resolves_root_relative_action() {
        val pageUrl = "https://host.example.com/proxy/15000/login".toHttpUrl()
        val html = """<form action="/login" method="post"></form>"""
        assertEquals(
            "https://host.example.com/login",
            LoginForm.resolveAction(html, pageUrl).toString(),
        )
    }

    @Test
    fun empty_action_posts_back_to_page() {
        val pageUrl = "https://host.example.com/login".toHttpUrl()
        val html = """<form action="" method="post"></form>"""
        assertEquals(pageUrl.toString(), LoginForm.resolveAction(html, pageUrl).toString())
    }

    @Test
    fun no_form_returns_null_so_caller_falls_back() {
        val pageUrl = "https://host.example.com/login".toHttpUrl()
        assertNull(LoginForm.resolveAction("<html>no form here</html>", pageUrl))
    }

    @Test
    fun single_quoted_action_is_parsed() {
        val pageUrl = "https://host.example.com/login".toHttpUrl()
        val html = "<form action='./login' method='post'></form>"
        assertEquals(
            "https://host.example.com/login",
            LoginForm.resolveAction(html, pageUrl).toString(),
        )
    }
}
