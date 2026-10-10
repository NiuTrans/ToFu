package com.tofu.client

import android.app.Application
import android.content.ClipboardManager
import android.content.Context
import android.content.ClipDescription
import com.tofu.client.session.DiagLog
import com.tofu.client.session.DiagnosticJournal
import com.tofu.client.ui.copyDiagnostics
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.File

/** Actual clipboard export, independent of a live server or functioning WebView. */
@RunWith(RobolectricTestRunner::class)
@Config(sdk = [33], application = Application::class)
class CopyDiagnosticsTest {
    @Test fun offline_copy_contains_redacted_previous_failure_and_build_metadata() {
        val context = RuntimeEnvironment.getApplication()
        val directory = File(context.cacheDir, "restart-diagnostics").apply { mkdirs() }
        DiagnosticJournal(directory).use { journal ->
            DiagLog(journal = journal).apply { exception("Login", IllegalStateException("Cookie: private-session=value")); flush() }
        }
        DiagnosticJournal(directory).use { journal ->
            val log = DiagLog(journal = journal)
            assertTrue(copyDiagnostics(context, log))
            val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
            val clip = clipboard.primaryClip!!
            val report = clip.getItemAt(0).text.toString()
            assertFalse(report.contains("private-session=value"))
            val json = JSONObject(report)
            assertEquals(BuildConfig.DIAGNOSTIC_BUILD_ID, json.getJSONObject("app").getString("buildId"))
            assertTrue(BuildConfig.DIAGNOSTIC_BUILD_ID.matches(Regex("[0-9a-f]{16}")))
            assertEquals(BuildConfig.VERSION_CODE, json.getJSONObject("app").getInt("versionCode"))
            assertTrue(json.getJSONArray("events").toString().contains("IllegalStateException"))
            assertTrue(clip.description.extras.getBoolean(ClipDescription.EXTRA_IS_SENSITIVE))
        }
    }
}
