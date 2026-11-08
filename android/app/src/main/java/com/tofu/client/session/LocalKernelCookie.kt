package com.tofu.client.session

import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.runtime.LaunchProtocol
import okhttp3.Cookie
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import java.math.BigInteger

/**
 * The local kernel's WebView credential (launch contract §6,
 * docs/ANDROID_LOCAL_KERNEL.md): the HttpOnly cookie `tofu_session=<install
 * secret>` for host 127.0.0.1, installed before the first page load. Never
 * `/?token=`.
 *
 * Cookies are scoped by host and ignore the port (RFC 6265 §8.5), so that host
 * belongs to this one kernel, and no other kernel can ever be sent the secret:
 * - [install] writes it for 127.0.0.1 only and refuses any other endpoint;
 * - no URL kernel may use that host ([isReservedHost], refused by
 *   [SessionManager] and [ProfileForm]) however it is spelled: the WebView
 *   reads `127.1`, `2130706433` and `0x7f.1` as 127.0.0.1;
 * - the WebView sends nothing to 127.0.0.1 except the front LOCAL kernel's own
 *   origin ([webViewMayLoad], enforced by [ReauthWebViewClient]), so no page
 *   can redirect it, cookie and all, to another listener on another port;
 * - SSH kernels forward on 127.0.0.2 (docs/ANDROID_KERNELS.md, security rule 4).
 */
object LocalKernelCookie {
    /** The server's session cookie (`routes/api_v1/auth.py`). */
    const val NAME = "tofu_session"

    /** The only host the credential is ever written for: the kernel's loopback bind. */
    const val HOST = LaunchProtocol.LOOPBACK

    /** Host-only (no Domain), path `/`, HttpOnly; [CookieHeaders] adds SameSite=Lax and the expiry. */
    fun build(secret: String): Cookie =
        Cookie.Builder().name(NAME).value(secret).hostOnlyDomain(HOST).path("/").httpOnly().build()

    /** Write the credential for [endpoint], which must be the local kernel's `http://127.0.0.1:<port>/`. */
    fun install(cookies: CookieSink, endpoint: KernelEndpoint, secret: String) {
        val url = endpoint.baseUrl.toHttpUrlOrNull()
        require(url != null && url.scheme == "http" && url.host == HOST && endpoint.cookieHost == HOST) {
            "the local kernel's credential is written for http://$HOST only"
        }
        cookies.inject("http://$HOST:${url.port}", listOf(build(secret)))
    }

    /**
     * Remove the credential once the kernel at [endpoint] is no longer ready: a
     * WebView still showing it keeps requesting its port, which any app may
     * bind once the kernel has exited. Also drops that origin's web storage,
     * which no later launch reuses (each launch has its own port).
     */
    fun withdraw(cookies: CookieSink, endpoint: KernelEndpoint) {
        val url = endpoint.baseUrl.toHttpUrlOrNull() ?: return
        cookies.purgeOrigin("http://$HOST:${url.port}", endpoint.baseUrl)
    }

    /** True when [host] (as [ServerUrl] or the WebView reports it) is 127.0.0.1 in any IPv4 spelling. */
    fun isReservedHost(host: String): Boolean = ipv4(host.lowercase()) == LOOPBACK_ADDRESS

    /** [isReservedHost] for a URL; false for anything that is not an absolute http(s) URL. */
    fun isReservedUrl(url: String): Boolean = url.toHttpUrlOrNull()?.let { isReservedHost(it.host) } == true

    /**
     * Whether the WebView showing [front] may send a request to [url]. Only
     * 127.0.0.1 is restricted: to the front LOCAL kernel's own origin.
     */
    fun webViewMayLoad(front: Profile, url: String): Boolean {
        val target = url.toHttpUrlOrNull() ?: return true
        if (!isReservedHost(target.host)) return true
        if (front.kind != KernelKind.LOCAL) return false
        val own = front.baseUrl.toHttpUrlOrNull() ?: return false
        return target.scheme == own.scheme && target.port == own.port
    }

    /** User-facing refusal of a URL kernel on the reserved host. */
    const val RESERVED_HOST_MESSAGE =
        "$HOST belongs to This phone's own kernel. For another server on this device, use http://localhost:<port>/ instead."

    private const val LOOPBACK_ADDRESS = 0x7F00_0001L

    /**
     * The WHATWG URL host parser's IPv4 rules, as the WebView applies them: a
     * host whose last label is a number is an IPv4 address of one to four
     * decimal, octal (`0` prefix) or hex (`0x` prefix) parts, the last part
     * filling the remaining bytes. Null for a domain or an invalid address.
     */
    internal fun ipv4(host: String): Long? {
        val parts = host.split('.').toMutableList()
        if (parts.size > 1 && parts.last().isEmpty()) parts.removeAt(parts.lastIndex)
        val last = parts.last()
        if (!(last.isNotEmpty() && last.all { it in '0'..'9' }) && ipv4Number(last) == null) return null
        if (parts.size > 4) return null
        val numbers = parts.map { ipv4Number(it) ?: return null }
        if (numbers.dropLast(1).any { it > BigInteger.valueOf(255) }) return null
        if (numbers.last() >= BigInteger.valueOf(256).pow(5 - numbers.size)) return null
        var address = numbers.last().toLong()
        numbers.dropLast(1).forEachIndexed { index, part -> address += part.toLong() shl (8 * (3 - index)) }
        return address
    }

    private fun ipv4Number(part: String): BigInteger? {
        if (part.isEmpty()) return null
        val (digits, radix) = when {
            part.length >= 2 && (part.startsWith("0x") || part.startsWith("0X")) -> part.substring(2) to 16
            part.length >= 2 && part.startsWith("0") -> part.substring(1) to 8
            else -> part to 10
        }
        if (digits.isEmpty()) return BigInteger.ZERO
        val alphabet = "0123456789abcdef".substring(0, radix)
        if (!digits.lowercase().all { it in alphabet }) return null
        return BigInteger(digits, radix)
    }
}
