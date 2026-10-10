package com.tofu.client.ui

import android.content.ClipboardManager
import android.content.Context
import android.widget.Toast
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ChevronRight
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material.icons.filled.Dns
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.tofu.client.R
import com.tofu.client.session.DeepLink

/**
 * First-run guidance, shown only when the user has no servers AND has never
 * completed setup. It leads with the three real ways to connect — scan, paste,
 * or type — because "how do I add my server" is the entire first-minute task,
 * and a blank form is a worse answer than a choice.
 *
 * Everything routes through the same [DeepLink] parser the deep-link
 * handler uses, so a scanned or pasted payload is validated once, consistently.
 */
@Composable
fun OnboardingScreen(
    onConnect: (url: String, alias: String?) -> Unit,
    onManual: () -> Unit,
    onSkip: () -> Unit,
    onDiagnostics: (() -> Unit)? = null,
) {
    val context = LocalContext.current
    val scan = rememberQrScanner { payload ->
        val connect = DeepLink.parse(payload)
        if (connect != null) {
            onConnect(connect.url, connect.alias)
        } else {
            Toast.makeText(
                context, context.getString(R.string.toast_bad_qr), Toast.LENGTH_LONG,
            ).show()
        }
    }

    Box(
        Modifier
            .fillMaxSize()
            .background(MaterialTheme.colorScheme.background),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            Modifier
                .widthIn(max = ContentMaxWidth)
                .fillMaxHeight()
                .verticalScroll(rememberScrollState())
                .statusBarsPadding()
                .navigationBarsPadding()
                .padding(horizontal = 28.dp, vertical = 24.dp),
            verticalArrangement = Arrangement.Center,
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Box(
                Modifier
                    .size(76.dp)
                    .clip(RoundedCornerShape(22.dp))
                    .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    Icons.Filled.Dns,
                    contentDescription = null,
                    tint = MaterialTheme.colorScheme.primary,
                    modifier = Modifier.size(34.dp),
                )
            }
            Spacer(Modifier.height(22.dp))
            Text(
                stringResource(R.string.onboard_welcome),
                style = MaterialTheme.typography.headlineSmall,
                color = MaterialTheme.colorScheme.onBackground,
            )
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(R.string.onboard_sub),
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                textAlign = TextAlign.Center,
            )
            Spacer(Modifier.height(30.dp))
            SectionLabel(stringResource(R.string.onboard_connect), Modifier.fillMaxWidth())
            Spacer(Modifier.height(12.dp))
            ConnectOption(
                icon = Icons.Filled.QrCodeScanner,
                title = stringResource(R.string.onboard_scan_title),
                subtitle = stringResource(R.string.onboard_scan_sub),
                onClick = scan,
            )
            Spacer(Modifier.height(10.dp))
            ConnectOption(
                icon = Icons.Filled.ContentPaste,
                title = stringResource(R.string.onboard_paste_title),
                subtitle = stringResource(R.string.onboard_paste_sub),
                onClick = { pasteInto(context, onConnect) },
            )
            Spacer(Modifier.height(10.dp))
            ConnectOption(
                icon = Icons.Filled.Dns,
                title = stringResource(R.string.onboard_manual_title),
                subtitle = stringResource(R.string.onboard_manual_sub),
                onClick = onManual,
            )
            if (onDiagnostics != null) TextButton(onClick = onDiagnostics) { Text(stringResource(R.string.diagnostics_copy)) }
            Spacer(Modifier.height(20.dp))
            TextButton(onClick = onSkip, modifier = TouchTarget) {
                Text(
                    stringResource(R.string.onboard_skip),
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

private fun pasteInto(context: Context, onConnect: (String, String?) -> Unit) {
    val connect = DeepLink.parse(clipboardText(context))
    if (connect != null) {
        onConnect(connect.url, connect.alias)
    } else {
        Toast.makeText(context, context.getString(R.string.toast_no_link), Toast.LENGTH_LONG).show()
    }
}

/** The clipboard's first text item, or null. Shared by onboarding + the form. */
internal fun clipboardText(context: Context): String? {
    val manager = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
    val clip = manager.primaryClip ?: return null
    if (clip.itemCount == 0) return null
    return clip.getItemAt(0).coerceToText(context).toString()
}

@Composable
private fun ConnectOption(
    icon: ImageVector,
    title: String,
    subtitle: String,
    onClick: () -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(14.dp))
            .background(MaterialTheme.colorScheme.surface)
            .border(
                1.dp, MaterialTheme.colorScheme.outlineVariant,
                RoundedCornerShape(14.dp),
            )
            .clickable(onClick = onClick)
            .padding(horizontal = 16.dp, vertical = 15.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            icon,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(24.dp),
        )
        Spacer(Modifier.width(14.dp))
        Column(Modifier.weight(1f)) {
            Text(
                title,
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                subtitle,
                Modifier.padding(top = 2.dp),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.width(8.dp))
        Icon(
            Icons.Filled.ChevronRight,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}
