package com.tofu.client.ui

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.background
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
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
import androidx.compose.foundation.lazy.grid.GridCells
import androidx.compose.foundation.lazy.grid.LazyVerticalGrid
import androidx.compose.foundation.lazy.grid.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.BugReport
import androidx.compose.material.icons.filled.Dns
import androidx.compose.material.icons.filled.Language
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.filled.PhoneAndroid
import androidx.compose.material.icons.filled.SystemUpdateAlt
import androidx.compose.material.icons.filled.Terminal
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.res.pluralStringResource
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.tofu.client.BuildConfig
import com.tofu.client.R
import com.tofu.client.data.KernelKind
import com.tofu.client.data.Profile
import com.tofu.client.data.SshEndpoint
import com.tofu.client.session.DiagLog
import com.tofu.client.session.KernelState
import com.tofu.client.session.LocalKernelHost
import com.tofu.client.session.ServerLifecycle
import com.tofu.client.session.ServerState
import com.tofu.client.session.ServerUrl
import com.tofu.client.session.SessionManager
import com.tofu.client.session.UpdateStatus
import com.tofu.client.ui.theme.CardShape
import com.tofu.client.ui.theme.TofuButtonShape
import kotlinx.coroutines.flow.StateFlow

/**
 * Home — the Kernels list / switcher: "This phone" pinned first, then every URL
 * and SSH kernel. A URL kernel is today's server card; LOCAL and SSH kernels
 * show the state their [com.tofu.client.session.KernelController] reports.
 *
 * Redesigned from the first version, which was a stock `TopAppBar` over plain
 * cards whose only affordance was a text "Open" link. The problems it had, and
 * what replaces them:
 *
 *  * **No identity.** Every row was a bold alias over a 90-char URL, so a list
 *    of sandboxes was visually identical. Now each server carries a colored
 *    monogram tile and shows only its meaningful URL fragment.
 *  * **No state.** Whether a server was up was invisible until you opened it.
 *    Now every managed server shows a live status chip.
 *  * **Cluttered actions.** Edit/Delete icons competed with the primary action.
 *    They now live in an overflow menu, leaving Open as the obvious target.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ProfileListScreen(
    /** The Kernels list, "This phone" first ([com.tofu.client.session.KernelList.pinned]). */
    profiles: List<Profile>,
    status: UiStatus,
    onActivate: (Profile) -> Unit,
    onEdit: (Profile) -> Unit,
    onDelete: (Profile) -> Unit,
    /** Add a URL kernel (the server form). */
    onAdd: () -> Unit,
    /** Add an SSH kernel. */
    onAddSsh: () -> Unit,
    /**
     * Establishes a session on demand so a STOPPED server can still be started
     * — code-server stays up while Tofu is down. Non-null by contract: a null
     * here would silently disable login-then-act and re-create the
     * start-a-stopped-server deadlock.
     */
    session: SessionManager,
    /** Process-wide diagnostic ring, so a list-level report carries the events
     *  (login failures, load errors) leading up to it. */
    diag: DiagLog,
    /** A kernel's lifecycle, from its controller (same flow for the same kernel). */
    kernelState: (Profile) -> StateFlow<KernelState>,
    /** The kernel in front (last opened), whose name and state the header shows. */
    activeKernel: Profile? = null,
    /** Newest client version any configured server advertises, or null. */
    update: UpdateStatus? = null,
    /** Open the advertised download link. */
    onUpdate: (String) -> Unit = {},
    /** Kick off the one-shot update check (idempotent). */
    onCheckUpdate: () -> Unit = {},
) {
    // Per-profile lifecycle state, lifted here so the header can summarize how
    // many servers are up without each card re-polling.
    val context = androidx.compose.ui.platform.LocalContext.current
    val states = remember { mutableStateMapOf<Long, ServerState>() }
    // LOCAL/SSH kernels that are up (Ready or SafeMode), for the same summary.
    val kernelsUp = remember { mutableStateMapOf<Long, Boolean>() }
    var reportOpen by remember { mutableStateOf(false) }
    val phone = profiles.firstOrNull { it.kind == KernelKind.LOCAL }
    val others = profiles.filter { it.kind != KernelKind.LOCAL }
    // The app's "This phone" host; none in previews and screenshots.
    val phoneHost = remember { LocalKernelHost.current }
    val openPhone = if (phoneHost != null) rememberThisPhoneOpen(phoneHost, onActivate) else onActivate

    // Runs once per process (the ViewModel guards it) and only once a server
    // exists to ask. Keyed on the count so it fires when Room's first emit
    // lands rather than on the empty initial value.
    LaunchedEffect(others.size) { onCheckUpdate() }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
    ) { pad ->
        CappedContent(
            padding = pad,
            maxWidth = ListMaxWidth,
            floatingActionButton = {
                var addOpen by remember { mutableStateOf(false) }
                Box {
                    ExtendedFloatingActionButton(
                        onClick = { addOpen = true },
                        icon = { Icon(Icons.Default.Add, contentDescription = null) },
                        text = { Text(stringResource(R.string.list_fab_add), fontWeight = FontWeight.SemiBold) },
                        containerColor = MaterialTheme.colorScheme.primary,
                        contentColor = MaterialTheme.colorScheme.onPrimary,
                    )
                    AddKernelMenu(addOpen, { addOpen = false }, onAdd, onAddSsh)
                }
            },
        ) {
            HomeHeader(
                serverCount = profiles.size,
                states = states,
                kernelsUp = kernelsUp,
                onReport = { reportOpen = true },
            )
            activeKernel?.let { ActiveKernelLine(it, kernelState) }
            update?.takeIf { it.available && it.downloadUrl != null }?.let { status ->
                UpdateBanner(
                    latest = status.latest,
                    installed = status.installed,
                    onDownload = { status.downloadUrl?.let(onUpdate) },
                )
            }
            TextButton(onClick = { copyDiagnostics(context, diag) }) {
                Text(stringResource(R.string.diagnostics_copy))
            }
            TextButton(onClick = {
                context.startActivity(android.content.Intent(context, com.tofu.client.selfcheck.SelfCheckActivity::class.java))
            }) {
                Text(stringResource(R.string.selfcheck_title))
            }
            StatusBanner(status)
            Box(Modifier.weight(1f).fillMaxWidth()) {
                if (others.isEmpty()) {
                    Column(Modifier.fillMaxSize()) {
                        if (phone != null) {
                            val setUp = remember(phone.id) { { up: Boolean -> kernelsUp[phone.id] = up } }
                            Box(Modifier.padding(start = 16.dp, end = 16.dp, top = 4.dp)) {
                                KernelCard(phone, kernelState, openPhone, onEdit, onDelete, setUp, phoneHost)
                            }
                        }
                        Box(Modifier.weight(1f)) { EmptyState(onAdd, onAddSsh) }
                    }
                } else {
                    LazyVerticalGrid(
                        // Adaptive: one column on a phone, two on a tablet (the
                        // 840dp [ListMaxWidth] cap bounds how wide that grows).
                        columns = GridCells.Adaptive(minSize = 320.dp),
                        modifier = Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(
                            start = 16.dp, end = 16.dp, top = 4.dp, bottom = 96.dp,
                        ),
                        verticalArrangement = Arrangement.spacedBy(12.dp),
                        horizontalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        if (phone != null) item(key = "kernel-this-phone") {
                            val setUp = remember(phone.id) { { up: Boolean -> kernelsUp[phone.id] = up } }
                            KernelCard(phone, kernelState, openPhone, onEdit, onDelete, setUp, phoneHost)
                        }
                        items(others, key = { it.id }) { p ->
                            when (p.kind) {
                                KernelKind.REMOTE_URL -> {
                                    // Stable per-row setter. An inline lambda is
                                    // reallocated on every list recomposition, which
                                    // makes the row un-skippable and forces every card
                                    // to recompose whenever any card's state changes.
                                    val setState = remember(p.id) {
                                        { s: ServerState -> states[p.id] = s }
                                    }
                                    ServerCard(
                                        profile = p,
                                        session = session,
                                        onActivate = onActivate,
                                        onEdit = onEdit,
                                        onDelete = onDelete,
                                        onStateChange = setState,
                                    )
                                }
                                KernelKind.LOCAL, KernelKind.SSH -> {
                                    val setUp = remember(p.id) { { up: Boolean -> kernelsUp[p.id] = up } }
                                    KernelCard(p, kernelState, onActivate, onEdit, onDelete, setUp)
                                }
                            }
                        }
                    }
                }
            }
            VersionFooter()
        }
    }

    if (reportOpen) {
        ReportProblemSheet(
            title = "Report a problem",
            dataSummary = reportSummary(diag, null, webIncluded = false),
            // No single server to upload to from the list, and the server may
            // be exactly what is broken — share/clipboard remain available.
            canSendToServer = false,
            onDismiss = { reportOpen = false },
            buildReport = { note -> buildClientReport(diag, null, note, null) },
            sendToServer = { "" },
        )
    }
}

/**
 * Branded header. Replaces the stock TopAppBar: it carries the wordmark plus a
 * live one-line summary ("3 kernels · 1 running"), which is the single most
 * useful thing to know on opening the app.
 */
@Composable
private fun HomeHeader(
    serverCount: Int,
    states: Map<Long, ServerState>,
    kernelsUp: Map<Long, Boolean>,
    onReport: () -> Unit,
) {
    // Read the per-server states HERE, not in the caller: a snapshot read in
    // the parent would recompose the whole list every time any card's status
    // changes. Scoped to the header, the same read recomposes only the header.
    val runningCount = states.values.count { it == ServerState.RUNNING } + kernelsUp.values.count { it }
    Column(
        Modifier
            .fillMaxWidth()
            .statusBarsPadding()
            .padding(start = 20.dp, end = 12.dp, top = 20.dp, bottom = 14.dp),
    ) {
        Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.CenterVertically) {
            Text(
                "Tofu",
                style = MaterialTheme.typography.headlineSmall,
                color = MaterialTheme.colorScheme.onBackground,
            )
            Spacer(Modifier.width(9.dp))
            Box(
                Modifier
                    .clip(RoundedCornerShape(6.dp))
                    .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.14f))
                    .padding(horizontal = 7.dp, vertical = 3.dp),
            ) {
                Text(
                    "v${BuildConfig.VERSION_NAME}",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
            Spacer(Modifier.weight(1f))
            // Always-available support entry: the one place a user can report a
            // problem even when no server will open.
            IconButton(onClick = onReport) {
                Icon(
                    Icons.Filled.BugReport,
                    contentDescription = stringResource(R.string.list_report_cd),
                    tint = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Spacer(Modifier.height(3.dp))
        Text(
            summaryText(serverCount, runningCount),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/**
 * "A newer Tofu is available" strip. Shown only when a reachable server
 * advertises a newer client than the installed build AND carries a download
 * link, so it can never be a dead button. One tap opens the link; installation
 * is the platform's normal APK flow.
 */
@Composable
private fun UpdateBanner(latest: String, installed: String, onDownload: () -> Unit) {
    Row(
        Modifier
            .fillMaxWidth()
            .padding(horizontal = 16.dp, vertical = 4.dp)
            .clip(RoundedCornerShape(12.dp))
            .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.10f))
            .padding(start = 14.dp, end = 6.dp, top = 8.dp, bottom = 8.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Icon(
            Icons.Filled.SystemUpdateAlt,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(20.dp),
        )
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(
                stringResource(R.string.list_update_available, latest),
                style = MaterialTheme.typography.titleSmall,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                stringResource(R.string.list_update_installed, installed),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        TextButton(onClick = onDownload, modifier = TouchTarget) {
            Text(stringResource(R.string.list_download), fontWeight = FontWeight.SemiBold)
        }
    }
}

/** Localized header subtitle, e.g. "3 kernels · 1 running". */
@Composable
private fun summaryText(serverCount: Int, runningCount: Int): String = when {
    serverCount == 0 -> stringResource(R.string.list_no_servers)
    runningCount > 0 ->
        pluralStringResource(
            R.plurals.list_server_count, serverCount, serverCount,
        ) + " · " + pluralStringResource(
            R.plurals.list_running_count, runningCount, runningCount,
        )
    else -> pluralStringResource(
        R.plurals.list_server_count, serverCount, serverCount,
    )
}

/**
 * One server. The whole card is the Open target (a 44dp text link was a poor
 * touch target); secondary actions sit in the overflow menu, and the supervisor
 * controls appear inline only for servers that opted into management.
 */
@Composable
private fun ServerCard(
    profile: Profile,
    session: SessionManager,
    onActivate: (Profile) -> Unit,
    onEdit: (Profile) -> Unit,
    onDelete: (Profile) -> Unit,
    onStateChange: (ServerState) -> Unit,
) {
    var menuOpen by remember { mutableStateOf(false) }
    var state by remember(profile.id) {
        mutableStateOf(ServerLifecycle.resolve(profile, null))
    }
    val managed = ServerLifecycle.isManaged(profile)

    Card(
        onClick = { onActivate(profile) },
        shape = CardShape,
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
        ),
        // Zero elevation + surface-on-background fill is ~4% contrast in the
        // dark theme; the hairline is what actually separates card from page
        // (same role --border plays on the web's cards).
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                ServerAvatar(profile.alias)
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(
                        profile.alias,
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSurface,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Spacer(Modifier.height(2.dp))
                    Text(
                        ServerUrl.displayLabel(profile.baseUrl),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                if (managed) {
                    StatusChip(state, serverStateLabel(state))
                    Spacer(Modifier.width(2.dp))
                }
                Box {
                    IconButton(onClick = { menuOpen = true }) {
                        Icon(
                            Icons.Default.MoreVert,
                            contentDescription = stringResource(
                                R.string.list_actions_cd, profile.alias,
                            ),
                            tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    DropdownMenu(menuOpen, onDismissRequest = { menuOpen = false }) {
                        DropdownMenuItem(
                            text = { Text(stringResource(R.string.list_menu_edit)) },
                            onClick = { menuOpen = false; onEdit(profile) },
                        )
                        DropdownMenuItem(
                            text = {
                                Text(
                                    stringResource(R.string.list_menu_delete),
                                    color = MaterialTheme.colorScheme.error,
                                )
                            },
                            onClick = { menuOpen = false; onDelete(profile) },
                        )
                    }
                }
            }

            if (managed) {
                Spacer(Modifier.height(12.dp))
                HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                Spacer(Modifier.height(10.dp))
                SupervisorControls(
                    profile = profile,
                    session = session,
                    onStateChange = { state = it; onStateChange(it) },
                    // A server the user just started is a server they want to
                    // use — go straight in rather than making them tap Open.
                    onServerReady = { onActivate(profile) },
                )
            }
        }
    }
}

/** "In front: <kernel>" with its state — which kernel the WebView shows, and how it is. */
@Composable
private fun ActiveKernelLine(active: Profile, kernelState: (Profile) -> StateFlow<KernelState>) {
    val state by remember(active) { kernelState(active) }.collectAsState()
    Row(
        Modifier.fillMaxWidth().padding(start = 20.dp, end = 16.dp, bottom = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            stringResource(R.string.list_in_front, kernelName(active)),
            Modifier.weight(1f, fill = false),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
        )
        Spacer(Modifier.width(8.dp))
        StatusChip(serverStateFor(state), kernelStateLabel(state))
    }
}

/**
 * A LOCAL or SSH kernel: its state comes from its controller, not from the
 * host supervisor. "This phone" has no overflow menu — it is pinned, not a
 * saved server. A failure's reason is spelled out under the title.
 */
@Composable
private fun KernelCard(
    profile: Profile,
    kernelState: (Profile) -> StateFlow<KernelState>,
    onActivate: (Profile) -> Unit,
    onEdit: (Profile) -> Unit,
    onDelete: (Profile) -> Unit,
    onUpChange: (Boolean) -> Unit,
    /** This phone's controls, for the LOCAL row of the app (null elsewhere). */
    thisPhone: LocalKernelHost? = null,
) {
    val state by remember(profile) { kernelState(profile) }.collectAsState()
    val up = state == KernelState.Ready || state == KernelState.SafeMode
    LaunchedEffect(up) { onUpChange(up) }
    var menuOpen by remember { mutableStateOf(false) }
    val local = profile.kind == KernelKind.LOCAL

    Card(
        onClick = { onActivate(profile) },
        shape = CardShape,
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.dp),
        modifier = Modifier.fillMaxWidth(),
    ) {
        Column(Modifier.padding(14.dp)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                if (local) PhoneAvatar() else ServerAvatar(profile.alias)
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text(
                        kernelName(profile),
                        style = MaterialTheme.typography.titleMedium,
                        color = MaterialTheme.colorScheme.onSurface,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                    Spacer(Modifier.height(2.dp))
                    Text(
                        if (local) stringResource(R.string.kernel_this_phone_desc) else profile.ssh?.let(::sshLabel).orEmpty(),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        maxLines = 1,
                        overflow = TextOverflow.Ellipsis,
                    )
                }
                StatusChip(serverStateFor(state), kernelStateLabel(state))
                if (!local) {
                    Spacer(Modifier.width(2.dp))
                    Box {
                        IconButton(onClick = { menuOpen = true }) {
                            Icon(
                                Icons.Default.MoreVert,
                                contentDescription = stringResource(R.string.list_actions_cd, profile.alias),
                                tint = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        DropdownMenu(menuOpen, onDismissRequest = { menuOpen = false }) {
                            DropdownMenuItem(
                                text = { Text(stringResource(R.string.list_menu_edit)) },
                                onClick = { menuOpen = false; onEdit(profile) },
                            )
                            DropdownMenuItem(
                                text = {
                                    Text(
                                        stringResource(R.string.list_menu_delete),
                                        color = MaterialTheme.colorScheme.error,
                                    )
                                },
                                onClick = { menuOpen = false; onDelete(profile) },
                            )
                        }
                    }
                }
            }
            (state as? KernelState.Failed)?.let { failed ->
                Spacer(Modifier.height(10.dp))
                Text(
                    failed.reason,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (local && thisPhone != null) ThisPhoneDetails(thisPhone, profile, state)
        }
    }
}

/** The "This phone" tile, in the same shape as [ServerAvatar]. */
@Composable
private fun PhoneAvatar() {
    Box(
        Modifier
            .size(44.dp)
            .clip(RoundedCornerShape(14.dp))
            .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.14f)),
        contentAlignment = Alignment.Center,
    ) {
        Icon(
            Icons.Filled.PhoneAndroid,
            contentDescription = null,
            tint = MaterialTheme.colorScheme.primary,
            modifier = Modifier.size(22.dp),
        )
    }
}

/** What "Add" offers: a URL kernel (the server form) or an SSH kernel. */
@Composable
private fun AddKernelMenu(expanded: Boolean, onDismiss: () -> Unit, onAddUrl: () -> Unit, onAddSsh: () -> Unit) {
    DropdownMenu(expanded, onDismissRequest = onDismiss) {
        DropdownMenuItem(
            text = { Text(stringResource(R.string.add_kind_url)) },
            leadingIcon = { Icon(Icons.Filled.Language, contentDescription = null) },
            onClick = { onDismiss(); onAddUrl() },
        )
        DropdownMenuItem(
            text = { Text(stringResource(R.string.add_kind_ssh)) },
            leadingIcon = { Icon(Icons.Filled.Terminal, contentDescription = null) },
            onClick = { onDismiss(); onAddSsh() },
        )
    }
}

@Composable
private fun kernelName(profile: Profile): String =
    if (profile.kind == KernelKind.LOCAL) stringResource(R.string.kernel_this_phone) else profile.alias

/** `user@host:port`, bracketing an IPv6 literal. */
private fun sshLabel(endpoint: SshEndpoint): String {
    val host = if (':' in endpoint.host) "[${endpoint.host}]" else endpoint.host
    return "${endpoint.user}@$host:${endpoint.port}"
}

/** Chip colour: the [ServerState] whose meaning matches (one palette for every card). */
private fun serverStateFor(state: KernelState): ServerState = when (state) {
    KernelState.Ready -> ServerState.RUNNING
    KernelState.Starting, KernelState.Stopping, KernelState.SafeMode -> ServerState.TRANSITIONING
    KernelState.Stopped -> ServerState.STOPPED
    is KernelState.Failed -> ServerState.UNREACHABLE
}

@Composable
private fun kernelStateLabel(state: KernelState): String = stringResource(
    when (state) {
        KernelState.Ready -> R.string.kernel_state_ready
        KernelState.Starting -> R.string.kernel_state_starting
        KernelState.Stopping -> R.string.kernel_state_stopping
        KernelState.Stopped -> R.string.kernel_state_stopped
        is KernelState.Failed -> R.string.kernel_state_failed
        KernelState.SafeMode -> R.string.kernel_state_safe_mode
    },
)

@Composable
internal fun StatusBanner(status: UiStatus) {
    val text = when (status) {
        is UiStatus.LoggingIn -> stringResource(R.string.list_signing_in, status.alias)
        is UiStatus.Error -> status.message
        is UiStatus.BadCredentials ->
            stringResource(R.string.list_wrong_password, status.profile.alias)
        UiStatus.Idle -> null
    }
    val isError = status is UiStatus.Error || status is UiStatus.BadCredentials
    AnimatedVisibility(
        visible = text != null,
        enter = fadeIn() + expandVertically(),
        exit = fadeOut() + shrinkVertically(),
    ) {
        val tint = if (isError) MaterialTheme.colorScheme.error
        else MaterialTheme.colorScheme.primary
        Row(
            Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 4.dp)
                .clip(RoundedCornerShape(12.dp))
                .background(tint.copy(alpha = 0.10f))
                .padding(horizontal = 14.dp, vertical = 11.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text.orEmpty(),
                style = MaterialTheme.typography.bodyMedium,
                color = tint,
            )
        }
    }
}

/**
 * First-run state. The old copy explained the app's mechanics; this explains
 * the payoff and gives the user the one action that matters.
 */
@Composable
private fun EmptyState(onAdd: () -> Unit, onAddSsh: () -> Unit) {
    Column(
        Modifier.fillMaxSize().padding(horizontal = 40.dp),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Box(
            Modifier
                .size(72.dp)
                .clip(RoundedCornerShape(22.dp))
                .background(MaterialTheme.colorScheme.primary.copy(alpha = 0.12f)),
            contentAlignment = Alignment.Center,
        ) {
            Icon(
                Icons.Default.Dns,
                contentDescription = null,
                tint = MaterialTheme.colorScheme.primary,
                modifier = Modifier.size(32.dp),
            )
        }
        Spacer(Modifier.height(20.dp))
        Text(
            stringResource(R.string.list_empty_title),
            style = MaterialTheme.typography.titleLarge,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            stringResource(R.string.list_empty_body),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        var addOpen by remember { mutableStateOf(false) }
        Box {
            FilledTonalButton(onClick = { addOpen = true }, shape = TofuButtonShape) {
                Icon(Icons.Default.Add, null, Modifier.size(18.dp))
                Spacer(Modifier.width(8.dp))
                Text(stringResource(R.string.list_empty_cta))
            }
            AddKernelMenu(addOpen, { addOpen = false }, onAdd, onAddSsh)
        }
    }
}

/**
 * Version footer. Reads [BuildConfig] so the shipped build is always
 * identifiable in-app and can never drift from the manifest.
 */
@Composable
private fun VersionFooter() {
    Text(
        stringResource(
            R.string.list_footer_version,
            BuildConfig.VERSION_NAME, BuildConfig.VERSION_CODE,
        ),
        Modifier.fillMaxWidth().navigationBarsPadding().padding(bottom = 10.dp),
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f),
        textAlign = TextAlign.Center,
    )
}
