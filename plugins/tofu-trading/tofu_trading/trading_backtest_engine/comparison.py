"""lib/trading_backtest_engine/comparison.py — Strategy Comparison

Run multiple strategies on identical data and rank by risk-adjusted performance.
"""

from .config import ALL_STRATEGIES, STRATEGY_NAMES
from .engine import BacktestEngine

__all__ = [
    "compare_strategies",
]


def compare_strategies(asset_prices, benchmark_navs=None, config=None, strategies=None):
    """Run multiple strategies on the same data and compare.

    Returns ranked comparison with all metrics.
    """
    config = config or {}

    if strategies is None:
        strategies = list(ALL_STRATEGIES)

    results = []
    for strat in strategies:
        strat_config = dict(config)
        strat_config['strategy'] = strat

        engine = BacktestEngine(strat_config)
        result = engine.run(asset_prices, benchmark_navs)

        entry = {
            'name': _strategy_display_name(strat),
            'strategy': strat,
        }

        if 'error' not in result:
            entry['metrics'] = result.get('summary', {})
            entry['benchmark'] = result.get('benchmark', {})
            entry['equity_curve'] = result.get('equity_curve', [])
            entry['trade_log_count'] = len(result.get('trade_log', []))
        else:
            entry['metrics'] = {'error': result['error']}

        results.append(entry)

    # Rank by total return (primary) and Sharpe (secondary)
    valid = [r for r in results if 'error' not in r.get('metrics', {})]
    valid.sort(key=lambda x: (
        x['metrics'].get('sharpe_ratio', -999),  # Primary: risk-adjusted
        x['metrics'].get('total_return_pct', -999),  # Secondary: absolute return
    ), reverse=True)

    # Add rank
    for i, r in enumerate(valid):
        r['rank'] = i + 1

    return {
        'results': results,
        'best_strategy': valid[0]['name'] if valid else None,
        'data_period': {
            'start': min(n['date'] for navs in asset_prices.values() for n in navs) if asset_prices else None,
            'end': max(n['date'] for navs in asset_prices.values() for n in navs) if asset_prices else None,
        },
    }


def _strategy_display_name(strat):
    return STRATEGY_NAMES.get(strat, strat)
