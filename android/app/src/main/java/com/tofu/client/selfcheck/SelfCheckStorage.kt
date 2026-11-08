package com.tofu.client.selfcheck

import android.content.ClipData
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.tofu.client.R
import com.tofu.client.clientDiagnostics
import java.io.File
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/** Where a run persists: the report JSON, a heartbeat, and the pids that must not outlive it. */
class SelfCheckFiles(context: Context) {
    private val app = context.applicationContext
    private val root = File(app.filesDir, "selfcheck")
    private val reportFile = File(root, "report.json")
    private val heartbeatFile = File(root, "heartbeat")
    private val pidsFile = File(root, "pids")

    fun reportFile(): File = reportFile
    fun reportText(): String? = reportFile.takeIf { it.isFile }?.readText()
    fun heartbeatText(): String? = heartbeatFile.takeIf { it.isFile }?.readText()?.trim()
    fun pids(): List<Int> = pidsFile.takeIf { it.isFile }?.readLines().orEmpty().mapNotNull { it.trim().toIntOrNull() }

    fun writeReport(text: String) = write(reportFile, text)
    fun writeHeartbeat(text: String) = write(heartbeatFile, text)
    fun writePids(pids: List<Int>) = write(pidsFile, pids.joinToString("\n", postfix = if (pids.isEmpty()) "" else "\n"))

    fun clear() {
        heartbeatFile.delete()
        pidsFile.delete()
    }

    /** The report handed to the share sheet lives under the FileProvider's existing `captures` cache root. */
    fun shareCopy(reportId: String): File {
        val directory = File(app.cacheDir, "captures/selfcheck").apply { mkdirs() }
        val target = File(directory, "tofu-selfcheck-${reportId.take(24)}.json")
        target.writeText(reportText().orEmpty())
        return target
    }

    private fun write(file: File, text: String) {
        root.mkdirs()
        val temporary = File(root, ".${file.name}.tmp")
        temporary.writeText(text)
        Files.move(temporary.toPath(), file.toPath(), StandardCopyOption.REPLACE_EXISTING)
    }
}

class AndroidSelfCheckStore(
    context: Context,
    private val redactor: () -> SelfCheckRedactor,
) : SelfCheckStore {
    private val files = SelfCheckFiles(context)

    override fun writeReport(report: SelfCheckReport) {
        files.writeReport(ReportJson.of(report).render(redactor()))
    }

    override fun writeInterrupted(reportId: String, detectedAtWallMs: Long, lastHeartbeatWallMs: Long?, runStartedAtWallMs: Long?, exitReasons: List<ExitReasonRecord>, previousReportJson: String?) {
        files.writeReport(ReportJson.interrupted(reportId, detectedAtWallMs, lastHeartbeatWallMs, runStartedAtWallMs, exitReasons, previousReportJson).render(redactor()))
    }

    override fun writeHeartbeat(wallMs: Long) = files.writeHeartbeat(wallMs.toString())
    override fun writePids(pids: List<Int>) = files.writePids(pids)
    override fun pids(): List<Int> = files.pids()
    override fun heartbeat(): Long? = files.heartbeatText()?.toLongOrNull()
    override fun previousReportJson(): String? = files.reportText()
    override fun interruptedRun(): Boolean = files.heartbeatText() != null
    override fun clear() = files.clear()
}

/** The JSON file through the existing FileProvider (`${applicationId}.fileprovider`, cache-path covers cacheDir). */
fun shareSelfCheckReport(context: Context) {
    val files = SelfCheckFiles(context)
    val report = files.reportText() ?: return
    val reportId = "\"reportId\"\\s*:\\s*\"([A-Za-z0-9]+)\"".toRegex().find(report)?.groupValues?.get(1) ?: "latest"
    val file = files.shareCopy(reportId)
    val uri = FileProvider.getUriForFile(context.applicationContext, "${context.packageName}.fileprovider", file)
    val send = Intent(Intent.ACTION_SEND).apply {
        type = "application/json"
        putExtra(Intent.EXTRA_STREAM, uri)
        putExtra(Intent.EXTRA_SUBJECT, context.getString(R.string.selfcheck_share_subject))
        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
        clipData = ClipData.newUri(context.contentResolver, context.getString(R.string.selfcheck_report_clip_label), uri)
    }
    val chooser = Intent.createChooser(send, context.getString(R.string.selfcheck_share))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    try {
        context.startActivity(chooser)
    } catch (error: Exception) {
        context.clientDiagnostics()?.exception("SelfCheckShare", error)
    }
}
