package com.tofu.client.session

/** The client version a server advertises via ``/api/health``, plus its link. */
data class AdvertisedClient(val version: String, val downloadUrl: String?)

/** The outcome of comparing the installed app against what a server advertises. */
data class UpdateStatus(
    val available: Boolean,
    val installed: String,
    val latest: String,
    val downloadUrl: String?,
)

/**
 * Pure logic for the in-app update check: parse the two client fields out of a
 * server's ``/api/health`` blob and compare the advertised version against the
 * installed one.
 *
 * Deliberately dependency-free (no `org.json`) so it runs in the fast JDK test
 * tier. ``/api/health`` is a small flat JSON object, so a targeted field
 * extractor is honest here and avoids pulling a parser into the shell; a
 * non-matching body simply yields no advertised version rather than throwing.
 *
 * The comparison is numeric and segment-wise ("0.1.18" > "0.1.9", unlike a
 * string compare), tolerant of a leading `v` and of pre-release suffixes
 * ("0.2.0-rc1" compares as 0.2.0), and default-zero-padded so "0.2" == "0.2.0".
 */
object UpdateCheck {

    /** Extract the advertised client version/link, or null when absent. */
    fun parseAdvertised(healthJson: String?): AdvertisedClient? {
        if (healthJson.isNullOrBlank()) return null
        val version = jsonString(healthJson, "mobile_client_version")
        if (version.isNullOrBlank()) return null
        val url = jsonString(healthJson, "mobile_client_url")
        return AdvertisedClient(version, url?.takeIf { it.isNotBlank() })
    }

    /** Build the status for the UI. [advertised] null → nothing to offer. */
    fun evaluate(installedName: String, advertised: AdvertisedClient?): UpdateStatus {
        if (advertised == null) {
            return UpdateStatus(false, installedName, installedName, null)
        }
        val newer = compareVersions(advertised.version, installedName) > 0
        return UpdateStatus(
            available = newer,
            installed = installedName,
            latest = advertised.version,
            downloadUrl = advertised.downloadUrl,
        )
    }

    /** Returns > 0 when [a] is newer than [b], < 0 when older, 0 when equal. */
    fun compareVersions(a: String, b: String): Int {
        val pa = segments(a)
        val pb = segments(b)
        val count = maxOf(pa.size, pb.size)
        for (i in 0 until count) {
            val x = pa.getOrElse(i) { 0 }
            val y = pb.getOrElse(i) { 0 }
            if (x != y) return x - y
        }
        return 0
    }

    private fun segments(version: String): List<Int> =
        version.trim()
            // Drop pre-release / build metadata before the leading '-' / '+',
            // so "0.2.0-rc1" is compared as 0.2.0 (not 0.2.0.1).
            .substringBefore('-')
            .substringBefore('+')
            .split(Regex("[^0-9]+"))
            .filter { it.isNotEmpty() }
            .mapNotNull { it.toIntOrNull() }

    private fun jsonString(json: String, key: String): String? {
        val pattern = Regex(
            "\"" + Regex.escape(key) + "\"\\s*:\\s*\"((?:\\\\.|[^\"\\\\])*)\"",
        )
        val match = pattern.find(json) ?: return null
        return unescape(match.groupValues[1])
    }

    private fun unescape(raw: String): String {
        if ('\\' !in raw) return raw
        val out = StringBuilder(raw.length)
        var i = 0
        while (i < raw.length) {
            val ch = raw[i]
            if (ch != '\\' || i + 1 >= raw.length) {
                out.append(ch)
                i++
                continue
            }
            when (val next = raw[i + 1]) {
                'n' -> out.append('\n')
                't' -> out.append('\t')
                'r' -> out.append('\r')
                'b' -> out.append('\b')
                'f' -> out.append('\u000C')
                '"', '\\', '/' -> out.append(next)
                else -> out.append(next)
            }
            i += 2
        }
        return out.toString()
    }
}
