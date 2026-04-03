"""lib/trading_strategy_engine/signals — Multi-timeframe signal generation & smoothing.

Sections:
  1. Multi-timeframe signal confirmation (short/medium/long)
  2. Signal smoothing & whipsaw prevention (EMA, persistence, hysteresis)
"""

from lib.trading_signals import (
    bollinger_bands,
    compute_signal_series,
    compute_signal_snapshot,
    daily_returns,
    detect_trend_regime,
    detect_volatility_regime,
    ema,
    macd,
    momentum,
    rolling_max_drawdown,
    rolling_volatility,
    rsi,
    sma,
)

__all__ = [
    'compute_multi_timeframe_signal',
    'compute_smoothed_signal_series',
]


# ═══════════════════════════════════════════════════════════
#  Multi-Timeframe Signal Confirmation
# ═══════════════════════════════════════════════════════════

def compute_multi_timeframe_signal(navs):
    """Compute signals across 3 timeframes and synthesize a confirmed signal.

    Timeframes:
      - Short  (5-20 day indicators): Entry/exit timing
      - Medium (20-60 day indicators): Trend confirmation
      - Long   (60-120 day indicators): Major trend direction

    Agreement across timeframes dramatically increases signal reliability.

    Returns:
        {
            short_signal, medium_signal, long_signal,
            confirmed_signal, confirmation_strength,
            timeframe_alignment, composite_multi_tf_score,
            individual_scores, reliability_rating
        }
    """
    if not navs or len(navs) < 120:
        return {
            'error': 'Need >= 120 days for multi-timeframe analysis',
            'data_points': len(navs) if navs else 0,
        }

    n = len(navs)
    last = n - 1
    current_nav = navs[last]['nav']

    # ── Short-term signals (5-20 day) ──
    ma5 = sma(navs, 5)
    ma10 = sma(navs, 10)
    rsi_7 = rsi(navs, 7)   # faster RSI for short-term
    mom_5 = momentum(navs, 5)

    short_score = 0
    short_signals = {}

    # MA5 > MA10 and price > MA5
    if ma5[last] and ma10[last]:
        if ma5[last] > ma10[last]:
            short_score += 25
            short_signals['ma_cross'] = 'bullish'
        else:
            short_score -= 25
            short_signals['ma_cross'] = 'bearish'

    if ma5[last] and current_nav > ma5[last]:
        short_score += 15
    elif ma5[last]:
        short_score -= 15

    # RSI-7 zones
    r7 = rsi_7[last]
    if r7 is not None:
        short_signals['rsi_7'] = round(r7, 1)
        if r7 < 25:
            short_score += 30  # extreme oversold — bounce likely
        elif r7 < 35:
            short_score += 15
        elif r7 > 75:
            short_score -= 30  # extreme overbought
        elif r7 > 65:
            short_score -= 15

    # Short-term momentum
    m5 = mom_5[last]
    if m5 is not None:
        short_signals['momentum_5d'] = round(m5, 2)
        if m5 > 3:
            short_score += 20
        elif m5 > 1:
            short_score += 10
        elif m5 < -3:
            short_score -= 20
        elif m5 < -1:
            short_score -= 10

    short_score = max(-100, min(100, short_score))

    # ── Medium-term signals (20-60 day) ──
    ma20 = sma(navs, 20)
    ma60 = sma(navs, 60)
    rsi_14 = rsi(navs, 14)
    macd_l, macd_s, macd_h = macd(navs)
    bb_up, bb_mid, bb_low, bb_width = bollinger_bands(navs, 20)
    vol_20 = rolling_volatility(navs, 20)
    mom_20 = momentum(navs, 20)
    trend_20 = detect_trend_regime(navs, 20, 60)

    medium_score = 0
    medium_signals = {}

    # MA20 vs MA60
    if ma20[last] and ma60[last]:
        gap = (ma20[last] - ma60[last]) / ma60[last] * 100
        medium_signals['ma20_60_gap_pct'] = round(gap, 2)
        if gap > 2:
            medium_score += 25
        elif gap > 0:
            medium_score += 10
        elif gap < -2:
            medium_score -= 25
        else:
            medium_score -= 10

    # Price vs MA20
    if ma20[last]:
        dist = (current_nav - ma20[last]) / ma20[last] * 100
        medium_signals['dist_from_ma20'] = round(dist, 2)
        if dist > 5:
            medium_score -= 10  # too extended
        elif dist > 0:
            medium_score += 15
        elif dist < -5:
            medium_score += 10  # potential mean reversion
        else:
            medium_score -= 15

    # RSI-14
    r14 = rsi_14[last]
    if r14 is not None:
        medium_signals['rsi_14'] = round(r14, 1)
        if r14 < 30:
            medium_score += 25
        elif r14 < 45:
            medium_score += 10
        elif r14 > 70:
            medium_score -= 25
        elif r14 > 55:
            medium_score -= 10

    # MACD
    if macd_h[last] is not None:
        medium_signals['macd_histogram'] = round(macd_h[last], 6)
        if macd_h[last] > 0:
            medium_score += 20
            # Check if histogram is growing
            if last >= 1 and macd_h[last - 1] is not None and macd_h[last] > macd_h[last - 1]:
                medium_score += 10
                medium_signals['macd_momentum'] = 'accelerating'
        else:
            medium_score -= 20
            if last >= 1 and macd_h[last - 1] is not None and macd_h[last] < macd_h[last - 1]:
                medium_score -= 10
                medium_signals['macd_momentum'] = 'decelerating'

    # Bollinger position
    if bb_up[last] and bb_low[last] and bb_up[last] != bb_low[last]:
        bb_pos = (current_nav - bb_low[last]) / (bb_up[last] - bb_low[last]) * 100
        medium_signals['bb_position'] = round(bb_pos, 1)
        if bb_pos < 10:
            medium_score += 15
        elif bb_pos > 90:
            medium_score -= 15

    # Trend regime
    if trend_20[last]:
        medium_signals['trend_regime'] = trend_20[last]
        regime_scores = {
            'strong_bull': 30, 'bull': 15, 'sideways': 0,
            'bear': -15, 'strong_bear': -30,
        }
        medium_score += regime_scores.get(trend_20[last], 0)

    medium_score = max(-100, min(100, medium_score))

    # ── Long-term signals (60-120 day) ──
    ma120 = sma(navs, min(120, n))
    rsi_28 = rsi(navs, 28)
    vol_60 = rolling_volatility(navs, 60)
    mom_60 = momentum(navs, 60)
    dd_120 = rolling_max_drawdown(navs, min(120, n))
    trend_60 = detect_trend_regime(navs, 60, min(120, n))

    long_score = 0
    long_signals = {}

    # Price vs MA120
    if len(navs) >= 120 and ma120[last]:
        dist120 = (current_nav - ma120[last]) / ma120[last] * 100
        long_signals['dist_from_ma120'] = round(dist120, 2)
        if dist120 > 0:
            long_score += 20
        else:
            long_score -= 20

    # MA60 vs MA120
    if len(navs) >= 120 and ma60[last] and ma120[last]:
        gap60_120 = (ma60[last] - ma120[last]) / ma120[last] * 100
        long_signals['ma60_120_gap_pct'] = round(gap60_120, 2)
        if gap60_120 > 0:
            long_score += 20
        else:
            long_score -= 20

    # Long-term RSI
    r28 = rsi_28[last] if len(rsi_28) > last else None
    if r28 is not None:
        long_signals['rsi_28'] = round(r28, 1)
        if r28 < 35:
            long_score += 20
        elif r28 > 65:
            long_score -= 20

    # Long-term momentum
    m60 = mom_60[last]
    if m60 is not None:
        long_signals['momentum_60d'] = round(m60, 2)
        if m60 > 10:
            long_score += 25
        elif m60 > 0:
            long_score += 10
        elif m60 < -10:
            long_score -= 25
        else:
            long_score -= 10

    # Max drawdown severity
    dd120 = dd_120[last]
    if dd120 is not None:
        long_signals['max_dd_120d'] = round(dd120, 2)
        if dd120 > -5:
            long_score += 15
        elif dd120 > -10:
            long_score += 5
        elif dd120 < -20:
            long_score -= 25
        elif dd120 < -15:
            long_score -= 15

    # Long-term trend
    if len(navs) >= 120 and trend_60[last]:
        long_signals['trend_regime_long'] = trend_60[last]
        regime_scores = {
            'strong_bull': 25, 'bull': 12, 'sideways': 0,
            'bear': -12, 'strong_bear': -25,
        }
        long_score += regime_scores.get(trend_60[last], 0)

    long_score = max(-100, min(100, long_score))

    # ── Synthesize multi-timeframe signal ──
    # Agreement scoring
    all_positive = short_score > 0 and medium_score > 0 and long_score > 0
    all_negative = short_score < 0 and medium_score < 0 and long_score < 0
    # Count agreements
    signs = [1 if s > 10 else (-1 if s < -10 else 0)
             for s in [short_score, medium_score, long_score]]
    agreement_count = max(signs.count(1), signs.count(-1))

    if all_positive:
        alignment = 'fully_bullish'
        confirmation_strength = min(short_score, medium_score, long_score)
    elif all_negative:
        alignment = 'fully_bearish'
        confirmation_strength = max(short_score, medium_score, long_score)  # least negative
    elif agreement_count >= 2:
        dominant = 1 if signs.count(1) >= 2 else -1
        alignment = 'mostly_bullish' if dominant > 0 else 'mostly_bearish'
        confirmation_strength = min(abs(short_score), abs(medium_score), abs(long_score)) * 0.6
        if dominant < 0:
            confirmation_strength = -confirmation_strength
    else:
        alignment = 'conflicting'
        confirmation_strength = 0

    # Weighted composite (long-term direction matters most for trading)
    composite_multi_tf = (
        short_score * 0.20 +    # timing weight
        medium_score * 0.40 +   # trend confirmation (most important)
        long_score * 0.40       # major trend direction
    )

    # Reliability rating based on agreement and signal strength
    avg_abs_score = (abs(short_score) + abs(medium_score) + abs(long_score)) / 3
    if all_positive or all_negative:
        reliability = min(95, 60 + avg_abs_score * 0.35)
    elif agreement_count >= 2:
        reliability = min(80, 40 + avg_abs_score * 0.30)
    else:
        reliability = max(10, 30 - avg_abs_score * 0.10)

    # Classify confirmed signal
    if composite_multi_tf >= 40 and reliability >= 60:
        confirmed_signal = 'strong_buy'
    elif composite_multi_tf >= 15 and reliability >= 40:
        confirmed_signal = 'buy'
    elif composite_multi_tf <= -40 and reliability >= 60:
        confirmed_signal = 'strong_sell'
    elif composite_multi_tf <= -15 and reliability >= 40:
        confirmed_signal = 'sell'
    elif abs(composite_multi_tf) < 10:
        confirmed_signal = 'neutral'
    else:
        confirmed_signal = 'weak_' + ('buy' if composite_multi_tf > 0 else 'sell')

    return {
        'date': navs[last]['date'],
        'nav': current_nav,
        'data_points': n,

        'short_term': {
            'score': round(short_score, 1),
            'signal': _score_to_signal(short_score),
            'indicators': short_signals,
        },
        'medium_term': {
            'score': round(medium_score, 1),
            'signal': _score_to_signal(medium_score),
            'indicators': medium_signals,
        },
        'long_term': {
            'score': round(long_score, 1),
            'signal': _score_to_signal(long_score),
            'indicators': long_signals,
        },

        'confirmed_signal': confirmed_signal,
        'composite_multi_tf_score': round(composite_multi_tf, 1),
        'confirmation_strength': round(confirmation_strength, 1),
        'timeframe_alignment': alignment,
        'reliability_rating': round(reliability, 1),

        'volatility_20d': round(vol_20[last] * 100, 2) if vol_20[last] else None,
        'volatility_60d': round(vol_60[last] * 100, 2) if vol_60[last] else None,
        'volatility_regime': detect_volatility_regime(navs)[last],
    }


def _score_to_signal(score):
    """Convert a numeric score to a signal label."""
    if score >= 40:
        return 'strong_buy'
    elif score >= 15:
        return 'buy'
    elif score > -15:
        return 'neutral'
    elif score > -40:
        return 'sell'
    return 'strong_sell'


# ═══════════════════════════════════════════════════════════
#  Signal Smoothing & Whipsaw Prevention
# ═══════════════════════════════════════════════════════════

def compute_smoothed_signal_series(navs, smoothing_period=5, persistence_days=3):
    """Compute signal series with smoothing and whipsaw prevention.

    Smoothing: EMA of raw signal scores to reduce noise.
    Persistence: Signal must maintain direction for N days before acting.
    Hysteresis: Once a signal is triggered, it stays until a stronger
                opposing signal appears (not just crossing zero).

    Args:
        navs: price history sorted by date ASC
        smoothing_period: EMA period for signal smoothing
        persistence_days: Days the signal must persist before triggering

    Returns:
        List of (index, date, smoothed_snapshot) with additional fields:
          smoothed_score, raw_score, persistent_signal, signal_age,
          whipsaw_blocked
    """
    MIN_HISTORY = 60
    if len(navs) < MIN_HISTORY + smoothing_period:
        return []

    # First compute raw signal series
    raw_series = compute_signal_series(navs, compute_every=1)
    if not raw_series:
        return []

    # Extract raw scores
    raw_scores = [s[2].get('composite_score', 0) for s in raw_series]
    # Apply EMA smoothing to scores
    smoothed_scores = _ema_smooth(raw_scores, smoothing_period)

    # Apply persistence and hysteresis
    smoothed_series = []
    current_signal = 'neutral'
    signal_age = 0
    consecutive_direction = 0
    last_direction = 0

    # Hysteresis thresholds (stronger signal needed to reverse)
    ENTRY_THRESHOLD = 10      # Score needed to trigger a new signal
    EXIT_THRESHOLD = -5       # Score needed to reverse (from the opposite side)

    for i, (idx, date, snap) in enumerate(raw_series):
        raw_score = raw_scores[i]
        smooth_score = smoothed_scores[i] if i < len(smoothed_scores) else raw_score

        # Track consecutive direction
        if smooth_score > 5:
            direction = 1
        elif smooth_score < -5:
            direction = -1
        else:
            direction = 0

        if direction == last_direction and direction != 0:
            consecutive_direction += 1
        else:
            consecutive_direction = 1 if direction != 0 else 0
        last_direction = direction

        # Whipsaw prevention: require persistence
        whipsaw_blocked = False
        if consecutive_direction >= persistence_days:
            # Signal has persisted long enough
            if direction > 0 and smooth_score >= ENTRY_THRESHOLD:
                if current_signal not in ('buy', 'strong_buy'):
                    current_signal = 'buy' if smooth_score < 30 else 'strong_buy'
                    signal_age = 0
            elif direction < 0 and smooth_score <= -ENTRY_THRESHOLD:
                if current_signal not in ('sell', 'strong_sell'):
                    current_signal = 'sell' if smooth_score > -30 else 'strong_sell'
                    signal_age = 0
        else:
            whipsaw_blocked = True

        # Hysteresis: only exit on stronger opposing signal
        if current_signal in ('buy', 'strong_buy') and smooth_score < EXIT_THRESHOLD:
            if consecutive_direction >= persistence_days:
                current_signal = 'neutral'
                signal_age = 0
        elif current_signal in ('sell', 'strong_sell') and smooth_score > -EXIT_THRESHOLD:
            if consecutive_direction >= persistence_days:
                current_signal = 'neutral'
                signal_age = 0

        signal_age += 1

        smoothed_snap = dict(snap)
        smoothed_snap.update({
            'raw_score': round(raw_score, 1),
            'smoothed_score': round(smooth_score, 1),
            'persistent_signal': current_signal,
            'signal_age': signal_age,
            'consecutive_direction_days': consecutive_direction,
            'whipsaw_blocked': whipsaw_blocked,
        })
        smoothed_series.append((idx, date, smoothed_snap))

    return smoothed_series


def _ema_smooth(values, period):
    """Apply EMA smoothing to a list of values."""
    if not values or period <= 1:
        return list(values)
    result = [values[0]]
    k = 2.0 / (period + 1)
    for i in range(1, len(values)):
        result.append(values[i] * k + result[-1] * (1 - k))
    return result
