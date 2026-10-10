package com.tofu.client

import android.app.Application
import android.app.ActivityManager
import android.content.Context
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import androidx.webkit.WebViewCompat
import androidx.core.content.ContextCompat
import android.content.pm.PackageManager
import com.tofu.client.session.ClientReport
import com.tofu.client.session.DiagLog
import com.tofu.client.session.DiagnosticJournal
import java.io.File

/** Process owner for local support evidence; initialized before Room, vault or any Activity. */
class TofuApplication : Application() {
    lateinit var diagnostics: DiagLog
        private set
    private var priorCrashHandler: Thread.UncaughtExceptionHandler? = null
    private var crashHandler: Thread.UncaughtExceptionHandler? = null
    private lateinit var journal: DiagnosticJournal

    override fun onCreate() {
        super.onCreate()
        val heapBytes = Runtime.getRuntime().maxMemory()
        val capacity = (heapBytes / (1024 * 1024)).toInt().coerceIn(DiagLog.DEFAULT_CAPACITY, ClientReport.MAX_EVENTS)
        val segmentBytes = (heapBytes / 1024).coerceIn(64 * 1024L, 256 * 1024L).toInt()
        journal = DiagnosticJournal(File(filesDir, "diagnostics"), capacity, segmentBytes)
        diagnostics = DiagLog(capacity = capacity, journal = journal)
        val prior = Thread.getDefaultUncaughtExceptionHandler()
        priorCrashHandler = prior
        crashHandler = Thread.UncaughtExceptionHandler { thread, error ->
            try {
                diagnostics.record("E", "AppCrash", "uncaught thread=${thread.name}")
                diagnostics.exception("AppCrash", error)
                diagnostics.flush()
            } catch (_: Throwable) {
                // Only the last-resort crash hook: allocation/I/O may itself fail
                // during OOM. Always preserve Android's original crash handling.
            } finally {
                if (prior != null) prior.uncaughtException(thread, error)
                else { android.os.Process.killProcess(android.os.Process.myPid()); kotlin.system.exitProcess(10) }
            }
        }
        Thread.setDefaultUncaughtExceptionHandler(crashHandler)
        diagnostics.record("I", "App", "process start version=${BuildConfig.VERSION_NAME}/${BuildConfig.VERSION_CODE} buildId=${BuildConfig.DIAGNOSTIC_BUILD_ID} heapBytes=$heapBytes eventCapacity=$capacity journalSegmentBytes=$segmentBytes")
        recordDiagnosticEnvironment(this, diagnostics)
        if (Build.VERSION.SDK_INT >= 30) try {
            val exits = (getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager)
                .getHistoricalProcessExitReasons(packageName, 0, 1)
            exits.firstOrNull()?.let { diagnostics.record("W", "PreviousExit", "timestamp=${it.timestamp} reason=${it.reason} status=${it.status} description=${it.description}") }
        } catch (error: Exception) { diagnostics.exception("PreviousExit", error) }
    }

    override fun onTerminate() {
        if (Thread.getDefaultUncaughtExceptionHandler() === crashHandler) Thread.setDefaultUncaughtExceptionHandler(priorCrashHandler)
        journal.close()
        super.onTerminate()
    }
}

/** UI helpers access the process owner through their explicit Android context. */
fun Context.clientDiagnostics(): DiagLog? = (applicationContext as? TofuApplication)?.diagnostics

/** Current device/WebView/network facts, without SSID, IP addresses or account information. */
fun recordDiagnosticEnvironment(context: Context, log: DiagLog) {
    try {
        val webView = WebViewCompat.getCurrentWebViewPackage(context)
        val connectivity = context.getSystemService(Context.CONNECTIVITY_SERVICE) as ConnectivityManager
        val network = connectivity.getNetworkCapabilities(connectivity.activeNetwork)
        val transports = listOf("wifi" to NetworkCapabilities.TRANSPORT_WIFI, "cellular" to NetworkCapabilities.TRANSPORT_CELLULAR, "vpn" to NetworkCapabilities.TRANSPORT_VPN, "ethernet" to NetworkCapabilities.TRANSPORT_ETHERNET)
            .filter { network?.hasTransport(it.second) == true }.map { it.first }
        val camera = ContextCompat.checkSelfPermission(context, android.Manifest.permission.CAMERA) == PackageManager.PERMISSION_GRANTED
        val microphone = ContextCompat.checkSelfPermission(context, android.Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED
        log.record("I", "Permissions", "camera=$camera microphone=$microphone")
        log.record("I", "Environment", "android=${Build.VERSION.SDK_INT}/${Build.VERSION.RELEASE} device=${Build.MANUFACTURER}/${Build.MODEL} webView=${webView?.packageName}/${webView?.versionName} network=$transports validated=${network?.hasCapability(NetworkCapabilities.NET_CAPABILITY_VALIDATED)} metered=${connectivity.isActiveNetworkMetered}")
    } catch (error: Exception) { log.exception("Environment", error) }
}
