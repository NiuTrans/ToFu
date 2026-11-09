package com.tofu.client.session

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.suspendCancellableCoroutine
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Response
import okhttp3.ResponseBody.Companion.toResponseBody
import java.io.IOException
import kotlin.coroutines.resumeWithException

/** The native HTTP decoded-body correctness bound, owned by android/README.md. */
internal const val NATIVE_HTTP_RESPONSE_MAX_BYTES = 262_144L

/** Native response boundary: bounded decoded bytes and cancellable socket ownership. */
internal fun Response.readBoundedBody(maxBytes: Long): String? {
    val body = body ?: return ""
    if (body.contentLength() > maxBytes) return null
    val source = body.source()
    source.request(maxBytes + 1)
    if (source.buffer.size > maxBytes) return null
    return source.readUtf8()
}

/** Buffer and close while cancellation still owns the live socket, not after handoff. */
@OptIn(ExperimentalCoroutinesApi::class)
internal suspend fun Call.awaitBounded(maxBytes: Long): Response = suspendCancellableCoroutine { continuation ->
    val call = this
    continuation.invokeOnCancellation { call.cancel() }
    call.enqueue(object : Callback {
        override fun onFailure(call: Call, e: IOException) { continuation.resumeWithException(e) }
        override fun onResponse(call: Call, response: Response) {
            try {
                val text = response.readBoundedBody(maxBytes)
                    ?: throw IOException("Native response exceeds the documented body boundary")
                val bounded = response.newBuilder().body(text.toResponseBody(response.body?.contentType())).build()
                response.close()
                continuation.resume(bounded, onCancellation = { bounded.close() })
            } catch (error: Exception) {
                response.close()
                continuation.resumeWithException(error)
            }
        }
    })
}
