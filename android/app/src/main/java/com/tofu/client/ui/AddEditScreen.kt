package com.tofu.client.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.ContentPaste
import androidx.compose.material.icons.filled.QrCodeScanner
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.OutlinedTextFieldDefaults
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
import android.widget.Toast
import com.tofu.client.R
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.session.DeepLink
import com.tofu.client.session.HealthProbe
import com.tofu.client.session.ProfileForm
import com.tofu.client.session.ServerUrl
import com.tofu.client.session.TofuProbe
import com.tofu.client.ui.theme.TofuButtonShape
import kotlinx.coroutines.launch
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import com.tofu.client.session.DiagLog

/**
 * Add / edit a server.
 *
 * Restructured from a flat stack of text fields into three labelled sections
 * (Identity → Access → Server control). The previous version put every field at
 * the same visual weight, so the optional "Project path" — the switch that
 * turns on remote start/stop — looked exactly as mandatory as the URL, and the
 * auth picker was three `TextButton`s prefixed with ●/○ characters rather than
 * a real control.
 *
 * Validation still runs through the pure [ProfileForm.validate], so the same
 * rules the unit tests cover gate the Save button.
 */
@Composable
fun AddEditScreen(
    editing: Profile?,
    existingAliases: Set<String>,
    secretAlreadyStored: Boolean,
    onCancel: () -> Unit,
    onSubmit: (alias: String, url: String, auth: AuthType, secret: String, projectPath: String) -> Unit,
    /** Returns the host whose stored password would be reused for this URL when
     *  the field is left blank, or null. Used for a proactive hint. */
    reusableHostLookup: suspend (url: String, excludeAlias: String?) -> String? =
        { _, _ -> null },
    /** URL / name supplied by a scan, paste, or inbound deep link. */
    prefillUrl: String? = null,
    prefillAlias: String? = null,
    diagnostics: DiagLog? = null,
    status: UiStatus = UiStatus.Idle,
) {
    var alias by remember { mutableStateOf(editing?.alias ?: prefillAlias.orEmpty()) }
    var url by remember { mutableStateOf(editing?.baseUrl ?: prefillUrl.orEmpty()) }
    // Auth defaults are URL-aware for NEW profiles: a `/proxy/<port>/` URL is
    // behind a code-server password gate → CODE_SERVER_PASSWORD; a bare host →
    // NONE. We track whether the user has manually overridden the picker so we
    // stop auto-following the URL once they do (and never override an edit).
    var authTouched by remember { mutableStateOf(editing != null) }
    var auth by remember {
        mutableStateOf(
            editing?.authType ?: ServerUrl.defaultAuthType(url),
        )
    }
    var secret by remember { mutableStateOf("") }
    var secretVisible by remember { mutableStateOf(false) }
    var projectPath by remember { mutableStateOf(editing?.projectPath ?: "") }
    // The project path is what enables remote start/stop, so it is presented as
    // a feature toggle rather than yet another text box the user must decode.
    var manageEnabled by remember {
        mutableStateOf(!editing?.projectPath.isNullOrBlank())
    }

    // Until the user picks an auth type by hand, keep it in sync with the URL
    // they're typing (add-mode only).
    LaunchedEffect(url) {
        if (!authTouched) auth = ServerUrl.defaultAuthType(url)
    }
    // Host whose saved password would be reused if the field is left blank.
    var reuseHost by remember { mutableStateOf<String?>(null) }
    LaunchedEffect(url, auth, editing?.alias) {
        reuseHost = if (auth == AuthType.CODE_SERVER_PASSWORD)
            reusableHostLookup(url.trim(), editing?.alias) else null
    }

    val context = LocalContext.current
    fun applyConnect(connect: DeepLink.Connect) {
        url = connect.url
        if (alias.isBlank()) alias = connect.alias ?: ServerUrl.displayLabel(connect.url)
        // Let the auth picker re-derive from the newly supplied URL.
        authTouched = false
    }
    val scanQr = rememberQrScanner { payload ->
        val connect = DeepLink.parse(payload)
        if (connect != null) {
            applyConnect(connect)
        } else {
            Toast.makeText(
                context, context.getString(R.string.toast_bad_qr), Toast.LENGTH_LONG,
            ).show()
        }
    }
    fun pasteLink() {
        val connect = DeepLink.parse(clipboardText(context))
        if (connect != null) {
            applyConnect(connect)
        } else {
            Toast.makeText(context, context.getString(R.string.toast_no_link), Toast.LENGTH_LONG).show()
        }
    }

    // A blank password is acceptable when EITHER this profile already has a
    // stored secret (edit) OR a same-host password can be reused (add/edit).
    val canOmitSecret = secretAlreadyStored || reuseHost != null
    val validation = ProfileForm.validate(
        alias = alias, baseUrl = url, authType = auth, secret = secret,
        existingAliases = existingAliases,
        editingAlias = editing?.alias,
        secretAlreadyStored = canOmitSecret,
    )

    Scaffold(containerColor = MaterialTheme.colorScheme.background) { pad ->
        CappedScrollContent(pad) {
            FormHeader(
                title = if (editing == null) {
                    stringResource(R.string.add_title)
                } else {
                    stringResource(R.string.edit_title)
                },
                subtitle = if (editing == null) {
                    stringResource(R.string.add_subtitle)
                } else {
                    stringResource(R.string.edit_subtitle)
                },
                onCancel = onCancel,
            )

            if (diagnostics != null) TextButton(onClick = { copyDiagnostics(context, diagnostics, editing) }) {
                Text(stringResource(R.string.diagnostics_copy))
            }
            StatusBanner(status)
            Column(
                Modifier.padding(horizontal = 20.dp),
                verticalArrangement = Arrangement.spacedBy(28.dp),
            ) {
                FormSection(stringResource(R.string.section_identity)) {
                    TofuField(
                        value = alias,
                        onValueChange = { alias = it },
                        label = stringResource(R.string.field_name),
                        placeholder = stringResource(R.string.field_name_placeholder),
                        error = validation.errors["alias"],
                        helper = stringResource(R.string.field_name_helper),
                        capitalize = true,
                    )
                    TofuField(
                        value = url,
                        onValueChange = { url = it },
                        label = stringResource(R.string.field_url),
                        placeholder = stringResource(R.string.field_url_placeholder),
                        error = validation.errors["baseUrl"],
                        helper = stringResource(R.string.field_url_helper),
                        keyboardType = KeyboardType.Uri,
                    )
                    // Fast paths to the two error-prone fields: instead of
                    // typing a long URL, scan a QR or reuse the clipboard.
                    Row(horizontalArrangement = Arrangement.spacedBy(4.dp)) {
                        TextButton(onClick = scanQr, modifier = TouchTarget) {
                            Icon(
                                Icons.Filled.QrCodeScanner, null,
                                Modifier.size(18.dp),
                            )
                            Spacer(Modifier.width(6.dp))
                            Text(stringResource(R.string.action_scan_qr))
                        }
                        TextButton(onClick = { pasteLink() }, modifier = TouchTarget) {
                            Icon(
                                Icons.Filled.ContentPaste, null,
                                Modifier.size(18.dp),
                            )
                            Spacer(Modifier.width(6.dp))
                            Text(stringResource(R.string.action_paste_link))
                        }
                    }
                    ConnectionTest(
                        url = url,
                        auth = auth,
                        hasSecret = secret.isNotBlank() || canOmitSecret,
                        diagnostics = diagnostics,
                    )
                }

                FormSection(stringResource(R.string.section_access)) {
                    AuthTypePicker(auth) { authTouched = true; auth = it }
                    AnimatedVisibility(auth == AuthType.CODE_SERVER_PASSWORD) {
                        TofuField(
                            value = secret,
                            onValueChange = { secret = it },
                            label = stringResource(
                                when {
                                    secretAlreadyStored -> R.string.field_password_keep
                                    reuseHost != null -> R.string.field_password_reuse
                                    else -> R.string.field_password
                                },
                            ),
                            placeholder = stringResource(R.string.field_password_placeholder),
                            error = validation.errors["secret"],
                            helper = stringResource(
                                when {
                                    secret.isEmpty() && reuseHost != null ->
                                        R.string.helper_password_reuse
                                    secretAlreadyStored ->
                                        R.string.helper_password_stored
                                    else ->
                                        R.string.helper_password_new
                                },
                                reuseHost.orEmpty(),
                            ),
                            visualTransformation = if (secretVisible) {
                                VisualTransformation.None
                            } else {
                                PasswordVisualTransformation()
                            },
                            keyboardType = KeyboardType.Password,
                            trailing = {
                                IconButton(onClick = { secretVisible = !secretVisible }) {
                                    Icon(
                                        if (secretVisible) Icons.Filled.VisibilityOff
                                        else Icons.Filled.Visibility,
                                        contentDescription = stringResource(
                                            if (secretVisible) {
                                                R.string.cd_hide_password
                                            } else {
                                                R.string.cd_show_password
                                            },
                                        ),
                                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            },
                        )
                    }
                }

                FormSection(stringResource(R.string.section_server_control)) {
                    ToggleRow(
                        title = stringResource(R.string.toggle_manage_title),
                        subtitle = stringResource(R.string.toggle_manage_subtitle),
                        checked = manageEnabled,
                        onCheckedChange = {
                            manageEnabled = it
                            if (!it) projectPath = ""
                        },
                    )
                    AnimatedVisibility(manageEnabled) {
                        TofuField(
                            value = projectPath,
                            onValueChange = { projectPath = it },
                            label = stringResource(R.string.field_project_path),
                            placeholder = stringResource(R.string.field_project_path_placeholder),
                            helper = stringResource(R.string.field_project_path_helper),
                        )
                    }
                }
            }

            Spacer(Modifier.height(28.dp))
            Column(Modifier.padding(horizontal = 20.dp).navigationBarsPadding()) {
                Button(
                    onClick = {
                        onSubmit(
                            alias.trim(), url.trim(), auth, secret,
                            if (manageEnabled) projectPath.trim() else "",
                        )
                    },
                    enabled = validation.ok,
                    shape = TofuButtonShape,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = MaterialTheme.colorScheme.primary,
                    ),
                    modifier = Modifier.fillMaxWidth().height(50.dp),
                ) {
                    Text(stringResource(R.string.action_save_connect), fontWeight = FontWeight.SemiBold)
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

@Composable
private fun FormHeader(title: String, subtitle: String, onCancel: () -> Unit) {
    Column(
        Modifier
            .statusBarsPadding()
            .padding(start = 8.dp, end = 20.dp, top = 12.dp, bottom = 20.dp),
    ) {
        IconButton(onClick = onCancel) {
            Icon(
                Icons.AutoMirrored.Filled.ArrowBack,
                contentDescription = stringResource(R.string.cd_back),
                tint = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(4.dp))
        Text(
            title,
            Modifier.padding(start = 12.dp),
            style = MaterialTheme.typography.headlineSmall,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Text(
            subtitle,
            Modifier.padding(start = 12.dp, top = 3.dp),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun FormSection(label: String, content: @Composable () -> Unit) {
    Column {
        // A ruled section header: label + hairline rule, with generous spacing
        // before the fields, so the form reads as distinct groups rather than
        // one undifferentiated stack.
        Row(verticalAlignment = Alignment.CenterVertically) {
            SectionLabel(label)
            Spacer(Modifier.width(10.dp))
            HorizontalDivider(
                Modifier.weight(1f),
                color = MaterialTheme.colorScheme.outlineVariant,
            )
        }
        Spacer(Modifier.height(16.dp))
        Column(verticalArrangement = Arrangement.spacedBy(14.dp)) { content() }
    }
}

/**
 * A text field with consistent shape, helper text and error handling. Wrapping
 * it once keeps every field on the same visual rhythm — the previous screen
 * repeated `OutlinedTextField` boilerplate with subtly different supportingText
 * behaviour per field.
 */
@Composable
private fun TofuField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    placeholder: String = "",
    error: String? = null,
    helper: String? = null,
    capitalize: Boolean = false,
    keyboardType: KeyboardType = KeyboardType.Text,
    visualTransformation: VisualTransformation = VisualTransformation.None,
    trailing: @Composable (() -> Unit)? = null,
) {
    Column {
        OutlinedTextField(
            value = value,
            onValueChange = onValueChange,
            label = { Text(label) },
            placeholder = {
                Text(placeholder, color = MaterialTheme.colorScheme.onSurfaceVariant)
            },
            isError = error != null,
            singleLine = true,
            shape = RoundedCornerShape(12.dp),
            visualTransformation = visualTransformation,
            trailingIcon = trailing,
            keyboardOptions = KeyboardOptions(
                keyboardType = keyboardType,
                capitalization = if (capitalize) KeyboardCapitalization.Words
                else KeyboardCapitalization.None,
            ),
            colors = OutlinedTextFieldDefaults.colors(
                focusedBorderColor = MaterialTheme.colorScheme.primary,
                unfocusedBorderColor = MaterialTheme.colorScheme.outlineVariant,
                focusedContainerColor = MaterialTheme.colorScheme.surface,
                unfocusedContainerColor = MaterialTheme.colorScheme.surface,
            ),
            modifier = Modifier.fillMaxWidth(),
        )
        val support = error ?: helper
        if (support != null) {
            Text(
                support,
                Modifier.padding(start = 14.dp, top = 5.dp),
                style = MaterialTheme.typography.bodySmall,
                color = if (error != null) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/**
 * Paste-time reachability check — the one-price-upfront version of the desktop
 * agent's lesson (lib/desktop_agent/_probe.py): a bad URL must be caught HERE,
 * not after hours of silent retrying. The probe asks {url}/api/health WITH the
 * session cookie — behind a VS Code port-forwarding proxy the edge 401s every
 * cookie-less request, so an uncookied probe would measure the GATE, not the
 * server — and the verdict renders through [TofuProbe]'s Tofu↔gateway
 * discrimination, in the error colour only when the user must act.
 *
 * The verdict is discarded the moment the URL changes (a stale "correct" next
 * to an edited URL is worse than none).
 */
@Composable
private fun ConnectionTest(url: String, auth: AuthType, hasSecret: Boolean, diagnostics: DiagLog?) {
    val scope = rememberCoroutineScope()
    // One probe (and its OkHttpClient) per form, not per tap: a fresh client
    // per click allocates a connection pool + dispatcher threads each time.
    val probe = remember { HealthProbe(diagnostics = diagnostics) }
    var testing by remember { mutableStateOf(false) }
    var outcome by remember { mutableStateOf<HealthProbe.Outcome?>(null) }
    LaunchedEffect(url) { outcome = null }
    Column {
        TextButton(
            enabled = !testing && ServerUrl.parse(url) != null,
            onClick = {
                scope.launch {
                    testing = true
                    outcome = try {
                        withContext(Dispatchers.IO) { probe.probe(url.trim()) }
                    } finally {
                        testing = false
                    }
                }
            },
        ) {
            Text(
                stringResource(
                    if (testing) R.string.test_testing else R.string.test_connection,
                ),
            )
        }
        outcome?.let { o ->
            val problem = TofuProbe.isProblem(o.verdict, auth, hasSecret)
            Text(
                TofuProbe.guidance(o.verdict, auth, hasSecret),
                Modifier.padding(start = 14.dp, top = 2.dp),
                style = MaterialTheme.typography.bodySmall,
                color = if (problem) MaterialTheme.colorScheme.error
                else MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** Feature toggle row — used for the optional remote-control capability. */
@Composable
private fun ToggleRow(
    title: String,
    subtitle: String,
    checked: Boolean,
    onCheckedChange: (Boolean) -> Unit,
) {
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.surface)
            .clickable { onCheckedChange(!checked) }
            .padding(horizontal = 14.dp, vertical = 12.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
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
        Spacer(Modifier.width(12.dp))
        Switch(checked = checked, onCheckedChange = onCheckedChange)
    }
}

/**
 * Auth mode picker as selectable cards. Each option states what it MEANS
 * ("Replays your saved password") rather than only naming a mechanism, because
 * picking the wrong one here is the single most common way to end up staring
 * at a code-server login page inside the app.
 */
@Composable
private fun AuthTypePicker(current: AuthType, onPick: (AuthType) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        AuthType.values().forEach { at ->
            val selected = at == current
            Row(
                Modifier
                    .fillMaxWidth()
                    .clip(RoundedCornerShape(12.dp))
                    .background(
                        if (selected) MaterialTheme.colorScheme.primary.copy(alpha = 0.10f)
                        else MaterialTheme.colorScheme.surface,
                    )
                    .border(
                        width = if (selected) 1.4.dp else 1.dp,
                        color = if (selected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.outlineVariant,
                        shape = RoundedCornerShape(12.dp),
                    )
                    .clickable { onPick(at) }
                    .padding(horizontal = 14.dp, vertical = 12.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Column(Modifier.weight(1f)) {
                    Text(
                        at.label(),
                        style = MaterialTheme.typography.titleSmall,
                        color = if (selected) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurface,
                    )
                    Text(
                        at.describe(),
                        Modifier.padding(top = 2.dp),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (selected) {
                    Box(
                        Modifier
                            .size(21.dp)
                            .clip(RoundedCornerShape(11.dp))
                            .background(MaterialTheme.colorScheme.primary),
                        contentAlignment = Alignment.Center,
                    ) {
                        Icon(
                            Icons.Filled.Check,
                            contentDescription = null,
                            tint = MaterialTheme.colorScheme.onPrimary,
                            modifier = Modifier.size(14.dp),
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun AuthType.label(): String = stringResource(
    when (this) {
        AuthType.CODE_SERVER_PASSWORD -> R.string.auth_code_server
        AuthType.INTERACTIVE_SSO -> R.string.auth_sso
        AuthType.NONE -> R.string.auth_none
    },
)

@Composable
private fun AuthType.describe(): String = stringResource(
    when (this) {
        AuthType.CODE_SERVER_PASSWORD -> R.string.auth_code_server_desc
        AuthType.INTERACTIVE_SSO -> R.string.auth_sso_desc
        AuthType.NONE -> R.string.auth_none_desc
    },
)
