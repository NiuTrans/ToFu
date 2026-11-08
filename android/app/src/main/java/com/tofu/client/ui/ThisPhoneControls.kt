package com.tofu.client.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.widget.Toast
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.tofu.client.R
import com.tofu.client.data.Profile
import com.tofu.client.session.KernelState
import com.tofu.client.session.LocalKernelHost
import kotlinx.coroutines.launch

/**
 * Opening "This phone": the first start on Android 13+ asks for
 * POST_NOTIFICATIONS, then opens either way. Denied, the kernel still runs, but
 * its ongoing notification (with Stop) and failure notices stay hidden; the
 * This phone card's own Stop remains.
 */
@Composable
fun rememberThisPhoneOpen(host: LocalKernelHost, onActivate: (Profile) -> Unit): (Profile) -> Unit {
    val context = LocalContext.current
    val activate by rememberUpdatedState(onActivate)
    var pending by remember { mutableStateOf<Profile?>(null) }
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
        if (!granted) Toast.makeText(context, R.string.this_phone_notifications_denied, Toast.LENGTH_LONG).show()
        pending?.let(activate)
        pending = null
    }
    return remember(host) {
        { phone ->
            val granted = Build.VERSION.SDK_INT < 33 ||
                ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED
            if (host.available && host.settings.shouldRequestNotifications(Build.VERSION.SDK_INT, granted)) {
                pending = phone
                launcher.launch(Manifest.permission.POST_NOTIFICATIONS)
            } else {
                activate(phone)
            }
        }
    }
}

/** Under "This phone": the "Keep running" toggle and Stop (when it can run here), and the licences. */
@Composable
fun ThisPhoneDetails(host: LocalKernelHost, profile: Profile, state: KernelState) {
    val keepRunning by host.settings.keepRunning.collectAsState()
    val scope = rememberCoroutineScope()
    var licences by remember { mutableStateOf(false) }
    Spacer(Modifier.height(10.dp))
    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
    Spacer(Modifier.height(6.dp))
    if (host.available) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Column(Modifier.weight(1f)) {
                Text(stringResource(R.string.this_phone_keep_running), style = MaterialTheme.typography.titleSmall)
                Text(
                    stringResource(R.string.this_phone_keep_running_desc),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Spacer(Modifier.width(8.dp))
            Switch(checked = keepRunning, onCheckedChange = host.settings::setKeepRunning)
        }
    }
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
        if (host.available && (state == KernelState.Ready || state == KernelState.Starting)) {
            TextButton(onClick = { scope.launch { host.controller.stop(profile) } }) {
                Text(stringResource(R.string.this_phone_stop))
            }
        }
        Spacer(Modifier.weight(1f))
        TextButton(onClick = { licences = true }) { Text(stringResource(R.string.licences_open)) }
    }
    if (licences) LicencesScreen(onDismiss = { licences = false })
}
