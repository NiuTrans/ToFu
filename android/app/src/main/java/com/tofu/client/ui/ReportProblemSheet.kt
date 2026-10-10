package com.tofu.client.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.ClipDescription
import android.os.PersistableBundle
import com.tofu.client.clientDiagnostics
import com.tofu.client.recordDiagnosticEnvironment
import android.content.Context
import android.content.Intent
import android.os.Build
import android.widget.Toast
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.BugReport
import androidx.compose.material.icons.filled.CloudUpload
import androidx.compose.material.icons.filled.ContentCopy
import androidx.compose.material.icons.filled.Share
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.ModalBottomSheet
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.rememberModalBottomSheetState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.tofu.client.BuildConfig
import com.tofu.client.R
import com.tofu.client.data.Profile
import com.tofu.client.session.AppReportInfo
import com.tofu.client.session.ClientReport
import com.tofu.client.session.DeviceReportInfo
import com.tofu.client.session.DiagLog
import com.tofu.client.session.ProfileReportInfo
import com.tofu.client.session.ServerLifecycle
import com.tofu.client.session.ServerUrl
import com.tofu.client.ui.theme.TofuButtonShape
import kotlinx.coroutines.launch

/**
 * "Report a problem" — the one user-visible support surface.
 *
 * Design intent: reporting must be reachable at the moment something breaks,
 * not buried behind a hidden debug handle. The sheet shows exactly WHAT will be
 * sent (a one-line summary) before the user commits, offers the three transports
 * the product supports — the system share sheet (works with no server), the
 * clipboard, and an upload to the connected server — and never blocks as a
 * silent no-op: every action reports its outcome inline.
 *
 * It builds the payload through [ClientReport] (pure, bounded, redacted), so no
 * password or token can leave the device even if a console line leaked one.
 */

/** Android facts for the report, kept out of the pure [ClientReport] builder. */
fun appReportInfo(): AppReportInfo = AppReportInfo(
    versionName = BuildConfig.VERSION_NAME,
    versionCode = BuildConfig.VERSION_CODE,
    packageName = BuildConfig.APPLICATION_ID,
    debug = BuildConfig.DEBUG,
    buildId = BuildConfig.DIAGNOSTIC_BUILD_ID,
)

fun deviceReportInfo(): DeviceReportInfo = DeviceReportInfo(
    sdkInt = Build.VERSION.SDK_INT,
    release = Build.VERSION.RELEASE.orEmpty(),
    model = Build.MODEL.orEmpty(),
    manufacturer = Build.MANUFACTURER.orEmpty(),
    abi = Build.SUPPORTED_ABIS?.firstOrNull().orEmpty(),
)

/** Identity-only projection of a profile — name/host/state, never a secret. */
fun profileReportInfo(profile: Profile): ProfileReportInfo = ProfileReportInfo(
    alias = profile.alias,
    host = ServerUrl.parse(profile.baseUrl)?.host.orEmpty(),
    authType = profile.authType.name,
    managed = ServerLifecycle.isManaged(profile),
    signedIn = ServerLifecycle.isSignedIn(profile),
)

/** Assemble the final JSON payload, merging the native ring + web diagnostics. */
fun buildClientReport(
    diag: DiagLog,
    profile: Profile?,
    note: String,
    webDiagnostics: String?,
): String = ClientReport.build(
    reportId = ClientReport.newReportId(),
    createdAtMs = System.currentTimeMillis(),
    note = note,
    app = appReportInfo(),
    device = deviceReportInfo(),
    profile = profile?.let(::profileReportInfo),
    events = diag.snapshot(),
    webDiagnostics = webDiagnostics,
    captureTruncated = diag.evictedCount > 0,
)

/** A short, honest description of what the report will contain. */
@Composable
fun reportSummary(diag: DiagLog, profile: Profile?, webIncluded: Boolean): String {
    val parts = mutableListOf(
        stringResource(
            R.string.report_version,
            BuildConfig.VERSION_NAME, BuildConfig.VERSION_CODE,
        ),
    )
    if (profile != null) parts += profile.alias
    parts += pluralStringResource(
        R.plurals.report_events, diag.size(), diag.size(),
    )
    if (webIncluded) parts += stringResource(R.string.report_summary_web)
    return stringResource(R.string.report_summary_prefix) + parts.joinToString(" · ")
}

internal fun shareReport(context: Context, text: String, subject: String) {
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "text/plain"
        putExtra(Intent.EXTRA_SUBJECT, subject)
        putExtra(Intent.EXTRA_TEXT, text)
    }
    val chooser = Intent.createChooser(send, context.getString(R.string.report_share))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    try {
        context.startActivity(chooser)
    } catch (e: Exception) {
        context.clientDiagnostics()?.exception("ShareReport", e)
        Toast.makeText(context, context.getString(R.string.report_no_share), Toast.LENGTH_SHORT).show()
    }
}

/** One-tap offline export; does not wait for the server or the WebView. */
fun copyDiagnostics(context: Context, diag: DiagLog, profile: Profile? = null, web: String? = null): Boolean {
    recordDiagnosticEnvironment(context, diag)
    return copyReport(context, buildClientReport(diag, profile, "", web))
}

internal fun copyReport(context: Context, text: String): Boolean = try {
    val manager = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    val clip = ClipData.newPlainText(context.getString(R.string.report_clip_label), text)
    if (Build.VERSION.SDK_INT >= 33) clip.description.extras = PersistableBundle().apply { putBoolean(ClipDescription.EXTRA_IS_SENSITIVE, true) }
    manager.setPrimaryClip(clip)
    Toast.makeText(context, context.getString(R.string.report_copied), Toast.LENGTH_SHORT).show()
    true
} catch (error: RuntimeException) {
    context.clientDiagnostics()?.exception("Clipboard", error)
    Toast.makeText(context, context.getString(R.string.diagnostics_copy_failed), Toast.LENGTH_LONG).show()
    false
}

/**
 * [sendToServer] hands back a human status, not a code, so tint it red only
 * when it is clearly a failure. These are the exact failure prefixes
 * `SessionManager.submitReport` can produce; if that copy changes, the text
 * simply stays in the neutral accent colour rather than mis-colouring.
 */
private fun reportSendFailed(message: String): Boolean =
    message.startsWith("Couldn't") ||
        message.startsWith("Can't") ||
        message.startsWith("Server rejected")

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReportProblemSheet(
    title: String,
    dataSummary: String,
    canSendToServer: Boolean,
    onDismiss: () -> Unit,
    /** Build the payload from the user's note (called per action, so the note
     *  typed after opening is always included). */
    buildReport: (String) -> String,
    /** Best-effort upload; returns a user-facing status string. Never throws. */
    sendToServer: suspend (String) -> String,
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val sheetState = rememberModalBottomSheetState(skipPartiallyExpanded = true)
    var note by remember { mutableStateOf("") }
    var sending by remember { mutableStateOf(false) }
    var status by remember { mutableStateOf<String?>(null) }
    var statusFailed by remember { mutableStateOf(false) }

    ModalBottomSheet(onDismissRequest = onDismiss, sheetState = sheetState) {
        Column(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 22.dp)
                .navigationBarsPadding(),
            verticalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    Icons.Filled.BugReport,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                )
                Spacer(Modifier.width(10.dp))
                Column {
                    Text(
                        title,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSurface,
                    )
                    Text(
                        stringResource(R.string.report_subtitle),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }

            OutlinedTextField(
                value = note,
                onValueChange = { note = it },
                label = { Text(stringResource(R.string.report_note_label)) },
                placeholder = { Text(stringResource(R.string.report_note_placeholder)) },
                minLines = 2,
                modifier = Modifier.fillMaxWidth(),
            )

            Text(
                dataSummary,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )

            status?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.bodySmall,
                    color = if (statusFailed) {
                        MaterialTheme.colorScheme.error
                    } else {
                        MaterialTheme.colorScheme.primary
                    },
                )
            }

            Button(
                onClick = {
                    shareReport(
                        context, buildReport(note),
                        context.getString(R.string.report_subject),
                    )
                    onDismiss()
                },
                enabled = !sending,
                shape = TofuButtonShape,
                modifier = Modifier.fillMaxWidth().height(48.dp),
            ) {
                Icon(Icons.Filled.Share, null, Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text(stringResource(R.string.report_share), fontWeight = FontWeight.SemiBold)
            }

            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                OutlinedButton(
                    onClick = {
                        val copied = copyReport(context, buildReport(note))
                        statusFailed = !copied
                        status = context.getString(if (copied) R.string.report_copied else R.string.diagnostics_copy_failed)
                    },
                    enabled = !sending,
                    shape = TofuButtonShape,
                    modifier = Modifier.weight(1f),
                ) {
                    Icon(Icons.Filled.ContentCopy, null, Modifier.size(17.dp))
                    Spacer(Modifier.width(6.dp))
                    Text(stringResource(R.string.diagnostics_copy))
                }
                if (canSendToServer) {
                    OutlinedButton(
                        onClick = {
                            sending = true
                            status = null
                            scope.launch {
                                val message = sendToServer(buildReport(note))
                                statusFailed = reportSendFailed(message)
                                status = message
                                sending = false
                            }
                        },
                        enabled = !sending,
                        shape = TofuButtonShape,
                        modifier = Modifier.weight(1f),
                    ) {
                        if (sending) {
                            CircularProgressIndicator(
                                Modifier.size(16.dp),
                                strokeWidth = 2.dp,
                                color = MaterialTheme.colorScheme.primary,
                            )
                        } else {
                            Icon(Icons.Filled.CloudUpload, null, Modifier.size(17.dp))
                        }
                        Spacer(Modifier.width(6.dp))
                        Text(stringResource(R.string.report_send))
                    }
                }
            }
            Spacer(Modifier.height(6.dp))
        }
    }
}
