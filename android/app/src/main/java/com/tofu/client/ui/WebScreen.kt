package com.tofu.client.ui

import android.Manifest
import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.util.Log
import android.webkit.ConsoleMessage
import android.webkit.CookieManager
import android.webkit.JavascriptInterface
import android.webkit.PermissionRequest
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.SmallFloatingActionButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
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
import androidx.compose.ui.unit.dp
import androidx.compose.ui.viewinterop.AndroidView
import com.tofu.client.data.Profile
import com.tofu.client.session.LoginResult
import com.tofu.client.session.ReauthWebViewClient
import com.tofu.client.session.SessionManager
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch

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

/** Copy [text] to the system clipboard and show a short confirmation Toast. */
private fun copyToClipboard(ctx: Context, text: String) {
    val cm = ctx.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    cm.setPrimaryClip(ClipData.newPlainText("Tofu diagnostics", text))
    Toast.makeText(ctx, "Diagnostics copied — paste to the maintainer", Toast.LENGTH_LONG).show()
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
    val fileChooserLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) { result ->
        val cb = pendingFileCallback[0]
        pendingFileCallback[0] = null
        if (cb != null) {
            val uris = if (result.resultCode == android.app.Activity.RESULT_OK) {
                val data = result.data
                // Multi-select: the picker returns the URIs in the Intent's
                // ClipData, NOT in getData(). The framework's parseResult()
                // only ever reads getData(), so it silently drops all but one
                // file on a multi-selection — extract ClipData ourselves and
                // fall back to parseResult() for the single-file case.
                val clip = data?.clipData
                if (clip != null) {
                    Array(clip.itemCount) { clip.getItemAt(it).uri }
                } else {
                    WebChromeClient.FileChooserParams.parseResult(result.resultCode, data)
                }
            } else {
                null
            }
            cb.onReceiveValue(uris)
        }
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
                    // desktop — safe to leave on for a self-hosted tool.
                    WebView.setWebContentsDebuggingEnabled(true)
                    // JS→native bridge for the diagnostics FAB. The collected
                    // JSON is copied to the clipboard natively so it works even
                    // when the SPA is wedged. Exposed as window.TofuDiag.
                    addJavascriptInterface(
                        DiagBridge { json ->
                            scope.launch(Dispatchers.Main) { copyToClipboard(ctx, json) }
                        },
                        "TofuDiag",
                    )
                    settings.javaScriptEnabled = true
                    settings.domStorageEnabled = true        // Tofu uses localStorage/IndexedDB
                    settings.databaseEnabled = true
                    settings.cacheMode = android.webkit.WebSettings.LOAD_DEFAULT
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
                            return try {
                                fileChooserLauncher.launch(intent)
                                true
                            } catch (e: Exception) {
                                Log.w("TofuFileChooser", "launch failed: ${e.message}")
                                pendingFileCallback[0] = null
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
                                (view.webViewClient as? ReauthWebViewClient)?.reauthSettled()
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
