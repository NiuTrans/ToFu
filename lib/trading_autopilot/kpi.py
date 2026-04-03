"""lib/trading_autopilot/kpi.py — Pre-Backtest KPI Evaluator.

Calculates comprehensive KPIs (return, sharpe, drawdown, sortino, etc.)
from price time series and scores assets for recommendation.
"""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any, Optional

from lib.log import get_logger
from lib.protocols import TradingDataProvider
from lib.trading import fetch_asset_info, fetch_price_history
from lib.trading._common import TradingClient

logger = get_logger(__name__)

__all__ = [
    'calculate_kpis',
    'pre_backtest_evaluate',
]


def calculate_kpis(nav_series, initial_value=10000):
    """Calculate comprehensive KPIs from a price time series.

    Args:
        nav_series: list of {'date': str, 'nav': float} sorted by date asc

    Returns dict with:
      total_return, annual_return, max_drawdown, sharpe_ratio,
      volatility, calmar_ratio, win_days_pct, best_day, worst_day,
      sortino_ratio, var_95
    """
    if not nav_series or len(nav_series) < 2:
        return {
            'total_return': 0, 'annual_return': 0, 'max_drawdown': 0,
            'sharpe_ratio': 0, 'volatility': 0, 'calmar_ratio': 0,
            'win_days_pct': 0, 'best_day': 0, 'worst_day': 0,
            'sortino_ratio': 0, 'var_95': 0,
        }

    # Daily returns
    daily_returns = []
    for i in range(1, len(nav_series)):
        prev = nav_series[i - 1]['nav']
        curr = nav_series[i]['nav']
        if prev > 0:
            daily_returns.append((curr - prev) / prev)

    if not daily_returns:
        return {
            'total_return': 0, 'annual_return': 0, 'max_drawdown': 0,
            'sharpe_ratio': 0, 'volatility': 0, 'calmar_ratio': 0,
            'win_days_pct': 0, 'best_day': 0, 'worst_day': 0,
            'sortino_ratio': 0, 'var_95': 0,
        }

    # Total return
    first_nav = nav_series[0]['nav']
    last_nav = nav_series[-1]['nav']
    total_return = (last_nav - first_nav) / first_nav if first_nav > 0 else 0

    # Annualized return
    days = len(daily_returns)
    annual_factor = 252 / max(days, 1)
    annual_return = ((1 + total_return) ** annual_factor - 1) if days > 0 else 0

    # Volatility (annualized)
    avg_r = sum(daily_returns) / len(daily_returns)
    variance = sum((r - avg_r) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
    daily_vol = math.sqrt(variance)
    annual_vol = daily_vol * math.sqrt(252)

    # Max drawdown
    peak = nav_series[0]['nav']
    max_dd = 0
    for pt in nav_series:
        if pt['nav'] > peak:
            peak = pt['nav']
        dd = (peak - pt['nav']) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    # Sharpe ratio (risk-free rate ~2.5% for China)
    risk_free_daily = 0.025 / 252
    excess_returns = [r - risk_free_daily for r in daily_returns]
    avg_excess = sum(excess_returns) / len(excess_returns) if excess_returns else 0
    std_excess = math.sqrt(sum((r - avg_excess) ** 2 for r in excess_returns) / max(len(excess_returns) - 1, 1)) if len(excess_returns) > 1 else 1
    sharpe = (avg_excess / std_excess * math.sqrt(252)) if std_excess > 0 else 0

    # Sortino ratio (downside deviation)
    downside = [r for r in excess_returns if r < 0]
    downside_dev = math.sqrt(sum(r ** 2 for r in downside) / max(len(downside), 1)) if downside else 0.0001
    sortino = (avg_excess / downside_dev * math.sqrt(252)) if downside_dev > 0 else 0

    # Calmar ratio
    calmar = (annual_return / max_dd) if max_dd > 0 else 0

    # Win days
    win_days = sum(1 for r in daily_returns if r > 0)
    win_pct = (win_days / len(daily_returns) * 100) if daily_returns else 0

    # VaR 95%
    sorted_returns = sorted(daily_returns)
    var_idx = max(0, int(len(sorted_returns) * 0.05) - 1)
    var_95 = sorted_returns[var_idx] if sorted_returns else 0

    return {
        'total_return': round(total_return * 100, 2),
        'annual_return': round(annual_return * 100, 2),
        'max_drawdown': round(max_dd * 100, 2),
        'sharpe_ratio': round(sharpe, 2),
        'volatility': round(annual_vol * 100, 2),
        'calmar_ratio': round(calmar, 2),
        'win_days_pct': round(win_pct, 1),
        'best_day': round(max(daily_returns) * 100, 2) if daily_returns else 0,
        'worst_day': round(min(daily_returns) * 100, 2) if daily_returns else 0,
        'sortino_ratio': round(sortino, 2),
        'var_95': round(var_95 * 100, 2),
    }


def pre_backtest_evaluate(
    db: Any,
    symbols: list[str],
    lookback_days: int = 90,
    *,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
) -> dict[str, dict[str, Any]]:
    """Evaluate a set of assets' KPIs BEFORE running a full backtest.

    Uses actual price history + current strategy context to produce
    a confidence-scored evaluation.

    Args:
        db:             Database connection.
        symbols:     List of asset codes to evaluate.
        lookback_days:  Number of days of history to analyse.
        client:         Optional :class:`~lib.trading._common.TradingClient` instance
                        for dependency injection.  Passed through to concrete
                        ``fetch_price_history`` / ``fetch_asset_info`` when no
                        *trading_provider* is given.
        trading_provider:  Optional :class:`~lib.protocols.TradingDataProvider` for
                        dependency injection.  Defaults to the concrete
                        ``lib.trading`` module functions (production path).
                        Pass a mock/stub for testing.

    Returns:
      { symbol: { kpis, strategy_alignment_score, recommendation_score } }
    """
    # Resolve trading data functions — use injected provider or module defaults.
    # When using concrete functions (no trading_provider), pass client= through
    # so callers can inject a custom TradingClient for testing or isolation.
    if trading_provider is not None:
        _fetch_nav_history = trading_provider.fetch_price_history
        _fetch_info = trading_provider.fetch_asset_info
    else:
        _fetch_nav_history = (
            lambda code, start, end: fetch_price_history(
                code, start, end, client=client,
            )
        )
        _fetch_info = lambda code: fetch_asset_info(code, client=client)  # noqa: E731

    results = {}
    end_date = date.today()
    start_date = end_date - timedelta(days=lookback_days)

    for code in symbols:
        try:
            nav_data = _fetch_nav_history(
                code,
                start_date.strftime('%Y-%m-%d'),
                end_date.strftime('%Y-%m-%d')
            )
            if not nav_data:
                results[code] = {'error': 'No price data available'}
                continue

            kpis = calculate_kpis(nav_data)

            # Compute quantitative technical signals
            quant_signals = {}
            try:
                from lib.trading_signals import compute_signal_snapshot
                quant_signals = compute_signal_snapshot(nav_data)
            except Exception as sig_err:
                logger.warning('[Autopilot] Signal computation failed for %s: %s', code, sig_err, exc_info=True)

            # Score the asset (enhanced with quant signals)
            score = _compute_recommendation_score(kpis, quant_signals)

            info = _fetch_info(code)
            results[code] = {
                'asset_name': info.get('name', '') if info else '',
                'asset_type': info.get('type', '') if info else '',
                'kpis': kpis,
                'quant_signals': quant_signals,
                'recommendation_score': score,
                'data_points': len(nav_data),
                'period': f"{start_date} ~ {end_date}",
            }
        except Exception as e:
            logger.warning('[Autopilot] Fund analysis failed for %s: %s', code, e, exc_info=True)
            results[code] = {'error': str(e)}

    return results


def _compute_recommendation_score(kpis, quant_signals=None):
    """Compute a 0-100 recommendation score from KPIs + quantitative signals.

    Weighted factors (when quant_signals available):
      - Sharpe ratio: 15%
      - Total return: 12%
      - Max drawdown: 15% (inverted)
      - Sortino ratio: 10%
      - Win days %: 5%
      - Volatility: 8% (inverted)
      - Composite signal score: 20%  ← NEW from asset_signals
      - Regime alignment: 15%        ← NEW from asset_signals

    Without quant_signals, falls back to original KPI-only weights.
    """
    # Normalize each KPI to 0-100 scale
    sharpe_score = min(100, max(0, (kpis['sharpe_ratio'] + 1) * 30))
    return_score = min(100, max(0, kpis['total_return'] + 20))
    dd_score = max(0, 100 - kpis['max_drawdown'] * 3)
    sortino_score = min(100, max(0, (kpis['sortino_ratio'] + 1) * 25))
    win_score = kpis['win_days_pct']
    vol_score = max(0, 100 - kpis['volatility'] * 2)

    if quant_signals and 'error' not in quant_signals:
        # Enhanced scoring with quantitative signals
        # compute_signal_snapshot returns 'composite_score' at top level (not nested)
        composite_score_raw = quant_signals.get('composite_score', 50)  # -100 to 100
        composite_norm = min(100, max(0, (composite_score_raw + 100) / 2))  # → 0-100

        # compute_signal_snapshot returns 'trend_regime' at top level (not nested)
        regime_name = quant_signals.get('trend_regime') or 'unknown'
        # Reward trending regimes, penalize high-vol
        regime_scores = {
            'strong_uptrend': 90, 'uptrend': 75, 'recovery': 65,
            'ranging': 50, 'unknown': 50,
            'distribution': 35, 'downtrend': 20, 'capitulation': 10,
        }
        regime_norm = regime_scores.get(regime_name, 50)

        weighted = (
            sharpe_score * 0.15 +
            return_score * 0.12 +
            dd_score * 0.15 +
            sortino_score * 0.10 +
            win_score * 0.05 +
            vol_score * 0.08 +
            composite_norm * 0.20 +
            regime_norm * 0.15
        )
        return round(min(100, max(0, weighted)), 1)

    # Fallback: KPI-only weights

    weighted = (
        sharpe_score * 0.25 +
        return_score * 0.20 +
        dd_score * 0.20 +
        sortino_score * 0.15 +
        win_score * 0.10 +
        vol_score * 0.10
    )
    return round(min(100, max(0, weighted)), 1)
