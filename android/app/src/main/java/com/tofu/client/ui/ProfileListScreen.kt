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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Dns
import androidx.compose.material.icons.filled.MoreVert
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
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.tofu.client.BuildConfig
import com.tofu.client.data.Profile
import com.tofu.client.session.ServerLifecycle
import com.tofu.client.session.ServerState
import com.tofu.client.session.ServerUrl
import com.tofu.client.session.SessionManager
import com.tofu.client.ui.theme.CardShape
import com.tofu.client.ui.theme.TofuButtonShape

/**
 * Home — the server list / switcher.
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
    profiles: List<Profile>,
    status: UiStatus,
    onActivate: (Profile) -> Unit,
    onEdit: (Profile) -> Unit,
    onDelete: (Profile) -> Unit,
    onAdd: () -> Unit,
    /**
     * Establishes a session on demand so a STOPPED server can still be started
     * — code-server stays up while Tofu is down. Non-null by contract: a null
     * here would silently disable login-then-act and re-create the
     * start-a-stopped-server deadlock.
     */
    session: SessionManager,
) {
    // Per-profile lifecycle state, lifted here so the header can summarize how
    // many servers are up without each card re-polling.
    val states = remember { mutableStateMapOf<Long, ServerState>() }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = onAdd,
                icon = { Icon(Icons.Default.Add, contentDescription = null) },
                text = { Text("Add server", fontWeight = FontWeight.SemiBold) },
                containerColor = MaterialTheme.colorScheme.primary,
                contentColor = MaterialTheme.colorScheme.onPrimary,
            )
        },
    ) { pad ->
        Column(Modifier.padding(pad).fillMaxSize()) {
            HomeHeader(
                serverCount = profiles.size,
                runningCount = states.values.count { it == ServerState.RUNNING },
            )
            StatusBanner(status)
            Box(Modifier.weight(1f).fillMaxWidth()) {
                if (profiles.isEmpty()) {
                    EmptyState(onAdd)
                } else {
                    LazyColumn(
                        Modifier.fillMaxSize(),
                        contentPadding = PaddingValues(
                            start = 16.dp, end = 16.dp, top = 4.dp, bottom = 96.dp,
                        ),
                        verticalArrangement = Arrangement.spacedBy(12.dp),
                    ) {
                        items(profiles, key = { it.id }) { p ->
                            ServerCard(
                                profile = p,
                                session = session,
                                onActivate = onActivate,
                                onEdit = onEdit,
                                onDelete = onDelete,
                                onStateChange = { states[p.id] = it },
                            )
                        }
                    }
                }
            }
            VersionFooter()
        }
    }
}

/**
 * Branded header. Replaces the stock TopAppBar: it carries the wordmark plus a
 * live one-line summary ("3 servers · 1 running"), which is the single most
 * useful thing to know on opening the app.
 */
@Composable
private fun HomeHeader(serverCount: Int, runningCount: Int) {
    Column(
        Modifier
            .fillMaxWidth()
            .statusBarsPadding()
            .padding(start = 20.dp, end = 20.dp, top = 20.dp, bottom = 14.dp),
    ) {
        Row(verticalAlignment = Alignment.CenterVertically) {
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
        }
        Spacer(Modifier.height(3.dp))
        Text(
            summaryLine(serverCount, runningCount),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

/** Pure text rule for the header subtitle — kept small and obvious. */
private fun summaryLine(serverCount: Int, runningCount: Int): String = when {
    serverCount == 0 -> "No servers yet"
    runningCount > 0 -> "$serverCount ${plural(serverCount)} · $runningCount running"
    else -> "$serverCount ${plural(serverCount)}"
}

private fun plural(n: Int) = if (n == 1) "server" else "servers"

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
                    StatusChip(state, ServerLifecycle.label(state))
                    Spacer(Modifier.width(2.dp))
                }
                Box {
                    IconButton(onClick = { menuOpen = true }) {
                        Icon(
                            Icons.Default.MoreVert,
                            contentDescription = "More actions for ${profile.alias}",
                            tint = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    DropdownMenu(menuOpen, onDismissRequest = { menuOpen = false }) {
                        DropdownMenuItem(
                            text = { Text("Edit") },
                            onClick = { menuOpen = false; onEdit(profile) },
                        )
                        DropdownMenuItem(
                            text = {
                                Text("Delete", color = MaterialTheme.colorScheme.error)
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

@Composable
private fun StatusBanner(status: UiStatus) {
    val text = when (status) {
        is UiStatus.LoggingIn -> "Signing in to ${status.alias}…"
        is UiStatus.Error -> status.message
        is UiStatus.BadCredentials ->
            "Wrong password for ${status.profile.alias} — edit it to fix."
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
private fun EmptyState(onAdd: () -> Unit) {
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
            "Your servers live here",
            style = MaterialTheme.typography.titleLarge,
            color = MaterialTheme.colorScheme.onBackground,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            "Add a Tofu server once — its address and password are remembered, " +
                "so opening it later is a single tap.",
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
        Spacer(Modifier.height(24.dp))
        FilledTonalButton(onClick = onAdd, shape = TofuButtonShape) {
            Icon(Icons.Default.Add, null, Modifier.size(18.dp))
            Spacer(Modifier.width(8.dp))
            Text("Add your first server")
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
        "Tofu v${BuildConfig.VERSION_NAME} (${BuildConfig.VERSION_CODE})",
        Modifier.fillMaxWidth().navigationBarsPadding().padding(bottom = 10.dp),
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant.copy(alpha = 0.6f),
        textAlign = TextAlign.Center,
    )
}
