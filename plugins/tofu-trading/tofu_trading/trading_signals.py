"""lib/asset_signals.py — Quantitative Signal Engine v1

Computes real technical indicators from price history and produces
machine-readable signals that drive the decision engine.

NO LLM dependency — pure math. Every signal is deterministic and reproducible.

Indicators:
  - Moving Averages (SMA, EMA) + crossover detection
  - RSI (Relative Strength Index)
  - MACD (Moving Average Convergence Divergence)
  - Bollinger Bands
  - ATR (Average True Range proxy)
  - Volatility (rolling annualized)
  - Max Drawdown (rolling)
  - Momentum (rate of change)
  - Market Regime detection (trend + volatility regime)
  - Composite Signal Score

All functions accept a list of {'date': str, 'nav': float} sorted by date ASC.
"""

import math

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Core Indicator Functions
# ═══════════════════════════════════════════════════════════

def sma(navs, period):
    """Simple Moving Average. Returns list same length as navs (None-padded)."""
    result = [None] * len(navs)
    for i in range(period - 1, len(navs)):
        window = [navs[j]['nav'] for j in range(i - period + 1, i + 1)]
        result[i] = sum(window) / period
    return result


def ema(navs, period):
    """Exponential Moving Average. Returns list same length as navs."""
    result = [None] * len(navs)
    if len(navs) < period:
        return result
    # Seed with SMA
    seed = sum(navs[i]['nav'] for i in range(period)) / period
    result[period - 1] = seed
    k = 2.0 / (period + 1)
    for i in range(period, len(navs)):
        result[i] = navs[i]['nav'] * k + result[i - 1] * (1 - k)
    return result


def rsi(navs, period=14):
    """Relative Strength Index. Returns list same length as navs."""
    result = [None] * len(navs)
    if len(navs) < period + 1:
        return result
    gains = []
    losses = []
    for i in range(1, len(navs)):
        delta = navs[i]['nav'] - navs[i - 1]['nav']
        gains.append(max(0, delta))
        losses.append(max(0, -delta))

    # Initial average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        result[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        result[period] = 100 - 100 / (1 + rs)

    # Smoothed
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result[i + 1] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[i + 1] = 100 - 100 / (1 + rs)

    return result


def macd(navs, fast=12, slow=26, signal_period=9):
    """MACD indicator. Returns (macd_line, signal_line, histogram) each same length."""
    n = len(navs)
    fast_ema = ema(navs, fast)
    slow_ema = ema(navs, slow)

    macd_line = [None] * n
    for i in range(n):
        if fast_ema[i] is not None and slow_ema[i] is not None:
            macd_line[i] = fast_ema[i] - slow_ema[i]

    # Signal line is EMA of MACD line
    signal_line = [None] * n
    # Find first valid MACD
    first_valid = None
    for i in range(n):
        if macd_line[i] is not None:
            first_valid = i
            break
    if first_valid is None:
        return macd_line, signal_line, [None] * n

    # Build pseudo-nav for EMA calculation of MACD line
    macd_vals = [{'nav': macd_line[i]} for i in range(first_valid, n) if macd_line[i] is not None]
    if len(macd_vals) >= signal_period:
        sig_raw = ema(macd_vals, signal_period)
        j = 0
        for i in range(first_valid, n):
            if macd_line[i] is not None and j < len(sig_raw):
                signal_line[i] = sig_raw[j]
                j += 1

    histogram = [None] * n
    for i in range(n):
        if macd_line[i] is not None and signal_line[i] is not None:
            histogram[i] = macd_line[i] - signal_line[i]

    return macd_line, signal_line, histogram


def bollinger_bands(navs, period=20, num_std=2.0):
    """Bollinger Bands. Returns (upper, middle, lower, bandwidth_pct)."""
    n = len(navs)
    upper = [None] * n
    middle = [None] * n
    lower = [None] * n
    bandwidth = [None] * n

    for i in range(period - 1, n):
        window = [navs[j]['nav'] for j in range(i - period + 1, i + 1)]
        avg = sum(window) / period
        std = math.sqrt(sum((x - avg) ** 2 for x in window) / period)
        middle[i] = avg
        upper[i] = avg + num_std * std
        lower[i] = avg - num_std * std
        bandwidth[i] = (upper[i] - lower[i]) / avg * 100 if avg > 0 else 0

    return upper, middle, lower, bandwidth


def rolling_volatility(navs, period=20, annualize=True):
    """Rolling annualized volatility. Returns list same length."""
    n = len(navs)
    result = [None] * n

    # First compute daily returns
    daily_ret = [None]
    for i in range(1, n):
        prev = navs[i - 1]['nav']
        curr = navs[i]['nav']
        if prev > 0:
            daily_ret.append((curr - prev) / prev)
        else:
            daily_ret.append(None)

    for i in range(period, n):
        window = [daily_ret[j] for j in range(i - period + 1, i + 1) if daily_ret[j] is not None]
        if len(window) < period // 2:
            continue
        avg = sum(window) / len(window)
        var = sum((r - avg) ** 2 for r in window) / max(len(window) - 1, 1)
        vol = math.sqrt(var)
        if annualize:
            vol *= math.sqrt(252)
        result[i] = vol

    return result


def momentum(navs, period=20):
    """Rate of Change (momentum). Returns percentage change over period."""
    n = len(navs)
    result = [None] * n
    for i in range(period, n):
        prev = navs[i - period]['nav']
        curr = navs[i]['nav']
        if prev > 0:
            result[i] = (curr - prev) / prev * 100
        else:
            result[i] = None
    return result


def rolling_max_drawdown(navs, period=60):
    """Rolling max drawdown over a window. Returns list of negative percentages."""
    n = len(navs)
    result = [None] * n
    for i in range(period - 1, n):
        window = [navs[j]['nav'] for j in range(i - period + 1, i + 1)]
        peak = window[0]
        max_dd = 0
        for v in window:
            if v > peak:
                peak = v
            dd = (v - peak) / peak if peak > 0 else 0
            if dd < max_dd:
                max_dd = dd
        result[i] = max_dd * 100  # as percentage
    return result


def daily_returns(navs):
    """Compute daily return series. Returns list of floats (first is 0)."""
    result = [0.0]
    for i in range(1, len(navs)):
        prev = navs[i - 1]['nav']
        curr = navs[i]['nav']
        if prev > 0:
            result.append((curr - prev) / prev)
        else:
            result.append(0.0)
    return result


# ═══════════════════════════════════════════════════════════
#  Crossover & Signal Detection
# ═══════════════════════════════════════════════════════════

def detect_ma_crossover(fast_ma, slow_ma):
    """Detect golden cross (fast > slow) and death cross (fast < slow).

    Returns list of {'index': int, 'type': 'golden'|'death', 'fast': float, 'slow': float}
    """
    signals = []
    prev_diff = None
    for i in range(len(fast_ma)):
        if fast_ma[i] is None or slow_ma[i] is None:
            continue
        diff = fast_ma[i] - slow_ma[i]
        if prev_diff is not None:
            if prev_diff <= 0 and diff > 0:
                signals.append({'index': i, 'type': 'golden', 'fast': fast_ma[i], 'slow': slow_ma[i]})
            elif prev_diff >= 0 and diff < 0:
                signals.append({'index': i, 'type': 'death', 'fast': fast_ma[i], 'slow': slow_ma[i]})
        prev_diff = diff
    return signals


def detect_macd_crossover(macd_line, signal_line):
    """Detect MACD crossovers (bullish when MACD crosses above signal, bearish below)."""
    signals = []
    prev_diff = None
    for i in range(len(macd_line)):
        if macd_line[i] is None or signal_line[i] is None:
            continue
        diff = macd_line[i] - signal_line[i]
        if prev_diff is not None:
            if prev_diff <= 0 and diff > 0:
                signals.append({'index': i, 'type': 'bullish', 'macd': macd_line[i], 'signal': signal_line[i]})
            elif prev_diff >= 0 and diff < 0:
                signals.append({'index': i, 'type': 'bearish', 'macd': macd_line[i], 'signal': signal_line[i]})
        prev_diff = diff
    return signals




# ═══════════════════════════════════════════════════════════
#  Market Regime Detection
# ═══════════════════════════════════════════════════════════

def detect_trend_regime(navs, short_period=20, long_period=60):
    """Classify market trend regime for each day.

    Returns list of regime strings:
      'strong_bull'  — short MA > long MA, both rising, momentum > 5%
      'bull'         — short MA > long MA
      'sideways'     — MAs close together (< 1% gap)
      'bear'         — short MA < long MA
      'strong_bear'  — short MA < long MA, both falling, momentum < -5%
    """
    n = len(navs)
    result = [None] * n
    short_ma = sma(navs, short_period)
    long_ma = sma(navs, long_period)
    mom = momentum(navs, short_period)

    for i in range(n):
        if short_ma[i] is None or long_ma[i] is None:
            continue

        gap_pct = (short_ma[i] - long_ma[i]) / long_ma[i] * 100 if long_ma[i] > 0 else 0
        mom_val = mom[i] if mom[i] is not None else 0

        # Check MA slope (rising/falling) over last 5 days
        short_rising = i >= 5 and short_ma[i - 5] is not None and short_ma[i] > short_ma[i - 5]
        long_rising = i >= 5 and long_ma[i - 5] is not None and long_ma[i] > long_ma[i - 5]

        if gap_pct > 1 and mom_val > 5 and short_rising and long_rising:
            result[i] = 'strong_bull'
        elif gap_pct > 0.5:
            result[i] = 'bull'
        elif gap_pct < -1 and mom_val < -5 and not short_rising and not long_rising:
            result[i] = 'strong_bear'
        elif gap_pct < -0.5:
            result[i] = 'bear'
        else:
            result[i] = 'sideways'

    return result


def detect_volatility_regime(navs, period=20):
    """Classify volatility regime.

    Returns list of regime strings:
      'low_vol'    — annualized vol < 15%
      'normal_vol' — 15% <= vol < 25%
      'high_vol'   — 25% <= vol < 40%
      'extreme_vol' — vol >= 40%
    """
    vol = rolling_volatility(navs, period)
    result = [None] * len(navs)
    for i, v in enumerate(vol):
        if v is None:
            continue
        v_pct = v * 100  # convert to percentage
        if v_pct < 15:
            result[i] = 'low_vol'
        elif v_pct < 25:
            result[i] = 'normal_vol'
        elif v_pct < 40:
            result[i] = 'high_vol'
        else:
            result[i] = 'extreme_vol'
    return result


# ═══════════════════════════════════════════════════════════
#  Composite Signal Score
# ═══════════════════════════════════════════════════════════

def compute_signal_snapshot(navs):
    """Compute ALL indicators for the LATEST point in the nav series.

    Returns a comprehensive snapshot dict with all indicator values
    and a composite signal score from -100 (strong sell) to +100 (strong buy).

    This is the primary entry point for the decision engine.
    """
    if not navs or len(navs) < 60:
        return {'error': 'Insufficient data (need >= 60 days)', 'data_points': len(navs) if navs else 0}

    n = len(navs)
    last = n - 1
    current_nav = navs[last]['nav']
    current_date = navs[last]['date']

    # ── Compute all indicators ──
    ma5 = sma(navs, 5)
    ma10 = sma(navs, 10)
    ma20 = sma(navs, 20)
    ma60 = sma(navs, 60)
    # ema12/ema26 computed implicitly by macd() below
    rsi_14 = rsi(navs, 14)
    macd_l, macd_s, macd_h = macd(navs)
    bb_up, bb_mid, bb_low, bb_width = bollinger_bands(navs)
    vol_20 = rolling_volatility(navs, 20)
    mom_20 = momentum(navs, 20)
    mom_5 = momentum(navs, 5)
    dd_60 = rolling_max_drawdown(navs, 60)
    trend = detect_trend_regime(navs)
    vol_regime = detect_volatility_regime(navs)

    # ── Build snapshot ──
    snapshot = {
        'date': current_date,
        'nav': current_nav,
        'data_points': n,

        # Moving Averages
        'ma5': _r(ma5[last]),
        'ma10': _r(ma10[last]),
        'ma20': _r(ma20[last]),
        'ma60': _r(ma60[last]),
        'ma_alignment': _ma_alignment(ma5[last], ma10[last], ma20[last], ma60[last]),

        # Position relative to MAs
        'above_ma5': current_nav > ma5[last] if ma5[last] else None,
        'above_ma20': current_nav > ma20[last] if ma20[last] else None,
        'above_ma60': current_nav > ma60[last] if ma60[last] else None,
        'distance_from_ma20_pct': _r((current_nav - ma20[last]) / ma20[last] * 100) if ma20[last] else None,
        'distance_from_ma60_pct': _r((current_nav - ma60[last]) / ma60[last] * 100) if ma60[last] else None,

        # RSI
        'rsi_14': _r(rsi_14[last]),
        'rsi_zone': _rsi_zone(rsi_14[last]),

        # MACD
        'macd_line': _r(macd_l[last], 6),
        'macd_signal': _r(macd_s[last], 6),
        'macd_histogram': _r(macd_h[last], 6),
        'macd_bullish': macd_h[last] is not None and macd_h[last] > 0,

        # Bollinger Bands
        'bb_upper': _r(bb_up[last]),
        'bb_middle': _r(bb_mid[last]),
        'bb_lower': _r(bb_low[last]),
        'bb_width_pct': _r(bb_width[last]),
        'bb_position': _bb_position(current_nav, bb_up[last], bb_mid[last], bb_low[last]),

        # Volatility
        'volatility_20d': _r(vol_20[last]),
        'volatility_regime': vol_regime[last],

        # Momentum
        'momentum_5d': _r(mom_5[last]),
        'momentum_20d': _r(mom_20[last]),

        # Drawdown
        'rolling_max_drawdown_60d': _r(dd_60[last]),

        # Regime
        'trend_regime': trend[last],
    }

    # ── Composite Signal Score ──
    score, breakdown = _compute_composite_score(snapshot)
    snapshot['composite_score'] = score
    snapshot['score_breakdown'] = breakdown
    snapshot['signal'] = (
        'strong_buy' if score >= 60 else
        'buy' if score >= 25 else
        'weak_buy' if score >= 10 else
        'neutral' if score >= -10 else
        'weak_sell' if score >= -25 else
        'sell' if score >= -60 else
        'strong_sell'
    )

    # ── Recent crossover signals (last 10 days) ──
    recent_ma_cross = detect_ma_crossover(ma10[-20:], ma20[-20:]) if n >= 20 else []
    recent_macd_cross = detect_macd_crossover(macd_l[-20:], macd_s[-20:]) if n >= 26 else []
    snapshot['recent_ma_crossovers'] = [
        {'type': s['type'], 'days_ago': 20 - s['index']} for s in recent_ma_cross if s['index'] >= 10
    ]
    snapshot['recent_macd_crossovers'] = [
        {'type': s['type'], 'days_ago': 20 - s['index']} for s in recent_macd_cross if s['index'] >= 10
    ]

    return snapshot


def _r(v, decimals=4):
    """Round helper that handles None."""
    return round(v, decimals) if v is not None else None


def _ma_alignment(ma5, ma10, ma20, ma60):
    """Check MA alignment (bullish = 5 > 10 > 20 > 60)."""
    if any(v is None for v in [ma5, ma10, ma20, ma60]):
        return 'unknown'
    if ma5 > ma10 > ma20 > ma60:
        return 'bullish_aligned'
    if ma5 < ma10 < ma20 < ma60:
        return 'bearish_aligned'
    if ma5 > ma20 and ma10 > ma60:
        return 'bullish_mixed'
    if ma5 < ma20 and ma10 < ma60:
        return 'bearish_mixed'
    return 'mixed'


def _rsi_zone(val):
    if val is None:
        return 'unknown'
    if val < 20:
        return 'extreme_oversold'
    if val < 30:
        return 'oversold'
    if val < 45:
        return 'weak'
    if val < 55:
        return 'neutral'
    if val < 70:
        return 'strong'
    if val < 80:
        return 'overbought'
    return 'extreme_overbought'


def _bb_position(nav, upper, mid, lower):
    """Position within Bollinger Bands as percentage (0 = lower, 100 = upper)."""
    if any(v is None for v in [upper, mid, lower]) or upper == lower:
        return None
    return round((nav - lower) / (upper - lower) * 100, 1)


def _compute_composite_score(snapshot):
    """Compute weighted composite score from -100 to +100.

    Weights:
      Trend (MA alignment + regime)  : 25%
      Momentum (RSI + MACD)          : 25%
      Mean Reversion (BB position)   : 15%
      Volatility adjustment          : 15%
      Drawdown penalty               : 10%
      Short-term momentum            : 10%
    """
    breakdown = {}
    total_weight = 0
    weighted_sum = 0

    # 1. Trend Score (25%)
    trend_score = 0
    ma_align = snapshot.get('ma_alignment', 'unknown')
    if ma_align == 'bullish_aligned':
        trend_score = 80
    elif ma_align == 'bullish_mixed':
        trend_score = 40
    elif ma_align == 'mixed':
        trend_score = 0
    elif ma_align == 'bearish_mixed':
        trend_score = -40
    elif ma_align == 'bearish_aligned':
        trend_score = -80

    regime = snapshot.get('trend_regime')
    if regime == 'strong_bull':
        trend_score = max(trend_score, 60)
        trend_score = (trend_score + 80) / 2
    elif regime == 'strong_bear':
        trend_score = min(trend_score, -60)
        trend_score = (trend_score - 80) / 2

    breakdown['trend'] = {'score': round(trend_score, 1), 'weight': 0.25}
    weighted_sum += trend_score * 0.25
    total_weight += 0.25

    # 2. Momentum Score (25%)
    rsi_val = snapshot.get('rsi_14')
    macd_bull = snapshot.get('macd_bullish')
    mom_score = 0
    if rsi_val is not None:
        # RSI contribution: oversold = bullish, overbought = bearish
        if rsi_val < 30:
            mom_score += 40  # Oversold → likely to bounce (buy signal)
        elif rsi_val < 45:
            mom_score += 15
        elif rsi_val > 70:
            mom_score -= 40  # Overbought → likely to pull back
        elif rsi_val > 55:
            mom_score -= 10
        # Note: RSI 30-45 is "recovering" (bullish), 55-70 is "getting hot"

    if macd_bull is not None:
        mom_score += 30 if macd_bull else -30

    mom_score = max(-100, min(100, mom_score))
    breakdown['momentum'] = {'score': round(mom_score, 1), 'weight': 0.25, 'rsi': rsi_val, 'macd_bullish': macd_bull}
    weighted_sum += mom_score * 0.25
    total_weight += 0.25

    # 3. Mean Reversion (BB position) (15%)
    bb_pos = snapshot.get('bb_position')
    mr_score = 0
    if bb_pos is not None:
        # Near lower band = buy signal, near upper = sell signal
        if bb_pos < 10:
            mr_score = 60  # Strong mean-reversion buy
        elif bb_pos < 25:
            mr_score = 30
        elif bb_pos > 90:
            mr_score = -60  # Strong mean-reversion sell
        elif bb_pos > 75:
            mr_score = -30
        else:
            mr_score = 0  # Neutral zone

    breakdown['mean_reversion'] = {'score': round(mr_score, 1), 'weight': 0.15, 'bb_position': bb_pos}
    weighted_sum += mr_score * 0.15
    total_weight += 0.15

    # 4. Volatility Adjustment (15%)
    vol_regime = snapshot.get('volatility_regime')
    vol_score = 0
    if vol_regime == 'low_vol':
        vol_score = 20  # Low vol is generally positive for holding
    elif vol_regime == 'normal_vol':
        vol_score = 0
    elif vol_regime == 'high_vol':
        vol_score = -30  # High vol → reduce exposure
    elif vol_regime == 'extreme_vol':
        vol_score = -60  # Extreme → strong caution

    breakdown['volatility'] = {'score': round(vol_score, 1), 'weight': 0.15, 'regime': vol_regime}
    weighted_sum += vol_score * 0.15
    total_weight += 0.15

    # 5. Drawdown Penalty (10%)
    dd = snapshot.get('rolling_max_drawdown_60d')
    dd_score = 0
    if dd is not None:
        if dd > -5:
            dd_score = 10  # Minimal drawdown → fine
        elif dd > -10:
            dd_score = 0
        elif dd > -20:
            dd_score = -30
        else:
            dd_score = -60  # Deep drawdown → danger

    breakdown['drawdown'] = {'score': round(dd_score, 1), 'weight': 0.10, 'max_dd_60d': dd}
    weighted_sum += dd_score * 0.10
    total_weight += 0.10

    # 6. Short-term Momentum (10%)
    mom5 = snapshot.get('momentum_5d')
    stm_score = 0
    if mom5 is not None:
        if mom5 > 3:
            stm_score = 40
        elif mom5 > 1:
            stm_score = 20
        elif mom5 < -3:
            stm_score = -40
        elif mom5 < -1:
            stm_score = -20

    breakdown['short_momentum'] = {'score': round(stm_score, 1), 'weight': 0.10, 'mom_5d': mom5}
    weighted_sum += stm_score * 0.10
    total_weight += 0.10

    final_score = weighted_sum / total_weight if total_weight > 0 else 0
    return round(final_score, 1), breakdown

#  Signal History for Backtesting
# ═══════════════════════════════════════════════════════════

def compute_signal_series(navs, compute_every=1):
    """Compute signal snapshots for every Nth day in the nav series.

    This is used by the backtest engine. For each day, we only use data
    up to that day (NO future data leakage).

    Args:
        navs: full price history sorted by date ASC
        compute_every: compute signal every N trading days (default 1 = daily)

    Returns:
        list of (index, date, signal_snapshot) for each computed day
    """
    MIN_HISTORY = 60  # Need at least 60 days before computing signals
    series = []

    for i in range(MIN_HISTORY, len(navs), compute_every):
        # Only use data UP TO AND INCLUDING day i — strict no-future-leakage
        historical_slice = navs[:i + 1]
        snap = compute_signal_snapshot(historical_slice)
        if 'error' not in snap:
            series.append((i, navs[i]['date'], snap))

    return series
