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
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Check
import androidx.compose.material.icons.filled.Visibility
import androidx.compose.material.icons.filled.VisibilityOff
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
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
import androidx.compose.ui.text.input.KeyboardCapitalization
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.text.input.KeyboardType
import com.tofu.client.data.AuthType
import com.tofu.client.data.Profile
import com.tofu.client.session.HealthProbe
import com.tofu.client.session.ProfileForm
import com.tofu.client.session.ServerUrl
import com.tofu.client.session.TofuProbe
import com.tofu.client.ui.theme.TofuButtonShape
import kotlinx.coroutines.launch

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
) {
    var alias by remember { mutableStateOf(editing?.alias ?: "") }
    var url by remember { mutableStateOf(editing?.baseUrl ?: "") }
    // Auth defaults are URL-aware for NEW profiles: a `/proxy/<port>/` URL is
    // behind a code-server password gate → CODE_SERVER_PASSWORD; a bare host →
    // NONE. We track whether the user has manually overridden the picker so we
    // stop auto-following the URL once they do (and never override an edit).
    var authTouched by remember { mutableStateOf(editing != null) }
    var auth by remember {
        mutableStateOf(
            when {
                editing == null -> ServerUrl.defaultAuthType(url)
                // Edit-mode safety net: if a persisted profile is a proxy URL
                // stuck on the stale NONE default, show the corrected type so
                // the pencil reflects what the launch migration also fixes.
                ServerUrl.needsProxyAuthFix(editing.baseUrl, editing.authType) ->
                    AuthType.CODE_SERVER_PASSWORD
                else -> editing.authType
            },
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
        Column(
            Modifier.padding(pad).fillMaxSize().verticalScroll(rememberScrollState()),
        ) {
            FormHeader(
                title = if (editing == null) "Add server" else "Edit server",
                subtitle = if (editing == null) {
                    "Saved once, opened with a tap"
                } else {
                    "Changes re-authenticate on save"
                },
                onCancel = onCancel,
            )

            Column(
                Modifier.padding(horizontal = 20.dp),
                verticalArrangement = Arrangement.spacedBy(22.dp),
            ) {
                FormSection("Identity") {
                    TofuField(
                        value = alias,
                        onValueChange = { alias = it },
                        label = "Name",
                        placeholder = "Shanghai sandbox",
                        error = validation.errors["alias"],
                        helper = "How this server appears in your list.",
                        capitalize = true,
                    )
                    TofuField(
                        value = url,
                        onValueChange = { url = it },
                        label = "Server URL",
                        placeholder = "https://…/proxy/15000/",
                        error = validation.errors["baseUrl"],
                        helper = "Paste the full address, including /proxy/15000/.",
                        keyboardType = KeyboardType.Uri,
                    )
                    ConnectionTest(
                        url = url,
                        auth = auth,
                        hasSecret = secret.isNotBlank() || canOmitSecret,
                    )
                }

                FormSection("Access") {
                    AuthTypePicker(auth) { authTouched = true; auth = it }
                    AnimatedVisibility(auth == AuthType.CODE_SERVER_PASSWORD) {
                        TofuField(
                            value = secret,
                            onValueChange = { secret = it },
                            label = when {
                                secretAlreadyStored -> "Password (blank keeps current)"
                                reuseHost != null -> "Password (blank reuses saved)"
                                else -> "Password"
                            },
                            placeholder = "code-server password",
                            error = validation.errors["secret"],
                            helper = when {
                                secret.isEmpty() && reuseHost != null ->
                                    "Will reuse the saved password for $reuseHost."
                                secretAlreadyStored ->
                                    "Stored encrypted on this device only."
                                else ->
                                    "Stored encrypted on this device, never sent anywhere else."
                            },
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
                                        contentDescription = if (secretVisible) {
                                            "Hide password"
                                        } else {
                                            "Show password"
                                        },
                                        tint = MaterialTheme.colorScheme.onSurfaceVariant,
                                    )
                                }
                            },
                        )
                    }
                }

                FormSection("Server control") {
                    ToggleRow(
                        title = "Start and stop from the app",
                        subtitle = "Needs supervisor.py running on the host.",
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
                            label = "Project path on the host",
                            placeholder = "/home/dev/tofu",
                            helper = "Absolute path, and allow-listed in " +
                                "TOFU_SUPERVISOR_PROJECTS on the host.",
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
                    Text("Save & connect", fontWeight = FontWeight.SemiBold)
                }
                TextButton(
                    onClick = onCancel,
                    modifier = Modifier.fillMaxWidth().padding(top = 4.dp),
                ) {
                    Text("Cancel", color = MaterialTheme.colorScheme.onSurfaceVariant)
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
                contentDescription = "Back",
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
        SectionLabel(label, Modifier.padding(bottom = 12.dp, start = 2.dp))
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
private fun ConnectionTest(url: String, auth: AuthType, hasSecret: Boolean) {
    val scope = rememberCoroutineScope()
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
                        HealthProbe().probe(url.trim())
                    } finally {
                        testing = false
                    }
                }
            },
        ) {
            Text(if (testing) "Testing…" else "Test connection")
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

private fun AuthType.label(): String = when (this) {
    AuthType.CODE_SERVER_PASSWORD -> "code-server password"
    AuthType.INTERACTIVE_SSO -> "Interactive sign-in"
    AuthType.NONE -> "No authentication"
}

private fun AuthType.describe(): String = when (this) {
    AuthType.CODE_SERVER_PASSWORD ->
        "Replays your saved password automatically. Right for /proxy/ URLs."
    AuthType.INTERACTIVE_SSO ->
        "Sign in once in the app; the session is then remembered."
    AuthType.NONE ->
        "For a Tofu exposed directly on a trusted network."
}
