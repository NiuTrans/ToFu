package com.tofu.client.session

import okhttp3.Call
import okhttp3.EventListener
import okhttp3.Handshake
import okhttp3.Cookie
import okhttp3.OkHttpClient
import okhttp3.Response
import java.io.IOException
import java.util.concurrent.atomic.AtomicLong

/** Request-stage evidence without request/response bodies or credential header values. */
object DiagnosticHttp {
    private val nextId = AtomicLong()
    fun attach(http: OkHttpClient, log: DiagLog?, tag: String): OkHttpClient {
        if (log == null) return http
        val original = http.eventListenerFactory
        return http.newBuilder().eventListenerFactory { call ->
            // Preserve existing instrumentation while owning our bounded evidence.
            val prior = original.create(call)
            object : EventListener() {
                private val id = nextId.incrementAndGet()
                private val started = System.nanoTime()
                private fun elapsed() = (System.nanoTime() - started) / 1_000_000
                private fun record(level: String, text: String) = log.record(level, tag, "http#$id $text")
                override fun callStart(call: Call) { prior.callStart(call); record("I", "${call.request().method} ${ClientReport.safeUrl(call.request().url.toString())}") }
                override fun dnsStart(call: Call, domainName: String) { prior.dnsStart(call, domainName); record("D", "dns $domainName") }
                override fun secureConnectEnd(call: Call, handshake: Handshake?) { prior.secureConnectEnd(call, handshake); record("D", "tls=${handshake?.tlsVersion} cipher=${handshake?.cipherSuite}") }
                override fun requestHeadersEnd(call: Call, request: okhttp3.Request) { prior.requestHeadersEnd(call, request); record("D", "cookies=${!request.header("Cookie").isNullOrBlank()}") }
                override fun responseHeadersEnd(call: Call, response: Response) {
                    prior.responseHeadersEnd(call, response)
                    val location = response.header("Location")?.let { response.request.url.resolve(it) }?.let { ClientReport.safeUrl(it.toString()) }
                    val scopes = response.headers("Set-Cookie").take(8).map { raw ->
                        Cookie.parse(response.request.url, raw)?.let { cookie ->
                            val sameSite = raw.split(';').firstOrNull { it.trim().startsWith("SameSite=", ignoreCase = true) }
                                ?.substringAfter('=')?.trim()?.lowercase()?.takeIf { it in setOf("none", "lax", "strict") } ?: "unspecified"
                            val scopeUrl = response.request.url.newBuilder().host(cookie.domain).encodedPath(cookie.path).query(null).fragment(null).build()
                            "${cookie.name} scope=${ClientReport.safeUrl(scopeUrl.toString())} secure=${cookie.secure} httpOnly=${cookie.httpOnly} sameSite=$sameSite expiresAt=${cookie.expiresAt}"
                        } ?: "rejected cookie header"
                    }
                    record(if (response.code >= 400) "W" else "I", "status=${response.code} type=${response.header("Content-Type").orEmpty().take(80)} redirect=${location ?: "none"} requestId=${response.header("X-Request-Id").orEmpty().take(96)} cookieScopes=$scopes bodyBytes=${response.body?.contentLength()} elapsedMs=${elapsed()}")
                }
                override fun proxySelectStart(call: Call, url: okhttp3.HttpUrl) { prior.proxySelectStart(call, url) }
                override fun proxySelectEnd(call: Call, url: okhttp3.HttpUrl, proxies: List<java.net.Proxy>) { prior.proxySelectEnd(call, url, proxies) }
                override fun dnsEnd(call: Call, domainName: String, inetAddressList: List<java.net.InetAddress>) { prior.dnsEnd(call, domainName, inetAddressList) }
                override fun connectStart(call: Call, inetSocketAddress: java.net.InetSocketAddress, proxy: java.net.Proxy) { prior.connectStart(call, inetSocketAddress, proxy); record("D", "connect port=${inetSocketAddress.port} proxyType=${proxy.type()}") }
                override fun secureConnectStart(call: Call) { prior.secureConnectStart(call); record("D", "tls handshake") }
                override fun connectEnd(call: Call, inetSocketAddress: java.net.InetSocketAddress, proxy: java.net.Proxy, protocol: okhttp3.Protocol?) { prior.connectEnd(call, inetSocketAddress, proxy, protocol) }
                override fun connectFailed(call: Call, inetSocketAddress: java.net.InetSocketAddress, proxy: java.net.Proxy, protocol: okhttp3.Protocol?, ioe: IOException) { prior.connectFailed(call, inetSocketAddress, proxy, protocol, ioe); record("E", "connect failed=${ioe.javaClass.simpleName} reason=${ioe.message}") }
                override fun connectionAcquired(call: Call, connection: okhttp3.Connection) { prior.connectionAcquired(call, connection) }
                override fun connectionReleased(call: Call, connection: okhttp3.Connection) { prior.connectionReleased(call, connection) }
                override fun requestHeadersStart(call: Call) { prior.requestHeadersStart(call) }
                override fun requestBodyStart(call: Call) { prior.requestBodyStart(call) }
                override fun requestBodyEnd(call: Call, byteCount: Long) { prior.requestBodyEnd(call, byteCount) }
                override fun requestFailed(call: Call, ioe: IOException) { prior.requestFailed(call, ioe); record("E", "request failed=${ioe.javaClass.simpleName} reason=${ioe.message}") }
                override fun responseHeadersStart(call: Call) { prior.responseHeadersStart(call) }
                override fun responseBodyStart(call: Call) { prior.responseBodyStart(call) }
                override fun responseBodyEnd(call: Call, byteCount: Long) { prior.responseBodyEnd(call, byteCount) }
                override fun responseFailed(call: Call, ioe: IOException) { prior.responseFailed(call, ioe); record("E", "response failed=${ioe.javaClass.simpleName} reason=${ioe.message}") }
                override fun satisfactionFailure(call: Call, response: Response) { prior.satisfactionFailure(call, response) }
                override fun cacheHit(call: Call, response: Response) { prior.cacheHit(call, response) }
                override fun cacheMiss(call: Call) { prior.cacheMiss(call) }
                override fun cacheConditionalHit(call: Call, response: Response) { prior.cacheConditionalHit(call, response) }
                override fun callEnd(call: Call) { prior.callEnd(call); record("D", "complete elapsedMs=${elapsed()}") }
                override fun callFailed(call: Call, ioe: IOException) { prior.callFailed(call, ioe); record("E", "failed=${ioe.javaClass.simpleName} reason=${ioe.message} elapsedMs=${elapsed()}") }
                override fun canceled(call: Call) { prior.canceled(call); record("W", "cancelled elapsedMs=${elapsed()}") }
            }
        }.build()
    }
}
