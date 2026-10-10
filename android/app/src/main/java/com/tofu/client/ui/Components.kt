package com.tofu.client.ui

import androidx.compose.animation.animateColorAsState
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.defaultMinSize
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import com.tofu.client.R
import com.tofu.client.session.ServerState
import com.tofu.client.ui.theme.TofuTokens
import com.tofu.client.ui.theme.monogramColorFor
import com.tofu.client.ui.theme.monogramOf

/**
 * Shared visual primitives so every screen speaks the same language. These
 * exist because the previous UI hand-rolled each affordance inline, which is
 * why the app read as "a form and a list" rather than a product.
 */

/**
 * A server's identity tile — a colored rounded square with a monogram, the
 * same device the web sidebar uses for projects. Gives each server an
 * instantly recognizable identity in a list of near-identical long URLs.
 */
@Composable
fun ServerAvatar(alias: String, size: Int = 44, modifier: Modifier = Modifier) {
    val color = monogramColorFor(alias)
    Box(
        modifier
            .size(size.dp)
            .clip(RoundedCornerShape((size * 0.32f).dp))
            .background(color.copy(alpha = 0.18f))
            .border((1.2f).dp, color.copy(alpha = 0.5f), RoundedCornerShape((size * 0.32f).dp)),
        contentAlignment = Alignment.Center,
    ) {
        Text(
            monogramOf(alias),
            color = color,
            fontWeight = FontWeight.Bold,
            style = if (size >= 40) MaterialTheme.typography.titleMedium
            else MaterialTheme.typography.labelMedium,
        )
    }
}

/** Semantic color for a lifecycle state — one place, so the chip and the dot agree. */
@Composable
fun colorForState(state: ServerState): Color = when (state) {
    ServerState.RUNNING -> TofuTokens.Ok
    ServerState.STOPPED -> MaterialTheme.colorScheme.onSurfaceVariant
    ServerState.TRANSITIONING -> TofuTokens.Warn
    ServerState.UNREACHABLE -> TofuTokens.Danger
    ServerState.UNKNOWN -> MaterialTheme.colorScheme.onSurfaceVariant
    ServerState.UNMANAGED -> MaterialTheme.colorScheme.onSurfaceVariant
}

/**
 * Live status dot. RUNNING breathes so a healthy server reads as *alive* at a
 * glance without needing to parse text — the one animation in the app that
 * carries information rather than decoration.
 */
@Composable
fun StatusDot(state: ServerState, modifier: Modifier = Modifier, dotSize: Int = 8) {
    val base = colorForState(state)
    val color by animateColorAsState(base, label = "statusDot")
    val pulsing = state == ServerState.RUNNING || state == ServerState.TRANSITIONING
    val transition = rememberInfiniteTransition(label = "pulse")
    val pulseAlpha by transition.animateFloat(
        initialValue = 1f,
        targetValue = 0.35f,
        animationSpec = infiniteRepeatable(tween(1100), RepeatMode.Reverse),
        label = "pulseAlpha",
    )
    val alpha = if (pulsing) pulseAlpha else 1f
    Canvas(modifier.size(dotSize.dp)) {
        // Soft halo, then the core — reads as a glow rather than a flat pip.
        drawCircle(color.copy(alpha = alpha * 0.25f), radius = size.minDimension / 2f)
        drawCircle(color.copy(alpha = alpha), radius = size.minDimension / 3.2f)
    }
}

/** A compact status pill: dot + word, used on every server card. */
@Composable
fun StatusChip(state: ServerState, label: String, modifier: Modifier = Modifier) {
    val color = colorForState(state)
    Row(
        modifier
            .clip(CircleShape)
            .background(color.copy(alpha = 0.12f))
            .padding(horizontal = 9.dp, vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        StatusDot(state)
        Spacer(Modifier.width(6.dp))
        Text(
            label,
            style = MaterialTheme.typography.labelMedium,
            color = color,
        )
    }
}

/** Section heading — small, uppercase, tracked-out, like the web rail titles. */
@Composable
fun SectionLabel(text: String, modifier: Modifier = Modifier) {
    Text(
        text.uppercase(),
        modifier,
        style = MaterialTheme.typography.labelSmall,
        color = MaterialTheme.colorScheme.onSurfaceVariant,
    )
}

/**
 * Max width of a single content column. Wider than this reads as a phone
 * layout stretched across a tablet; 600dp is the usual single-column cap.
 */
internal val ContentMaxWidth = 600.dp

/**
 * Wider cap for the server list, which lays out in an adaptive grid: on a
 * tablet this lets two cards sit side by side instead of one stretched column.
 */
internal val ListMaxWidth = 840.dp

/**
 * Center a single column at [ContentMaxWidth] on wide screens, full width on
 * phones. The web SPA inside WebScreen is intentionally NOT wrapped — it owns
 * its own responsive layout.
 */
@Composable
fun ContentWidth(content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        Box(Modifier.widthIn(max = ContentMaxWidth).fillMaxHeight()) {
            content()
        }
    }
}

/**
 * A [Column] capped at [ContentMaxWidth] and centered on wide screens, carrying
 * the scaffold's [padding] — the one-line container for list/form screens so
 * their rows and fields don't stretch edge-to-edge on a tablet.
 */
@Composable
fun CappedContent(
    padding: PaddingValues,
    maxWidth: Dp = ContentMaxWidth,
    floatingActionButton: (@Composable () -> Unit)? = null,
    content: @Composable ColumnScope.() -> Unit,
) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        Box(Modifier.widthIn(max = maxWidth).fillMaxHeight()) {
            Column(
                Modifier
                    .fillMaxSize()
                    .padding(padding),
                content = content,
            )
            // The FAB lives INSIDE the capped column, so on a tablet it sits at
            // the content's edge rather than floating off in the screen corner.
            floatingActionButton?.let { fab ->
                Box(Modifier.align(Alignment.BottomEnd).padding(16.dp)) { fab() }
            }
        }
    }
}

/** [CappedContent] with a vertical scroll, for taller-than-screen forms. */
@Composable
fun CappedScrollContent(
    padding: PaddingValues,
    content: @Composable ColumnScope.() -> Unit,
) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.TopCenter) {
        Column(
            Modifier
                .widthIn(max = ContentMaxWidth)
                .fillMaxHeight()
                .padding(padding)
                .verticalScroll(rememberScrollState()),
            content = content,
        )
    }
}

/**
 * Minimum accessible touch target. Material3's default button height is 40dp,
 * below the 48dp accessibility guideline; every shell control that is not
 * already taller applies this so small inline buttons stay tappable.
 */
internal val TouchTarget = Modifier.defaultMinSize(minHeight = 48.dp)

/**
 * Localizable status word for a [ServerState]. Kept in the UI (not in
 * [com.tofu.client.session.ServerLifecycle], whose English labels the pure
 * tests assert) so the chip follows the device language.
 */
@Composable
fun serverStateLabel(state: ServerState): String = stringResource(
    when (state) {
        ServerState.UNMANAGED -> R.string.state_open_only
        ServerState.UNKNOWN -> R.string.state_checking
        ServerState.RUNNING -> R.string.state_running
        ServerState.STOPPED -> R.string.state_stopped
        ServerState.TRANSITIONING -> R.string.state_working
        ServerState.UNREACHABLE -> R.string.state_unreachable
    },
)
