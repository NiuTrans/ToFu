package com.tofu.client.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.produceState
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.tofu.client.BuildConfig
import com.tofu.client.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * "Open-source licences": the notices the local kernel's native code requires
 * (android/native/README.md "Licences and source"). The texts are the files of
 * android/native/licenses, packaged as assets in place by app/build.gradle.kts.
 */
object Licences {
    /** Asset name to the line that introduces it. */
    val TEXTS = listOf(
        "proot-COPYING" to R.string.licences_proot,
        "talloc-LICENSE" to R.string.licences_talloc,
        "GPL-3.0.txt" to R.string.licences_gpl3,
    )
}

@Composable
fun LicencesScreen(onDismiss: () -> Unit) {
    Dialog(onDismissRequest = onDismiss, properties = DialogProperties(usePlatformDefaultWidth = false)) {
        Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
            Column(Modifier.fillMaxSize().statusBarsPadding().navigationBarsPadding().padding(horizontal = 16.dp)) {
                Row(Modifier.fillMaxWidth().padding(top = 8.dp), verticalAlignment = Alignment.CenterVertically) {
                    Text(
                        stringResource(R.string.licences_title),
                        Modifier.weight(1f),
                        style = MaterialTheme.typography.titleLarge,
                    )
                    TextButton(onClick = onDismiss) { Text(stringResource(R.string.licences_close)) }
                }
                Column(
                    Modifier.verticalScroll(rememberScrollState()).padding(bottom = 16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    Text(stringResource(R.string.licences_intro), style = MaterialTheme.typography.bodyMedium)
                    for ((asset, intro) in Licences.TEXTS) {
                        Text(stringResource(intro), style = MaterialTheme.typography.titleSmall)
                        Text(
                            assetText(asset),
                            style = MaterialTheme.typography.bodySmall,
                            fontFamily = FontFamily.Monospace,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    Text(
                        stringResource(R.string.licences_source_offer, "v${BuildConfig.VERSION_NAME}"),
                        style = MaterialTheme.typography.bodyMedium,
                    )
                }
            }
        }
    }
}

@Composable
private fun assetText(name: String): String {
    val context = LocalContext.current
    val text by produceState("", name) {
        value = withContext(Dispatchers.IO) { context.assets.open(name).bufferedReader().use { it.readText() } }
    }
    return text
}
