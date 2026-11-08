package com.tofu.client.selfcheck

/**
 * The self-check report's JSON values and their one serializer.
 *
 * Every string value is rendered through a [SelfCheckRedactor], so nothing a
 * probe captured can reach a report unscrubbed. [Json.Raw] embeds a report
 * this writer produced earlier (a run that ended with the app process), which
 * is already redacted. Pure Kotlin: the pure-JVM test tier has no org.json.
 */
sealed interface Json {
    data class Str(val value: String) : Json
    data class Num(val value: Long) : Json
    data class Bool(val value: Boolean) : Json
    data object Null : Json
    data class Arr(val items: List<Json>) : Json
    data class Obj(val fields: List<Pair<String, Json>>) : Json
    data class Raw(val json: String) : Json

    companion object {
        fun of(value: Any?): Json = when (value) {
            null -> Null
            is Json -> value
            is String -> Str(value)
            is Boolean -> Bool(value)
            is Int -> Num(value.toLong())
            is Long -> Num(value)
            is List<*> -> Arr(value.map(::of))
            else -> throw IllegalArgumentException("not a report value: ${value::class.java.simpleName}")
        }
    }
}

fun obj(vararg fields: Pair<String, Any?>): Json.Obj = Json.Obj(fields.map { (key, value) -> key to Json.of(value) })

/** Two-space indented, insertion-ordered, newline-terminated JSON. */
fun Json.render(redactor: SelfCheckRedactor): String = StringBuilder().also { write(it, redactor, 0) }.append('\n').toString()

private fun Json.write(out: StringBuilder, redactor: SelfCheckRedactor, depth: Int) {
    when (this) {
        is Json.Str -> quote(out, redactor.scrub(value))
        is Json.Num -> out.append(value)
        is Json.Bool -> out.append(value)
        Json.Null -> out.append("null")
        is Json.Raw -> out.append(json.trim())
        is Json.Arr -> if (items.isEmpty()) out.append("[]") else {
            out.append('[')
            items.forEachIndexed { index, item ->
                if (index > 0) out.append(',')
                newline(out, depth + 1)
                item.write(out, redactor, depth + 1)
            }
            newline(out, depth)
            out.append(']')
        }
        is Json.Obj -> if (fields.isEmpty()) out.append("{}") else {
            out.append('{')
            fields.forEachIndexed { index, (key, value) ->
                if (index > 0) out.append(',')
                newline(out, depth + 1)
                quote(out, key)
                out.append(": ")
                value.write(out, redactor, depth + 1)
            }
            newline(out, depth)
            out.append('}')
        }
    }
}

private fun newline(out: StringBuilder, depth: Int) {
    out.append('\n')
    repeat(depth) { out.append("  ") }
}

private fun quote(out: StringBuilder, value: String) {
    out.append('"')
    for (ch in value) {
        when (ch) {
            '"' -> out.append("\\\"")
            '\\' -> out.append("\\\\")
            '\n' -> out.append("\\n")
            '\r' -> out.append("\\r")
            '\t' -> out.append("\\t")
            '\b' -> out.append("\\b")
            '\u000C' -> out.append("\\f")
            else -> if (ch < ' ' || ch == '\u2028' || ch == '\u2029') {
                out.append("\\u").append(ch.code.toString(16).padStart(4, '0'))
            } else {
                out.append(ch)
            }
        }
    }
    out.append('"')
}
