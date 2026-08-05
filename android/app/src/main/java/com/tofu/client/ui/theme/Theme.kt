package com.tofu.client.ui.theme

import android.app.Activity
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Typography
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Shape
import androidx.compose.ui.platform.LocalView
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.Shapes
import androidx.core.view.WindowCompat

/**
 * The app's design tokens, ported from the web SPA's CSS custom properties
 * (`chatui/static/styles.css` `:root`) so the native shell and the page it hosts
 * read as ONE product instead of a stock-Material chrome wrapped around a
 * purple web app.
 *
 * Deliberately NOT Material You / dynamic color: the WebView content is styled
 * by the SPA's fixed palette, so letting the launcher wallpaper recolor the
 * native chrome would make the shell clash with its own content on most phones.
 * A branded, stable palette is the whole point.
 */
object TofuTokens {
    // ── Dark (primary brand surface — matches the SPA's default theme) ──
    val BgPrimary = Color(0xFF0A0A0C)     // --bg-primary
    val BgSecondary = Color(0xFF111115)   // --bg-secondary
    val BgTertiary = Color(0xFF1A1A21)    // --bg-tertiary
    val BgHover = Color(0xFF22222B)       // --bg-hover
    val BorderDark = Color(0xFF2A2A35)    // --border
    val BorderLight = Color(0xFF33333F)   // --border-light
    val TextPrimary = Color(0xFFE8E8ED)   // --text-primary
    val TextSecondary = Color(0xFF9898A8) // --text-secondary
    val TextTertiary = Color(0xFF6A6A7A)  // --text-tertiary

    // ── Accent (the Tofu violet — identical to --accent) ──
    val Accent = Color(0xFF6E56CF)
    val AccentHover = Color(0xFF7C66D4)
    val AccentSoft = Color(0xFFA78BFA)

    // ── Semantic ──
    val Ok = Color(0xFF10B981)            // --fetch-green: server running
    val Warn = Color(0xFFCFB850)          // --thinking-text: needs attention
    val Danger = Color(0xFFCF5050)        // --error-text

    // ── Light mode: the warm cream of the launcher icon / welcome screen ──
    val CreamBg = Color(0xFFFBF7EE)
    val CreamSurface = Color(0xFFFFFFFF)
    val CreamSurfaceAlt = Color(0xFFF3EEE1)
    val CreamBorder = Color(0xFFE3DACA)
    val InkPrimary = Color(0xFF1E1B2E)
    val InkSecondary = Color(0xFF5C5870)

    /** Per-server monogram tile colors, mirroring the SPA's project palette. */
    val MonogramPalette = listOf(
        Color(0xFF6E56CF), Color(0xFF3B82F6), Color(0xFF06B6D4),
        Color(0xFF10B981), Color(0xFFF59E0B), Color(0xFFEF4444),
        Color(0xFF8B5CF6), Color(0xFFEC4899),
    )
}

/** Stable per-alias accent so a server keeps its identity color across launches. */
fun monogramColorFor(seed: String): Color {
    if (seed.isEmpty()) return TofuTokens.Accent
    val h = seed.fold(0) { acc, c -> (acc * 31 + c.code) and 0x7FFFFFFF }
    return TofuTokens.MonogramPalette[h % TofuTokens.MonogramPalette.size]
}

/** 1–2 char monogram for a server tile (CJK-safe: takes whole code points). */
fun monogramOf(alias: String): String {
    val trimmed = alias.trim()
    if (trimmed.isEmpty()) return "?"
    val first = trimmed.first()
    // A CJK alias reads better as two glyphs; a latin one as a single initial.
    return if (first.code > 0x2E80) trimmed.take(2) else first.uppercase()
}

private val DarkScheme = darkColorScheme(
    primary = TofuTokens.Accent,
    onPrimary = Color.White,
    primaryContainer = TofuTokens.Accent.copy(alpha = 0.18f),
    onPrimaryContainer = TofuTokens.AccentSoft,
    secondary = TofuTokens.AccentSoft,
    onSecondary = Color.White,
    background = TofuTokens.BgPrimary,
    onBackground = TofuTokens.TextPrimary,
    surface = TofuTokens.BgSecondary,
    onSurface = TofuTokens.TextPrimary,
    surfaceVariant = TofuTokens.BgTertiary,
    onSurfaceVariant = TofuTokens.TextSecondary,
    surfaceContainerHighest = TofuTokens.BgHover,
    outline = TofuTokens.BorderLight,
    outlineVariant = TofuTokens.BorderDark,
    error = TofuTokens.Danger,
    onError = Color.White,
    errorContainer = Color(0x33CF3838),
    onErrorContainer = TofuTokens.Danger,
)

private val LightScheme = lightColorScheme(
    primary = TofuTokens.Accent,
    onPrimary = Color.White,
    primaryContainer = Color(0xFFEBE4FF),
    onPrimaryContainer = Color(0xFF3B2E80),
    secondary = Color(0xFF7C66D4),
    onSecondary = Color.White,
    background = TofuTokens.CreamBg,
    onBackground = TofuTokens.InkPrimary,
    surface = TofuTokens.CreamSurface,
    onSurface = TofuTokens.InkPrimary,
    surfaceVariant = TofuTokens.CreamSurfaceAlt,
    onSurfaceVariant = TofuTokens.InkSecondary,
    surfaceContainerHighest = TofuTokens.CreamSurfaceAlt,
    outline = TofuTokens.CreamBorder,
    outlineVariant = TofuTokens.CreamBorder,
    error = Color(0xFFB3261E),
    onError = Color.White,
)

/**
 * Tighter, more editorial type scale than stock Material3 — the default
 * headline sizes are tuned for consumer apps and read oversized in a dense
 * server/config tool.
 */
private val TofuTypography = Typography(
    headlineSmall = TextStyle(
        fontSize = 22.sp, lineHeight = 28.sp, fontWeight = FontWeight.Bold,
        letterSpacing = (-0.4).sp,
    ),
    titleLarge = TextStyle(
        fontSize = 19.sp, lineHeight = 25.sp, fontWeight = FontWeight.Bold,
        letterSpacing = (-0.2).sp,
    ),
    titleMedium = TextStyle(
        fontSize = 15.5.sp, lineHeight = 21.sp, fontWeight = FontWeight.SemiBold,
        letterSpacing = 0.sp,
    ),
    titleSmall = TextStyle(
        fontSize = 13.5.sp, lineHeight = 18.sp, fontWeight = FontWeight.SemiBold,
    ),
    bodyLarge = TextStyle(fontSize = 15.sp, lineHeight = 22.sp),
    bodyMedium = TextStyle(fontSize = 13.5.sp, lineHeight = 19.sp),
    bodySmall = TextStyle(fontSize = 12.sp, lineHeight = 16.sp),
    labelLarge = TextStyle(
        fontSize = 13.5.sp, lineHeight = 18.sp, fontWeight = FontWeight.SemiBold,
        letterSpacing = 0.1.sp,
    ),
    labelMedium = TextStyle(
        fontSize = 11.5.sp, lineHeight = 15.sp, fontWeight = FontWeight.SemiBold,
        letterSpacing = 0.3.sp,
    ),
    labelSmall = TextStyle(
        fontSize = 10.sp, lineHeight = 13.sp, fontWeight = FontWeight.Bold,
        letterSpacing = 0.8.sp,
    ),
)

/** Radii from the SPA (--radius 12 / --radius-sm 8 / --radius-xs 6). */
private val TofuShapes = Shapes(
    extraSmall = RoundedCornerShape(6.dp),
    small = RoundedCornerShape(8.dp),
    medium = RoundedCornerShape(12.dp),
    large = RoundedCornerShape(18.dp),
    extraLarge = RoundedCornerShape(26.dp),
)

/** Convenience alias so screens can name a shape without importing Shapes. */
val CardShape: Shape = RoundedCornerShape(16.dp)

@Composable
fun TofuTheme(
    darkTheme: Boolean = isSystemInDarkTheme(),
    content: @Composable () -> Unit,
) {
    val scheme = if (darkTheme) DarkScheme else LightScheme
    val view = LocalView.current
    if (!view.isInEditMode) {
        SideEffect {
            // Edge-to-edge: the app draws behind the system bars (screens apply
            // their own insets), so the bar icons must be contrasted against
            // OUR background, not the platform default.
            val window = (view.context as Activity).window
            WindowCompat.getInsetsController(window, view).apply {
                isAppearanceLightStatusBars = !darkTheme
                isAppearanceLightNavigationBars = !darkTheme
            }
        }
    }
    MaterialTheme(
        colorScheme = scheme,
        typography = TofuTypography,
        shapes = TofuShapes,
        content = content,
    )
}
