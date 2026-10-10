package com.tofu.client.session

import android.util.Log
import android.webkit.WebView
import androidx.webkit.WebViewCompat
import androidx.webkit.WebViewFeature
import androidx.webkit.WebMessageCompat

/**
 * Installs the SPA's native channels only for the configured origin. WebMessage
 * listeners expose the sender origin and frame identity; addJavascriptInterface
 * cannot distinguish IdP pages or third-party frames. Old engines degrade to
 * main-frame reauthentication rather than exposing a privileged Java object.
 */
object TrustedWebBridge {
    fun accepts(baseUrl: String, pageUrl: String?, sourceOrigin: String, mainFrame: Boolean): Boolean {
        if (!mainFrame || pageUrl == null) return false
        val server = ServerUrl.parse(baseUrl) ?: return false
        val source = ServerUrl.parse(sourceOrigin) ?: return false
        val page = ServerUrl.parse(pageUrl) ?: return false
        val basePath = server.httpUrl.encodedPath.trimEnd('/')
        val pagePath = page.httpUrl.encodedPath.trimEnd('/')
        return server.sameOrigin(source.httpUrl) && server.sameOrigin(page.httpUrl) &&
            (pagePath == basePath || pagePath.startsWith("$basePath/"))
    }

    fun install(view: WebView, baseUrl: String, onReauth: (WebView, String) -> Unit, onDiagnostics: (String) -> Unit, diagnostics: DiagLog? = null) {
        if (!WebViewFeature.isFeatureSupported(WebViewFeature.WEB_MESSAGE_LISTENER) ||
            !WebViewFeature.isFeatureSupported(WebViewFeature.DOCUMENT_START_SCRIPT)) {
            diagnostics?.record("W", "WebBridge", "WebView does not support scoped native channels; update WebView")
            Log.w("TofuBridge", "WebView needs updating for native session/diagnostic channels")
            return
        }
        val origin = ServerUrl.parse(baseUrl)?.origin ?: return
        WebViewCompat.addWebMessageListener(view, "TofuNativeChannel", setOf(origin)) { sender, message, source, main, _ ->
            if (message.type == WebMessageCompat.TYPE_STRING && accepts(baseUrl, sender.url, source.toString(), main)) {
                onReauth(sender, message.data.orEmpty().take(512))
            }
        }
        WebViewCompat.addWebMessageListener(view, "TofuDiagChannel", setOf(origin)) { sender, message, source, main, _ ->
            if (message.type == WebMessageCompat.TYPE_STRING && accepts(baseUrl, sender.url, source.toString(), main)) {
                onDiagnostics(message.data.orEmpty().take(1_048_576))
            }
        }
        WebViewCompat.addDocumentStartJavaScript(view, """
            window.TofuNative = {requestReauth: function(reason) {TofuNativeChannel.postMessage(String(reason));}};
            window.TofuDiag = {deliver: function(json) {TofuDiagChannel.postMessage(String(json));}};
        """.trimIndent(), setOf(origin))
    }
}
