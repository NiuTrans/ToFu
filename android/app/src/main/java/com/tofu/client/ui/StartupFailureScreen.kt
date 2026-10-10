package com.tofu.client.ui

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.tofu.client.R
import com.tofu.client.session.DiagLog

/** Copyable recovery surface independent of Room, the vault and any network connection. */
@Composable
fun StartupFailureScreen(log: DiagLog) {
    val context = LocalContext.current
    Surface(Modifier.fillMaxSize()) {
        Column(Modifier.fillMaxSize().padding(28.dp), verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.CenterHorizontally) {
            Text(stringResource(R.string.diagnostics_startup_failed), style = MaterialTheme.typography.titleLarge)
            Spacer(Modifier.height(16.dp))
            Text(stringResource(R.string.diagnostics_startup_help))
            Spacer(Modifier.height(20.dp))
            Button(onClick = { copyDiagnostics(context, log) }) { Text(stringResource(R.string.diagnostics_copy)) }
            TextButton(onClick = { shareReport(context, buildClientReport(log, null, "", null), context.getString(R.string.report_subject)) }) { Text(stringResource(R.string.report_share)) }
        }
    }
}
