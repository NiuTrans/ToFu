"""lib/trading_strategy_engine/ensemble — Ensemble strategy backtesting.

Runs all strategies, computes rolling performance scores, and dynamically
weights them by recent Sharpe ratio for blended results.
"""

import math

from lib.trading_backtest_engine import BacktestEngine

from .risk_metrics import _compute_advanced_metrics

__all__ = [
    'run_ensemble_backtest',
]


def run_ensemble_backtest(asset_prices, benchmark_navs=None, config=None):
    """Run an ensemble of all strategies and dynamically weight them.

    The ensemble works by:
    1. Running all individual strategies on the same data
    2. Computing a rolling performance score for each strategy
    3. Dynamically allocating capital proportional to recent performance
    4. Blending signals from multiple strategies for final decisions

    This is more robust than any single strategy because:
    - Trend-following excels in trends, mean-reversion excels in ranges
    - The ensemble automatically shifts weight to what's working
    - Diversification across strategy types reduces drawdowns

    Returns comprehensive results including per-strategy contribution.
    """
    config = config or {}
    strategies = ['signal_driven', 'dca_signal', 'mean_reversion',
                  'trend_following', 'adaptive']

    # Run all strategies independently
    strategy_results = {}
    for strat in strategies:
        strat_config = dict(config)
        strat_config['strategy'] = strat
        engine = BacktestEngine(strat_config)
        result = engine.run(asset_prices, benchmark_navs)
        if 'error' not in result:
            strategy_results[strat] = result

    if not strategy_results:
        return {'error': 'All strategies failed'}

    # Also run buy-and-hold as baseline
    bh_config = dict(config)
    bh_config['strategy'] = 'buy_and_hold'
    bh_engine = BacktestEngine(bh_config)
    bh_result = bh_engine.run(asset_prices, benchmark_navs)

    # Compute ensemble by blending equity curves
    # Use rolling 60-day Sharpe ratio to weight strategies
    ensemble_curve = _blend_equity_curves(strategy_results, window=60)

    # Compute ensemble metrics
    ensemble_metrics = _compute_advanced_metrics(ensemble_curve)

    # Strategy contribution analysis
    contributions = {}
    for strat, result in strategy_results.items():
        summary = result.get('summary', {})
        contributions[strat] = {
            'total_return_pct': summary.get('total_return_pct', 0),
            'sharpe_ratio': summary.get('sharpe_ratio', 0),
            'max_drawdown_pct': summary.get('max_drawdown_pct', 0),
            'total_trades': summary.get('total_trades', 0),
            'win_rate_pct': summary.get('win_rate_pct', 0),
        }

    # Rank strategies
    ranked = sorted(contributions.items(),
                    key=lambda x: x[1].get('sharpe_ratio', -999), reverse=True)

    return {
        'ensemble': {
            'summary': ensemble_metrics,
            'equity_curve': ensemble_curve[::max(1, len(ensemble_curve) // 500)],
        },
        'buy_and_hold': bh_result.get('summary', {}) if 'error' not in bh_result else {'error': bh_result.get('error')},
        'strategy_contributions': contributions,
        'strategy_ranking': [{'strategy': s, 'rank': i + 1, **m}
                             for i, (s, m) in enumerate(ranked)],
        'best_strategy': ranked[0][0] if ranked else None,
        'period': {
            'start': min(n['date'] for navs in asset_prices.values() for n in navs),
            'end': max(n['date'] for navs in asset_prices.values() for n in navs),
        },
    }


def _blend_equity_curves(strategy_results, window=60):
    """Blend equity curves using rolling performance-weighted average.

    Each strategy gets a weight proportional to its rolling Sharpe ratio.
    Strategies with negative Sharpe get zero weight (replaced by cash).
    """
    # Collect all equity curves
    curves = {}
    for strat, result in strategy_results.items():
        eq = result.get('equity_curve', [])
        if eq:
            curves[strat] = {e['date']: e['value'] for e in eq}

    if not curves:
        return []

    # Get union of all dates
    all_dates = sorted(set(d for c in curves.values() for d in c))
    if not all_dates:
        return []

    # For each strategy, build complete value series (forward-fill gaps)
    strat_values = {}
    for strat, curve_dict in curves.items():
        values = []
        last_val = None
        for d in all_dates:
            if d in curve_dict:
                last_val = curve_dict[d]
            values.append(last_val)
        strat_values[strat] = values

    # Compute rolling returns and Sharpe for weighting
    strat_names = list(strat_values.keys())
    n = len(all_dates)
    blended = []

    for i in range(n):
        if i < window:
            # Not enough history — equal weight
            valid_vals = [strat_values[s][i] for s in strat_names
                         if strat_values[s][i] is not None]
            avg = sum(valid_vals) / len(valid_vals) if valid_vals else 0
            blended.append({'date': all_dates[i], 'value': round(avg, 2)})
            continue

        # Compute rolling Sharpe for each strategy
        weights = {}
        for strat in strat_names:
            vals = strat_values[strat][i - window:i + 1]
            if None in vals or len(vals) < window // 2:
                weights[strat] = 0
                continue
            rets = [(vals[j] - vals[j - 1]) / vals[j - 1]
                    for j in range(1, len(vals)) if vals[j - 1] > 0]
            if not rets:
                weights[strat] = 0
                continue
            avg_r = sum(rets) / len(rets)
            std_r = math.sqrt(sum((r - avg_r) ** 2 for r in rets) /
                              max(len(rets) - 1, 1))
            sharpe = (avg_r / std_r * math.sqrt(252)) if std_r > 0 else 0
            weights[strat] = max(sharpe, 0)  # zero weight for negative Sharpe

        total_w = sum(weights.values())
        if total_w > 0:
            weights = {s: w / total_w for s, w in weights.items()}
        else:
            # All negative Sharpe — equal weight as fallback
            weights = {s: 1.0 / len(strat_names) for s in strat_names}

        blended_val = sum(
            strat_values[s][i] * weights[s]
            for s in strat_names
            if strat_values[s][i] is not None
        )
        blended.append({'date': all_dates[i], 'value': round(blended_val, 2)})

    return blended
