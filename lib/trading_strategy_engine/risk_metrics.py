"""lib/trading_strategy_engine/risk_metrics — Advanced risk-adjusted performance metrics.

Computes professional-grade metrics: Sharpe, Sortino, Calmar, Information Ratio,
Omega Ratio, Tail Ratio, VaR, CVaR, Ulcer Index, and more.
"""

import math

__all__ = [
    'compute_advanced_metrics',
]


def compute_advanced_metrics(equity_curve):
    """Compute advanced risk-adjusted performance metrics.

    Goes beyond basic Sharpe to give a comprehensive risk picture.

    Args:
        equity_curve: list of {'date': str, 'value': float}

    Returns all metrics a professional trader would want.
    """
    return _compute_advanced_metrics(equity_curve)


def _compute_advanced_metrics(equity_curve):
    """Internal implementation of advanced metrics."""
    if not equity_curve or len(equity_curve) < 10:
        return {'error': 'Insufficient data for metrics'}

    values = [e['value'] for e in equity_curve]
    dates = [e['date'] for e in equity_curve]
    n = len(values)

    initial = values[0]
    final = values[-1]

    # Daily returns
    rets = []
    for i in range(1, n):
        if values[i - 1] > 0:
            rets.append((values[i] - values[i - 1]) / values[i - 1])
        else:
            rets.append(0)

    if not rets:
        return {'error': 'No returns computed'}

    # Basic stats
    total_return = (final - initial) / initial * 100 if initial > 0 else 0
    days = n
    years = days / 252.0
    ann_return = (math.pow(final / initial, 1 / years) - 1) * 100 if years > 0 and final > 0 and initial > 0 else 0

    mean_ret = sum(rets) / len(rets)
    variance = sum((r - mean_ret) ** 2 for r in rets) / max(len(rets) - 1, 1)
    daily_vol = math.sqrt(variance)
    ann_vol = daily_vol * math.sqrt(252) * 100

    # Risk-free rate (China ~2.5%)
    rf_daily = 0.025 / 252

    # ── Sharpe Ratio ──
    sharpe = ((mean_ret - rf_daily) / daily_vol * math.sqrt(252)) if daily_vol > 0 else 0

    # ── Sortino Ratio (downside deviation only) ──
    downside_rets = [r for r in rets if r < rf_daily]
    if downside_rets:
        downside_var = sum((r - rf_daily) ** 2 for r in downside_rets) / len(downside_rets)
        downside_dev = math.sqrt(downside_var)
        sortino = ((mean_ret - rf_daily) / downside_dev * math.sqrt(252)) if downside_dev > 0 else 0
    else:
        sortino = sharpe * 1.5  # no downside days — very good

    # ── Max Drawdown ──
    peak = values[0]
    max_dd = 0
    max_dd_start = max_dd_end = dates[0]
    dd_start = dates[0]
    current_dd_days = 0
    max_dd_days = 0

    for i in range(1, n):
        if values[i] > peak:
            peak = values[i]
            dd_start = dates[i]
            current_dd_days = 0
        else:
            current_dd_days += 1

        dd = (peak - values[i]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_start = dd_start
            max_dd_end = dates[i]
            max_dd_days = current_dd_days

    # ── Calmar Ratio ──
    calmar = ann_return / (max_dd * 100) if max_dd > 0 else 0

    # ── Information Ratio (vs risk-free benchmark) ──
    # Would be vs market benchmark ideally, but using excess returns
    excess_rets = [r - rf_daily for r in rets]
    if excess_rets:
        tracking_error = math.sqrt(sum((r - mean_ret + rf_daily) ** 2 for r in excess_rets) /
                                    max(len(excess_rets) - 1, 1))
        info_ratio = (sum(excess_rets) / len(excess_rets) / tracking_error *
                      math.sqrt(252)) if tracking_error > 0 else 0
    else:
        info_ratio = 0

    # ── Omega Ratio (probability weighted gain/loss above threshold) ──
    threshold = rf_daily
    gains = sum(max(0, r - threshold) for r in rets)
    losses = sum(max(0, threshold - r) for r in rets)
    omega = (gains / losses) if losses > 0 else float('inf')

    # ── Tail Ratio (right tail / left tail at 95th percentile) ──
    sorted_rets = sorted(rets)
    p95_idx = max(0, int(len(sorted_rets) * 0.95) - 1)
    p5_idx = max(0, int(len(sorted_rets) * 0.05))
    right_tail = abs(sorted_rets[p95_idx]) if sorted_rets[p95_idx] > 0 else 0.0001
    left_tail = abs(sorted_rets[p5_idx]) if sorted_rets[p5_idx] < 0 else 0.0001
    tail_ratio = right_tail / left_tail

    # ── Value at Risk (VaR) 95% ──
    var_95 = sorted_rets[p5_idx] * 100

    # ── Conditional VaR (CVaR / Expected Shortfall) 95% ──
    cvar_returns = sorted_rets[:p5_idx + 1]
    cvar_95 = (sum(cvar_returns) / len(cvar_returns) * 100) if cvar_returns else var_95

    # ── Win Rate and Profit Factor ──
    winning_days = sum(1 for r in rets if r > 0)
    losing_days = sum(1 for r in rets if r < 0)
    win_rate = winning_days / len(rets) * 100 if rets else 0

    avg_win = sum(r for r in rets if r > 0) / max(winning_days, 1)
    avg_loss = abs(sum(r for r in rets if r < 0)) / max(losing_days, 1)
    profit_factor = avg_win / avg_loss if avg_loss > 0 else float('inf')

    # ── Skewness and Kurtosis ──
    skewness = (sum((r - mean_ret) ** 3 for r in rets) /
                (len(rets) * daily_vol ** 3)) if daily_vol > 0 else 0
    kurtosis = (sum((r - mean_ret) ** 4 for r in rets) /
                (len(rets) * daily_vol ** 4) - 3) if daily_vol > 0 else 0

    # ── Recovery Factor ──
    recovery_factor = total_return / (max_dd * 100) if max_dd > 0 else 0

    # ── Ulcer Index (RMS of drawdown) ──
    drawdowns = []
    peak_val = values[0]
    for v in values:
        if v > peak_val:
            peak_val = v
        dd_pct = (peak_val - v) / peak_val * 100 if peak_val > 0 else 0
        drawdowns.append(dd_pct)
    ulcer = math.sqrt(sum(d ** 2 for d in drawdowns) / len(drawdowns)) if drawdowns else 0

    # ── Longest Drawdown Period ──
    longest_dd = 0
    current_dd_length = 0
    peak_val = values[0]
    for v in values:
        if v >= peak_val:
            peak_val = v
            current_dd_length = 0
        else:
            current_dd_length += 1
            longest_dd = max(longest_dd, current_dd_length)

    return {
        # Core Returns
        'initial_capital': round(initial, 2),
        'final_value': round(final, 2),
        'total_return_pct': round(total_return, 2),
        'annualized_return_pct': round(ann_return, 2),

        # Volatility
        'annualized_volatility_pct': round(ann_vol, 2),
        'daily_volatility_pct': round(daily_vol * 100, 4),

        # Risk-Adjusted Returns
        'sharpe_ratio': round(sharpe, 3),
        'sortino_ratio': round(sortino, 3),
        'calmar_ratio': round(calmar, 3),
        'information_ratio': round(info_ratio, 3),
        'omega_ratio': round(min(omega, 99.99), 3),
        'tail_ratio': round(tail_ratio, 3),

        # Drawdown
        'max_drawdown_pct': round(max_dd * 100, 2),
        'max_drawdown_period': {'start': max_dd_start, 'end': max_dd_end},
        'max_drawdown_days': max_dd_days,
        'longest_drawdown_days': longest_dd,
        'ulcer_index': round(ulcer, 3),
        'recovery_factor': round(recovery_factor, 3),

        # Tail Risk
        'var_95_pct': round(var_95, 3),
        'cvar_95_pct': round(cvar_95, 3),
        'skewness': round(skewness, 3),
        'kurtosis_excess': round(kurtosis, 3),

        # Win/Loss
        'win_rate_pct': round(win_rate, 1),
        'profit_factor': round(min(profit_factor, 99.99), 3),
        'avg_win_pct': round(avg_win * 100, 4),
        'avg_loss_pct': round(avg_loss * 100, 4),
        'winning_days': winning_days,
        'losing_days': losing_days,
        'trading_days': len(rets),
    }
