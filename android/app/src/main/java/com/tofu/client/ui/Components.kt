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
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
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
