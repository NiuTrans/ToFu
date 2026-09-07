package com.tofu.client.ui

import android.Manifest
import android.annotation.SuppressLint
import android.app.DownloadManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.net.http.SslError
import android.os.Environment
import android.os.Message
import android.provider.MediaStore
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.SslErrorHandler
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.FileProvider
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.tween
import androidx.compose.animation.fadeOut
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BugReport
import androidx.compose.material.icons.filled.Close
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.Refresh
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.TextButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.platform.LocalLifecycleOwner
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import com.tofu.client.BuildConfig
import com.tofu.client.data.Profile
import com.tofu.client.session.LoginResult
import com.tofu.client.session.ReauthWebViewClient
import com.tofu.client.session.SessionManager
import com.tofu.client.ui.theme.TofuButtonShape
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

import java.io.File

/**
 * Hosts the Tofu SPA in a WebView for one active [profile].
 *
 * [ReauthWebViewClient] handles session-expiry re-auth (silent re-login on a
 * redirect-to-login / 401) and renderer-death recovery (returns to the profile
 * list instead of stranding a dead blank WebView). JS console output and load
 * errors are mirrored to logcat (remote-debuggable via chrome://inspect) — the
 * on-screen diagnostic overlay used during the blank-screen investigation has
 * been removed now that the viewport-height root cause is fixed server-side.
 *
 * A small floating Refresh button provides the reload affordance a WebView
 * shell otherwise lacks (unlike Chrome, there is no address bar / menu). It
 * replaces the removed pull-to-refresh, which was unusable here: the SPA keeps
 * html/body overflow:hidden and scrolls an INNER div, so SwipeRefreshLayout's
 * scrollY-based "am I at the top?" check always read 0 and every pull-down
 * reloaded mid-chat. A button is always available regardless of the SPA's inner
 * scroll position and hijacks no touch gesture.
 *
 * Voice input: the SPA's mic button calls getUserMedia(). A WebView denies that
 * by default, so [WebChromeClient.onPermissionRequest] must explicitly grant the
 * web-origin audio capture — AND the app must hold the runtime RECORD_AUDIO
 * permission (dangerous, so requested on first use via micLauncher). The
 * manifest permission alone is insufficient for either gate.
 */
/**
 * JS→native bridge for the one-click diagnostics FAB. The web collector
 * (static/js/diag_collect.js → window.__tofuCollectDiagnostics) is async (it
 * runs a live GET probe), so evaluateJavascript's synchronous return can't
 * capture its result. Instead the FAB invokes the collector and pipes its
 * resolved JSON string back through [onResult] via this @JavascriptInterface.
 * Copying to the clipboard + the Toast happen on the native side so they work
 * even when the SPA is wedged on the loading skeleton (the failure we diagnose).
 */
private class DiagBridge(val onResult: (String) -> Unit) {
    @JavascriptInterface
    fun deliver(json: String) { onResult(json) }
}


/**
 * JS→native bridge for the SPA's native-shell contract
 * (frontend/src/core/native-bridge.ts): the page calls
 * `window.TofuNative.requestReauth(reason)` when its API transport observes
 * the OUTER gateway's bare 401 — the edge session died while the page stayed
 * open, and only the shell can re-login headlessly. Routed to the
 * WebViewClient's latch, which runs the same bounded headless re-login as an
 * in-WebView trigger; the JS side rate-limits bursts, the client caps
 * consecutive failures.
 */
private class NativeBridge(val onReauth: (String) -> Unit) {
    @JavascriptInterface
    fun requestReauth(reason: String) { onReauth(reason) }
}

/** Copy [text] to the system clipboard and show a short confirmation Toast. */
private fun copyToClipboard(ctx: Context, text: String) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText("Tofu diagnostics", text))
    Toast.makeText(ctx, "Diagnostics copied — paste to the maintainer", Toast.LENGTH_LONG).show()
}


/** Open [uri] in the system browser / handler app. */
private fun openExternal(ctx: Context, uri: Uri) {
    try {
        ctx.startActivity(Intent(Intent.ACTION_VIEW, uri))
    } catch (e: Exception) {
        Log.w("TofuWebScreen", "no handler for $uri: ${e.message}")
        Toast.makeText(ctx, "No app can open this link", Toast.LENGTH_SHORT).show()
    }
}

/**
 * Hand a WebView download to the system DownloadManager, forwarding the
 * session cookies — without them the gateway answers 401 and the "download"
 * saves the login page. The filename comes from Content-Disposition, falling
 * back to the URL path.
 */
private fun downloadToSystem(
    ctx: Context,
    url: String,
    contentDisposition: String?,
    mimeType: String?,
) {
    val fileName = URLUtil.guessFileName(url, contentDisposition, mimeType)
    try {
        val request = DownloadManager.Request(Uri.parse(url))
            .setTitle(fileName)
            .setMimeType(mimeType ?: "application/octet-stream")
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName)
            .setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED,
            )
        val cookies = CookieManager.getInstance().getCookie(url)
        if (!cookies.isNullOrBlank()) request.addRequestHeader("Cookie", cookies)
        (ctx.getSystemService(Context.DOWNLOAD_SERVICE) as DownloadManager).enqueue(request)
        Toast.makeText(ctx, "Downloading $fileName", Toast.LENGTH_SHORT).show()
    } catch (e: Exception) {
        Log.w("TofuWebScreen", "download failed for $url: ${e.message}")
        Toast.makeText(ctx, "Download failed: ${e.message}", Toast.LENGTH_LONG).show()
    }
}

/** Accept tokens under which offering the camera makes sense. */
private val IMAGE_ACCEPT_TOKENS = setOf(
    "*/*", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic",
)

/**
 * Wrap the system picker in a chooser that also offers "take a photo" — a
 * phone's natural attachment is often a fresh picture. The capture lands at a
 * FileProvider uri parked in [pendingCameraUri]; the picker-result callback
 * maps an OK-with-no-data result onto it. Returns null when the camera can't
 * be staged (no cache dir / provider missing), in which case the plain picker
 * intent is used instead.
 */
private fun buildChooserWithCamera(
    ctx: Context,
    picker: Intent,
    pendingCameraUri: Array<Uri?>,
): Intent? {
    val photoFile = try {
        File.createTempFile("tofu_capture_", ".jpg", ctx.cacheDir)
    } catch (e: Exception) {
        Log.w("TofuFileChooser", "cannot stage capture file: ${e.message}")
        return null
    }
    val uri = try {
        FileProvider.getUriForFile(ctx, "${ctx.packageName}.fileprovider", photoFile)
    } catch (e: Exception) {
        Log.w("TofuFileChooser", "FileProvider unavailable: ${e.message}")
        return null
    }
    val capture = Intent(MediaStore.ACTION_IMAGE_CAPTURE)
        .putExtra(MediaStore.EXTRA_OUTPUT, uri)
        .addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION or Intent.FLAG_GRANT_READ_URI_PERMISSION)
    // Choosers propagate grants via ClipData, not intent flags, on many OEMs.
    capture.clipData = ClipData.newUri(ctx.contentResolver, "capture", uri)
    pendingCameraUri[0] = uri
    return Intent.createChooser(picker, null).putExtra(
        Intent.EXTRA_INITIAL_INTENTS, arrayOf(capture),
    )
}

/**
 * Fire the web collector and route its async result to the native clipboard.
 * The collector returns a Promise<string>; we resolve it in-page and hand the
 * string to the injected `TofuDiag.deliver(...)` bridge. If the collector is
 * missing (old web build) or errors, we still copy a helpful marker so the
 * user's tap is never a silent no-op.
 */
private fun collectAndCopyDiagnostics(wv: WebView?) {
    if (wv == null) return
    val js = """
        (function(){
          try {
            if (typeof window.__tofuCollectDiagnostics !== 'function') {
              TofuDiag.deliver('{"error":"diagnostics collector missing — web build predates diag_collect.js; Refresh once on a newer server build"}');
              return;
            }
            Promise.resolve(window.__tofuCollectDiagnostics()).then(
              function(s){ TofuDiag.deliver(String(s)); },
              function(e){ TofuDiag.deliver('{"error":"collector rejected: '+(e&&e.message||e)+'"}'); }
            );
          } catch (e) {
            TofuDiag.deliver('{"error":"collector threw: '+(e&&e.message||e)+'"}');
          }
        })();
    """.trimIndent()
    wv.evaluateJavascript(js, null)
}

@SuppressLint("SetJavaScriptEnabled")
@Composable
fun WebScreen(
    profile: Profile,
    session: SessionManager,
    scope: CoroutineScope,
    onBack: () -> Unit,
) {
    val webRef = remember { arrayOfNulls<WebView>(1) }

    // Load progress (0..100) from the chrome client. A WebView shell has no
    // browser chrome, so without this the screen is blank-then-content with no
    // feedback — indistinguishable from the white-screen failure mode we hit on
    // the Shanghai server. The overlay hides once the first paint lands.
    var progress by remember(profile.id) { mutableIntStateOf(0) }
    var firstLoadDone by remember(profile.id) { mutableStateOf(false) }


    // A parked TLS failure from ReauthWebViewClient, awaiting the user's
    // proceed-once / cancel decision. NEVER auto-proceeded — the app holds
    // credentials, so a silent proceed would make a hostile network
    // indistinguishable from the self-hosted server it protects.
    var sslErrorState by remember {
        mutableStateOf<Pair<SslErrorHandler, String>?>(null)
    }

    // A main-frame load failure parked by ReauthWebViewClient (transport error
    // or the proxy's 5xx page while the sandbox wakes). The WebView's built-in
    // error page is a dead end on a phone — no address bar, no reload affordance
    // — so the reason is shown on a native cover with an explicit Retry button.
    var loadFailure by remember(profile.id) { mutableStateOf<String?>(null) }

    // A getUserMedia() request that arrived before the runtime RECORD_AUDIO
    // permission was granted, parked here until micLauncher returns a result.
    val pendingMicRequest = remember { arrayOfNulls<PermissionRequest>(1) }
    val micLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted ->
        val req = pendingMicRequest[0]
        pendingMicRequest[0] = null
        if (req != null) {
            if (granted) req.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
            else req.deny()
        }
    }

    // The SPA's "+" attach button triggers <input type="file">.click(). A
    // WebView does NOT open a system picker for that on its own — the host must
    // implement onShowFileChooser and launch an intent itself. The pending
    // ValueCallback is parked here until the picker returns, mirroring the mic
    // flow above. CRITICAL: the callback MUST be invoked exactly once (with the
    // selected URIs, or null on cancel); leaving it pending permanently wedges
    // the <input> so it can never reopen.
    val pendingFileCallback = remember { arrayOfNulls<ValueCallback<Array<Uri>>>(1) }
    // FileProvider uri of an in-flight camera capture offered alongside the
    // picker (see buildChooserWithCamera); consumed below on OK-with-no-data.
    val pendingCameraUri = remember { arrayOfNulls<Uri>(1) }
    val fileChooserLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val cb = pendingFileCallback[0]
        pendingFileCallback[0] = null
        val cameraUri = pendingCameraUri[0]
        pendingCameraUri[0] = null
        if (cb != null) {
            val uris = if (result.resultCode == android.app.Activity.RESULT_OK) {
                val data = result.data
                val clip = data?.clipData
                when {
                    // Multi-select: the picker returns the URIs in the Intent's
                    // ClipData, NOT in getData(). The framework's parseResult()
                    // only ever reads getData(), so it silently drops all but one
                    // file on a multi-selection — extract ClipData ourselves and
                    // fall back to parseResult() for the single-file case.
                    clip != null -> Array(clip.itemCount) { clip.getItemAt(it).uri }
                    // Camera capture: OK with NO Intent payload — the photo
                    // sits at the FileProvider uri minted for the chooser.
                    data == null && cameraUri != null -> arrayOf(cameraUri)
                    else -> WebChromeClient.FileChooserParams.parseResult(
                        result.resultCode, data,
                    )
                }
            } else {
                null
            }
            cb.onReceiveValue(uris)
        }
    }

    // Fold the app lifecycle into the SPA's budget layers: a WebView never
    // flips document.visibilityState when the app backgrounds, so without this
    // signal every foreground-cadence poller (push ping, catalog reconcile,
    // inspector polling, elapsed tickers) keeps hammering the vscode tunnel
    // from the user's pocket. The event contract lives in
    // frontend/src/core/native-bridge.ts; budget layers fold it with
    // document.hidden into one effective-hidden predicate.
    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner) {
        val observer = LifecycleEventObserver { _, event ->
            val hidden = when (event) {
                Lifecycle.Event.ON_STOP -> true
                Lifecycle.Event.ON_START -> false
                else -> return@LifecycleEventObserver
            }
            webRef[0]?.evaluateJavascript(
                "(function(){try{document.dispatchEvent(new CustomEvent(" +
                    "'tofu:native-visibility',{detail:{hidden:$hidden}}));}catch(e){}})()",
                null,
            )
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }
    BackHandler {
        val wv = webRef[0]
        if (wv != null && wv.canGoBack()) wv.goBack() else onBack()
    }

    Box(Modifier.fillMaxSize()) {
        AndroidView(
            // The activity is edge-to-edge, so without a bottom inset the
            // gesture-nav bar overlaps the SPA's composer — the one control the
            // user needs most. The status bar is NOT inset: the SPA paints its
            // own dark header there, which reads better full-bleed.
            modifier = Modifier.fillMaxSize().navigationBarsPadding(),
            factory = { ctx ->
                WebView(ctx).apply {
                    webRef[0] = this
                    // Remote-debuggable via chrome://inspect on a connected
                    // desktop — DEBUG BUILDS ONLY. In a release build this would
                    // let any connected desktop inspect (and drive) a WebView
                    // holding live session credentials.
                    WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG)
                    // JS→native bridge for the diagnostics FAB. The collected
                    // JSON is copied to the clipboard natively so it works even
                    // when the SPA is wedged. Exposed as window.TofuDiag.
                    addJavascriptInterface(
                        DiagBridge { json ->
                            scope.launch(Dispatchers.Main) { copyToClipboard(ctx, json) }
                        },
                        "TofuDiag",
                    )

                    // The SPA's native-shell bridge (see NativeBridge): exposes
                    // window.TofuNative.requestReauth so the page can ask for a
                    // headless re-login when the gateway session dies under it.
                    addJavascriptInterface(
                        NativeBridge { reason ->
                            scope.launch(Dispatchers.Main) {
                                (this@apply.webViewClient as? ReauthWebViewClient)
                                    ?.requestReauth(this@apply, reason)
                            }
                        },
                        "TofuNative",
                    )
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true        // Tofu uses localStorage/IndexedDB
                    settings.databaseEnabled = true
                    settings.cacheMode = android.webkit.WebSettings.LOAD_DEFAULT

                    // Download links (export buttons, log downloads) are dead in
                    // a bare WebView — hand them to DownloadManager WITH the
                    // session cookies, or the gateway 401s the fetch.
                    setDownloadListener { url, _, contentDisposition, mimeType, _ ->
                        downloadToSystem(ctx, url, contentDisposition, mimeType)
                    }
                    // Honor the SPA's <meta viewport width=device-width> exactly
                    // like Chrome. Without useWideViewPort the WebView ignores
                    // the meta and lays out at the raw control width, so the
                    // computed innerWidth lands on the wrong side of the SPA's
                    // 768/1024 responsive breakpoints (core.js TOFU_BP) and the
                    // tablet renders a different layout than Chrome on the same
                    // device.
                    //
                    // NOTE: loadWithOverviewMode was tried alongside this in
                    // v0.1.3 but is deliberately NOT set — it forces a
                    // zoom-to-fit initial layout that can collapse the page to
                    // ~0 height (the "flash-then-blank" / black-line regression).
                    // useWideViewPort alone delivers the Chrome-parity width.
                    settings.useWideViewPort = true
                    val cm = CookieManager.getInstance()
                    cm.setAcceptCookie(true)
                    cm.setAcceptThirdPartyCookies(this, true) // gateway host != Tofu host

                    // Mirror JS console output to logcat (tag TofuWebConsole)
                    // and bridge the SPA's mic (getUserMedia) request to the
                    // app's runtime RECORD_AUDIO permission.
                    webChromeClient = object : WebChromeClient() {
                        override fun onProgressChanged(view: WebView, newProgress: Int) {
                            progress = newProgress
                        }

                        override fun onConsoleMessage(m: ConsoleMessage): Boolean {
                            Log.i(
                                "TofuWebConsole",
                                "[${m.messageLevel()}] ${m.message()} " +
                                    "(${m.sourceId()}:${m.lineNumber()})",
                            )
                            return true
                        }


                        // The SPA calls window.open() for outbound links (docs,
                        // issue trackers, OAuth providers). A WebView has no
                        // second window — the default is to silently DROP the
                        // navigation, a dead link with zero feedback. Route it
                        // to the system browser instead.
                        override fun onCreateWindow(
                            view: WebView,
                            isDialog: Boolean,
                            isUserGesture: Boolean,
                            resultMsg: Message,
                        ): Boolean {
                            val hitTest = view.hitTestResult
                            val url = hitTest?.extra
                            if (url != null) openExternal(view.context, Uri.parse(url))
                            return false
                        }

                        override fun onShowFileChooser(
                            webView: WebView,
                            filePathCallback: ValueCallback<Array<Uri>>,
                            fileChooserParams: FileChooserParams,
                        ): Boolean {
                            // Discard any stale callback from a picker that was
                            // never resolved (defensive — should not happen).
                            pendingFileCallback[0]?.onReceiveValue(null)
                            pendingFileCallback[0] = filePathCallback
                            val intent = try {
                                fileChooserParams.createIntent()
                            } catch (e: Exception) {
                                Log.w("TofuFileChooser", "createIntent failed: ${e.message}")
                                null
                            }
                            if (intent == null) {
                                pendingFileCallback[0] = null
                                return false
                            }
                            // Honor the SPA input's `multiple` attribute.
                            if (fileChooserParams.mode ==
                                FileChooserParams.MODE_OPEN_MULTIPLE
                            ) {
                                intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                            }
                            // The SPA's #fileInput `accept` mixes dozens of bare
                            // extensions (.pdf/.docx/.py/.md…) that are NOT valid
                            // MIME types. createIntent() copies acceptTypes into
                            // intent.type verbatim; some OEM pickers, handed a
                            // token they can't parse, filter the list down to
                            // images-only or to nothing — the window opens but
                            // no PDF/doc is selectable. So when any accept token
                            // is non-standard, widen intent.type to */* and pass
                            // the *valid* MIME hints via EXTRA_MIME_TYPES (capable
                            // pickers still narrow; broken ones show everything).
                            val acceptTypes = fileChooserParams.acceptTypes
                                ?.filter { it.isNotBlank() }
                                .orEmpty()
                            val hasNonStandard = acceptTypes.any { !it.contains('/') }
                            if (hasNonStandard) {
                                intent.type = "*/*"
                                val mimeHints = acceptTypes
                                    .filter { it.contains('/') }
                                    .toTypedArray()
                                if (mimeHints.isNotEmpty()) {
                                    intent.putExtra(Intent.EXTRA_MIME_TYPES, mimeHints)
                                }
                            }
                            // Offer the camera alongside the picker when images
                            // are acceptable — a phone's natural attachment is
                            // often a fresh photo (whiteboard, error dialog).
                            val wantsImages = acceptTypes.isEmpty() || acceptTypes.any {
                                val t = it.lowercase()
                                t.startsWith("image/") || t in IMAGE_ACCEPT_TOKENS
                            }
                            val launchIntent = if (wantsImages) {
                                buildChooserWithCamera(ctx, intent, pendingCameraUri) ?: intent
                            } else {
                                intent
                            }
                            return try {
                                fileChooserLauncher.launch(launchIntent)
                                true
                            } catch (e: Exception) {
                                Log.w("TofuFileChooser", "launch failed: ${e.message}")
                                pendingFileCallback[0] = null
                                pendingCameraUri[0] = null
                                filePathCallback.onReceiveValue(null)
                                false
                            }
                        }

                        override fun onPermissionRequest(request: PermissionRequest) {
                            val wantsAudio = request.resources.any {
                                it == PermissionRequest.RESOURCE_AUDIO_CAPTURE
                            }
                            // Only the mic is bridged; deny anything else
                            // (camera, protected media) the shell doesn't need.
                            if (!wantsAudio) {
                                request.deny()
                                return
                            }
                            val held = ctx.checkSelfPermission(
                                Manifest.permission.RECORD_AUDIO,
                            ) == PackageManager.PERMISSION_GRANTED
                            if (held) {
                                request.grant(arrayOf(PermissionRequest.RESOURCE_AUDIO_CAPTURE))
                            } else {
                                // Park the request and prompt; micLauncher's
                                // callback grants or denies once the user decides.
                                pendingMicRequest[0] = request
                                micLauncher.launch(Manifest.permission.RECORD_AUDIO)
                            }
                        }
                    }

                    val client = ReauthWebViewClient(
                        profile,
                        onReauth = { view ->
                            scope.launch(Dispatchers.Main) {
                                val result = session.login(profile)
                                if (result is LoginResult.Success) view.reload()
                                (view.webViewClient as? ReauthWebViewClient)
                                    ?.reauthSettled(result is LoginResult.Success)
                            }
                        },
                        onRendererGone = {
                            // Renderer died (crash / OOM) → the page is a dead
                            // blank surface; return to the profile list.
                            scope.launch(Dispatchers.Main) { onBack() }
                        },
                        onPageDone = { view, url ->
                            firstLoadDone = true
                            // An INTERACTIVE_SSO sign-in completes INSIDE the
                            // WebView, so no OkHttp response ever stamps
                            // cookieHost. Without this the profile stays
                            // "not signed in" forever and the supervisor's
                            // Start/Stop can never be used, no matter how many
                            // times the user signs in. Idempotent + guarded on
                            // landing back on our own host with a real cookie.
                            scope.launch {
                                session.noteInteractiveSignIn(profile, url)
                            }
                            // Viewport-parity probe: log the WebView's computed
                            // layout width + DPR so breakpoint agreement with
                            // Chrome (SPA TOFU_BP 768/1024, core.js) can be
                            // VERIFIED from logcat on a real device rather than
                            // assumed from useWideViewPort alone. Tag: TofuViewport.
                            view.evaluateJavascript(
                                "(function(){try{return JSON.stringify({" +
                                    "innerWidth:window.innerWidth," +
                                    "dpr:window.devicePixelRatio," +
                                    "screenW:window.screen&&window.screen.width," +
                                    "band:(window.innerWidth<=768?'mobile':" +
                                    "(window.innerWidth<=1024?'tablet':'desktop'))" +
                                    "});}catch(e){return 'probe-error:'+e;}})()",
                            ) { r -> Log.i("TofuViewport", "viewport=$r") }
                        },
                        onReauthExhausted = {
                            // The session can no longer be re-established
                            // headlessly (password changed / gateway down) —
                            // drop to the profile list, which has the controls
                            // and copy to fix it, instead of leaving a page
                            // that 401s forever.
                            scope.launch(Dispatchers.Main) {
                                Toast.makeText(
                                    ctx,
                                    "Session expired — sign in again from the server list",
                                    Toast.LENGTH_LONG,
                                ).show()
                                onBack()
                            }
                        },
                        onSslError = { handler, error ->
                            sslErrorState = handler to error.toString()
                        },
                        onExternalUrl = { uri -> openExternal(ctx, uri) },
                        onMainFrameFailure = { reason ->
                            scope.launch(Dispatchers.Main) { loadFailure = reason }
                        },
                    )
                    webViewClient = client
                    loadUrl(profile.baseUrl)
                }
            },
        )

        // Branded first-load cover. Without it the user stares at a white
        // rectangle while the SPA boots, which is indistinguishable from the
        // white-screen FAILURE mode — so a slow server read as a broken app.
        // Covers only the FIRST load; later navigations use the thin bar below.
        AnimatedVisibility(
            visible = !firstLoadDone,
            exit = fadeOut(animationSpec = tween(220)),
        ) {
            LoadingCover(profile.alias, progress)
        }

        // Thin determinate progress line for subsequent navigations/reloads —
        // the one piece of browser chrome a shell genuinely needs.
        if (firstLoadDone && progress in 1..99) {
            LinearProgressIndicator(
                progress = { progress / 100f },
                modifier = Modifier
                    .align(Alignment.TopCenter)
                    .statusBarsPadding()
                    .fillMaxWidth(),
                color = MaterialTheme.colorScheme.primary,
                trackColor = Color.Transparent,
                strokeCap = StrokeCap.Butt,
            )
        }

        // Main-frame failure cover: the WebView's own error page is a dead end
        // on a phone, so failures park here with the reason and a real Retry
        // button. Retry clears the cover and reloads; if the sandbox is still
        // waking the client re-parks the failure and the cover returns.
        loadFailure?.let { reason ->
            LoadFailureCover(
                alias = profile.alias,
                reason = reason,
                onRetry = {
                    loadFailure = null
                    webRef[0]?.reload()
                },
                onBackToServers = onBack,
            )
        }

        // Affordances the WebView shell otherwise lacks (no address bar/menu).
        // Collapsed to a SINGLE handle by default: two permanent FABs sat on top
        // of the SPA's own controls, so the shell's debug affordances were
        // competing with the product's UI. Tap to reveal Reload + Diagnostics.
        var toolsOpen by remember { mutableStateOf(false) }
        Column(
            modifier = Modifier
                .align(Alignment.TopEnd)
                .statusBarsPadding()
                .padding(10.dp),
            horizontalAlignment = Alignment.End,
            verticalArrangement = Arrangement.spacedBy(9.dp),
        ) {
            SmallFloatingActionButton(
                onClick = { toolsOpen = !toolsOpen },
                containerColor = MaterialTheme.colorScheme.surface,
                contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
                modifier = Modifier.alpha(if (toolsOpen) 0.95f else 0.4f),
            ) {
                Icon(
                    if (toolsOpen) Icons.Filled.Close else Icons.Filled.MoreVert,
                    contentDescription = if (toolsOpen) "Hide tools" else "Show tools",
                )
            }
            AnimatedVisibility(toolsOpen) {
                Column(
                    horizontalAlignment = Alignment.End,
                    verticalArrangement = Arrangement.spacedBy(9.dp),
                ) {
                    SmallFloatingActionButton(
                        onClick = { webRef[0]?.reload(); toolsOpen = false },
                        containerColor = MaterialTheme.colorScheme.surface,
                        contentColor = MaterialTheme.colorScheme.onSurface,
                    ) {
                        Icon(Icons.Filled.Refresh, contentDescription = "Reload")
                    }
                    SmallFloatingActionButton(
                        onClick = { collectAndCopyDiagnostics(webRef[0]); toolsOpen = false },
                        containerColor = MaterialTheme.colorScheme.surface,
                        contentColor = MaterialTheme.colorScheme.onSurface,
                    ) {
                        Icon(Icons.Filled.BugReport, contentDescription = "Copy diagnostics")
                    }
                }
            }
        }

        // TLS failure parked by ReauthWebViewClient: the USER decides, once.
        sslErrorState?.let { (handler, description) ->
            AlertDialog(
                onDismissRequest = {
                    handler.cancel()
                    sslErrorState = null
                },
                title = { Text("Certificate problem") },
                text = {
                    Text(
                        "The server's certificate failed validation ($description). " +
                            "Proceed only if you trust this network.",
                    )
                },
                confirmButton = {
                    TextButton(onClick = {
                        handler.proceed()
                        sslErrorState = null
                    }) { Text("Proceed once") }
                },
                dismissButton = {
                    TextButton(onClick = {
                        handler.cancel()
                        sslErrorState = null
                    }) { Text("Cancel") }
                },
            )
        }
    }
}

/**
 * Full-bleed cover for a main-frame load that FAILED (transport error or the
 * proxy's 5xx while the sandbox wakes). The WebView's built-in error page has
 * no retry affordance and the shell has no address bar — without this the user
 * is stranded. Deliberately opaque, like the loading cover.
 */
@Composable
private fun LoadFailureCover(
    alias: String,
    reason: String,
    onRetry: () -> Unit,
    onBackToServers: () -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background)
            .padding(32.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        ServerAvatar(alias, size = 60)
        Spacer(Modifier.height(18.dp))
        Text(
            "Couldn't load $alias",
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            reason,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(22.dp))
        androidx.compose.material3.Button(onClick = onRetry, shape = TofuButtonShape) {
            Text("Retry")
        }
        Spacer(Modifier.height(4.dp))
        TextButton(onClick = onBackToServers) {
            Text("Back to servers")
        }
    }
}

/**
 * Full-bleed cover shown until the SPA's first paint: the server's identity
 * tile, its name, and real load progress. Deliberately opaque — it must hide
 * the WebView's white default background, which is the whole point.
 */
@Composable
private fun LoadingCover(alias: String, progress: Int) {
    Column(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        ServerAvatar(alias, size = 60)
        Spacer(Modifier.height(18.dp))
        Text(
            alias,
            style = MaterialTheme.typography.titleMedium,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Spacer(Modifier.height(6.dp))
        Text(
            if (progress > 0) "Loading… $progress%" else "Connecting…",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(22.dp))
        LinearProgressIndicator(
            progress = { (progress / 100f).coerceAtLeast(0.04f) },
            modifier = Modifier.width(160.dp),
            color = MaterialTheme.colorScheme.primary,
            trackColor = MaterialTheme.colorScheme.surfaceVariant,
        )
    }
}
