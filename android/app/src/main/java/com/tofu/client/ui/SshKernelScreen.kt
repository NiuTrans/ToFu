package com.tofu.client.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import com.tofu.client.R
import com.tofu.client.data.Profile
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.KernelState
import com.tofu.client.session.ProfileForm
import com.tofu.client.ui.theme.TofuButtonShape
import kotlinx.coroutines.flow.StateFlow

/**
 * Add / edit an SSH kernel: the [SshEndpoint] fields, plus the SSH account's
 * password, which goes to the vault and never to Room (blank keeps a saved
 * one; none at all means key-only sign-in). Saving stores the kernel and
 * returns to the Kernels list, where tapping the card connects.
 *
 * Also shows this phone's [publicKey] to copy into the host's
 * `authorized_keys`, and — when a first connect stopped at an unpinned host —
 * the key that host presented ([hostKeyToTrust]) for the user to check and
 * trust ([onTrust]). The pinned key itself is read-only here: only trusting
 * sets it, and a new host or port clears it. While [editing] a kernel whose
 * last connect failed, the reason is shown.
 *
 * Validation is the pure [ProfileForm.validateSsh], as for the URL form.
 */
@Composable
fun SshKernelScreen(
    editing: Profile?,
    existingAliases: Set<String>,
    onCancel: () -> Unit,
    onSubmit: (alias: String, endpoint: SshEndpoint, password: String) -> Unit,
    status: UiStatus = UiStatus.Idle,
    publicKey: String? = null,
    passwordStored: Boolean = false,
    hostKeyToTrust: String? = null,
    onTrust: () -> Unit = {},
    kernelState: ((Profile) -> StateFlow<KernelState>)? = null,
) {
    val saved = editing?.ssh
    var alias by remember { mutableStateOf(editing?.alias.orEmpty()) }
    var host by remember { mutableStateOf(saved?.host.orEmpty()) }
    var port by remember { mutableStateOf((saved?.port ?: SshEndpoint.DEFAULT_PORT).toString()) }
    var user by remember { mutableStateOf(saved?.user.orEmpty()) }
    var remotePort by remember {
        mutableStateOf((saved?.remotePort ?: SshEndpoint.DEFAULT_REMOTE_PORT).toString())
    }
    var password by remember { mutableStateOf("") }
    val validation = ProfileForm.validateSsh(
        alias = alias, host = host, port = port, user = user, remotePort = remotePort,
        existingAliases = existingAliases, editingAlias = editing?.alias,
    )
    val failure = if (editing != null && kernelState != null) {
        remember(editing) { kernelState(editing) }.collectAsState().value as? KernelState.Failed
    } else {
        null
    }

    Scaffold(containerColor = MaterialTheme.colorScheme.background) { pad ->
        CappedScrollContent(pad) {
            FormHeader(
                title = stringResource(if (editing == null) R.string.ssh_add_title else R.string.ssh_edit_title),
                subtitle = stringResource(R.string.ssh_subtitle),
                onCancel = onCancel,
            )
            StatusBanner(status)
            if (hostKeyToTrust != null && saved != null) {
                TrustHostKey("${saved.host}:${saved.port}", hostKeyToTrust, onTrust)
            } else if (failure != null && status == UiStatus.Idle) {
                StatusBanner(UiStatus.Error(failure.reason))
            }
            Column(
                Modifier.padding(horizontal = 20.dp),
                verticalArrangement = Arrangement.spacedBy(28.dp),
            ) {
                FormSection(stringResource(R.string.section_identity)) {
                    TofuField(
                        value = alias,
                        onValueChange = { alias = it },
                        label = stringResource(R.string.field_name),
                        placeholder = stringResource(R.string.ssh_field_name_placeholder),
                        error = validation.errors["alias"],
                        helper = stringResource(R.string.field_name_helper),
                        capitalize = true,
                    )
                }
                FormSection(stringResource(R.string.ssh_section_connection)) {
                    TofuField(
                        value = host,
                        onValueChange = { host = it },
                        label = stringResource(R.string.ssh_field_host),
                        placeholder = stringResource(R.string.ssh_field_host_placeholder),
                        error = validation.errors["host"],
                        keyboardType = KeyboardType.Uri,
                    )
                    TofuField(
                        value = port,
                        onValueChange = { port = it },
                        label = stringResource(R.string.ssh_field_port),
                        error = validation.errors["port"],
                        keyboardType = KeyboardType.Number,
                    )
                    TofuField(
                        value = user,
                        onValueChange = { user = it },
                        label = stringResource(R.string.ssh_field_user),
                        error = validation.errors["user"],
                    )
                    TofuField(
                        value = remotePort,
                        onValueChange = { remotePort = it },
                        label = stringResource(R.string.ssh_field_remote_port),
                        error = validation.errors["remotePort"],
                        helper = stringResource(R.string.ssh_field_remote_port_helper),
                        keyboardType = KeyboardType.Number,
                    )
                    Text(
                        saved?.hostKeySha256?.let { stringResource(R.string.ssh_pinned_key, it) }
                            ?: stringResource(R.string.ssh_unpinned_key),
                        Modifier.padding(start = 14.dp),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                FormSection(stringResource(R.string.ssh_section_sign_in)) {
                    TofuField(
                        value = password,
                        onValueChange = { password = it },
                        label = stringResource(
                            if (passwordStored) R.string.ssh_field_password_keep else R.string.ssh_field_password,
                        ),
                        helper = stringResource(R.string.ssh_field_password_helper),
                        visualTransformation = PasswordVisualTransformation(),
                        keyboardType = KeyboardType.Password,
                    )
                    if (publicKey != null) PublicKey(publicKey)
                }
            }

            Spacer(Modifier.height(28.dp))
            Column(Modifier.padding(horizontal = 20.dp).navigationBarsPadding()) {
                Button(
                    onClick = { onSubmit(alias.trim(), ProfileForm.toSshEndpoint(host, port, user, remotePort), password) },
                    enabled = validation.ok,
                    shape = TofuButtonShape,
                    colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.primary),
                    modifier = Modifier.fillMaxWidth().height(50.dp),
                ) {
                    Text(stringResource(R.string.action_save), fontWeight = FontWeight.SemiBold)
                }
                TextButton(
                    onClick = onCancel,
                    modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                ) {
                    Text(
                        stringResource(R.string.action_cancel),
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            Spacer(Modifier.height(28.dp))
        }
    }
}

/** This phone's public key, selectable, with a one-tap copy for the host's `authorized_keys`. */
@Composable
private fun PublicKey(line: String) {
    val clipboard = LocalClipboardManager.current
    var copied by remember(line) { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Text(
            stringResource(R.string.ssh_public_key_label),
            style = MaterialTheme.typography.labelLarge,
            color = MaterialTheme.colorScheme.onSurface,
        )
        SelectionContainer {
            Text(
                line,
                style = MaterialTheme.typography.bodySmall,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Text(
            stringResource(R.string.ssh_public_key_helper),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        OutlinedButton(onClick = { clipboard.setText(AnnotatedString(line)); copied = true }, shape = TofuButtonShape) {
            Text(stringResource(if (copied) R.string.ssh_public_key_copied else R.string.ssh_copy_public_key))
        }
    }
}

/** The host key a first connect stopped at, for the user to check on the host before trusting it. */
@Composable
private fun TrustHostKey(place: String, fingerprint: String, onTrust: () -> Unit) {
    val tint = MaterialTheme.colorScheme.primary
    Column(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(tint.copy(alpha = 0.10f))
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        Text(stringResource(R.string.ssh_trust_title), style = MaterialTheme.typography.titleSmall, color = tint)
        Text(
            stringResource(R.string.ssh_trust_body, place),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
        SelectionContainer {
            Text(
                fingerprint,
                style = MaterialTheme.typography.bodyMedium,
                fontFamily = FontFamily.Monospace,
                color = MaterialTheme.colorScheme.onSurface,
            )
        }
        Button(onClick = onTrust, shape = TofuButtonShape, colors = ButtonDefaults.buttonColors(containerColor = tint)) {
            Text(stringResource(R.string.ssh_trust_action), fontWeight = FontWeight.SemiBold)
        }
    }
}
