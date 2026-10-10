package com.tofu.client.session

import okhttp3.HttpUrl

/**
 * Recognizes the maintained code-server password form; unknown/ambiguous HTML
 * belongs in the browser. Never submit a saved secret to an arbitrary form.
 * Entry point: discover. Dependencies: OkHttp URL resolution only.
 */
object LoginForm {
    data class PasswordForm(val target: HttpUrl, val fields: Map<String, String>)
    private val FORMS = Regex("""<form\b([^>]*)>(.*?)</form\s*>""", setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL))
    private val INPUTS = Regex("""<input\b([^>]*)>""", RegexOption.IGNORE_CASE)
    private val ATTRIBUTES = Regex("""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""")
    private val ENTITIES = Regex("""&(?:amp|quot|apos|lt|gt|#\d+|#x[0-9a-fA-F]+);""")

    private fun attributes(text: String): Map<String, String> = ATTRIBUTES.findAll(text)
        .associate { it.groupValues[1].lowercase() to decode(it.groupValues.drop(2).firstOrNull { v -> v.isNotEmpty() }.orEmpty()) }

    private fun decode(value: String): String = ENTITIES.replace(value) { m ->
        when (m.value) {
            "&amp;" -> "&"; "&quot;" -> "\""; "&apos;" -> "'"; "&lt;" -> "<"; "&gt;" -> ">"
            else -> {
                val entity = m.value.removePrefix("&#").removeSuffix(";")
                val number = if (entity.startsWith("x")) entity.drop(1).toIntOrNull(16) else entity.toIntOrNull()
                number?.takeIf { it in 1..0xffff }?.toChar()?.toString() ?: m.value
            }
        }
    }

    fun discover(html: String, pageUrl: HttpUrl, server: ServerUrl): PasswordForm? {
        if (!server.sameOrigin(pageUrl) || html.length > 262_144) return null
        // Comments and script literals are not forms the user can submit.
        val visible = html.replace(Regex("""<!--.*?-->|<script\b[^>]*>.*?</script\s*>""", setOf(RegexOption.IGNORE_CASE, RegexOption.DOT_MATCHES_ALL)), "")
        val candidates = FORMS.findAll(visible).mapNotNull { match ->
            val form = attributes(match.groupValues[1])
            if (!form["method"].orEmpty().equals("post", ignoreCase = true)) return@mapNotNull null
            val inputs = INPUTS.findAll(match.groupValues[2]).map { attributes(it.groupValues[1]) }.toList()
            if (inputs.count { it["name"] == "password" && it["type"].equals("password", true) } != 1) return@mapNotNull null
            if (inputs.none { it["name"] == "base" && it["type"].equals("hidden", true) }) return@mapNotNull null
            if (form["class"].orEmpty().split(Regex("""\s+""")).none { it == "login-form" }) return@mapNotNull null
            val target = pageUrl.resolve(form["action"].orEmpty()) ?: return@mapNotNull null
            if (!server.sameOrigin(target) || target.username.isNotEmpty() || target.password.isNotEmpty()) return@mapNotNull null
            val hidden = inputs.filter { it["type"].equals("hidden", true) && it.containsKey("name") }
            if (hidden.any { it["name"] == "password" } || hidden.map { it["name"] }.distinct().size != hidden.size) return@mapNotNull null
            val fields = hidden.associate { it.getValue("name") to it["value"].orEmpty() }.toMutableMap()
            if ("href" in fields) fields["href"] = pageUrl.toString()
            PasswordForm(target, fields)
        }.toList()
        return candidates.singleOrNull()
    }

    /** Kept for callers parsing a form action without granting credential authority. */
    fun resolveAction(html: String, pageUrl: HttpUrl): HttpUrl? {
        val form = FORMS.find(html) ?: return null
        val attrs = attributes(form.groupValues[1])
        return if ("action" in attrs) pageUrl.resolve(attrs.getValue("action")) else null
    }
}
