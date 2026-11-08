package com.tofu.client.selfcheck

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.FilterChip
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import com.tofu.client.R
import com.tofu.client.selfcheck.SelfCheckFiles
import com.tofu.client.ui.theme.TofuTheme
import kotlinx.coroutines.launch

/**
 * The device self-check screen. Three taps for the whole diagnostic:
 * open it from the list, tap Run, and (after pressing Home when asked) share
 * the report — from the completion notification or from here.
 */
class SelfCheckActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        SelfCheckController.recoverInterruptedRun(this)
        setContent {
            TofuTheme {
                Surface(Modifier.fillMaxSize()) {
                    SelfCheckScreen(
                        stateFlow = SelfCheckController.state(this),
                        onRun = { minutes -> SelfCheckController.start(this, minutes) },
                        onCancel = { SelfCheckController.cancel(this) },
                        onSkipBackground = { SelfCheckController.skipBackground(this) },
                        onShare = { shareSelfCheckReport(this) },
                        onClose = { finish() },
                    )
                }
            }
        }
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    private fun handleIntent(intent: Intent?) {
        if (intent?.action == ACTION_SHARE) {
            lifecycleScope.launch { shareSelfCheckReport(this@SelfCheckActivity) }
        }
    }

    companion object {
        const val ACTION_SHARE = "com.tofu.client.selfcheck.action.SHARE"
    }
}

@Composable
fun SelfCheckScreen(
    stateFlow: kotlinx.coroutines.flow.StateFlow<RunUiState>,
    onRun: (Int) -> Unit,
    onCancel: () -> Unit,
    onSkipBackground: () -> Unit,
    onShare: () -> Unit,
    onClose: () -> Unit,
) {
    val state by stateFlow.collectAsState()
    var minutes by remember { mutableIntStateOf(ChildStress.DEFAULT_PRESET_MINUTES) }
    Column(
        Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .navigationBarsPadding()
            .verticalScroll(rememberScrollState())
            .padding(horizontal = 20.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
            Text(stringResource(R.string.selfcheck_title), style = MaterialTheme.typography.headlineSmall)
            TextButton(onClick = onClose) { Text(stringResource(R.string.selfcheck_close)) }
        }
        Text(stringResource(R.string.selfcheck_intro), style = MaterialTheme.typography.bodyMedium)
        Text(stringResource(R.string.selfcheck_warning), style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.error)

        Text(stringResource(R.string.selfcheck_duration_label), style = MaterialTheme.typography.labelLarge)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            ChildStress.PRESET_MINUTES.forEach { preset ->
                FilterChip(
                    selected = minutes == preset,
                    onClick = { minutes = preset },
                    enabled = !state.running,
                    label = { Text("$preset min") },
                )
            }
        }

        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            if (state.running) {
                OutlinedButton(onClick = onCancel) { Text(stringResource(R.string.selfcheck_cancel)) }
            } else {
                Button(onClick = { onRun(minutes) }) { Text(stringResource(R.string.selfcheck_run)) }
            }
            Button(onClick = onShare, enabled = state.report != null) { Text(stringResource(R.string.selfcheck_share)) }
        }
        if (state.waitingForBackground) {
            OutlinedButton(onClick = onSkipBackground) { Text(stringResource(R.string.selfcheck_skip_background)) }
        }

        if (state.running) {
            Text("${state.step} · ${state.elapsedMs / 1000}s", style = MaterialTheme.typography.labelLarge)
            state.detail?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
            if (state.waitingForBackground) {
                Text(stringResource(R.string.selfcheck_background_instructions), style = MaterialTheme.typography.bodyMedium)
            }
        }

        state.report?.let { report ->
            HorizontalDivider()
            Text(stringResource(R.string.selfcheck_summary_title), style = MaterialTheme.typography.titleMedium)
            SelfCheckSummary.lines(report).forEach { line ->
                Text(line, style = MaterialTheme.typography.bodySmall)
            }
        } ?: Text(stringResource(R.string.selfcheck_no_report), style = MaterialTheme.typography.bodySmall)
        Spacer(Modifier.height(16.dp))
    }
}
