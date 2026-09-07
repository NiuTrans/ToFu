"""lib/trading_backtest_engine/validation.py — Walk-Forward & Multi-Period Validation

Core validation tools for backtesting robustness:
  - walk_forward_backtest: rolling walk-forward analysis
  - multi_period_backtest: stress testing across market regimes
"""

import math

from .engine import BacktestEngine

__all__ = [
    "walk_forward_backtest",
    "multi_period_backtest",
]


# ═══════════════════════════════════════════════════════════
#  Walk-Forward Validation
# ═══════════════════════════════════════════════════════════

def walk_forward_backtest(asset_prices, benchmark_navs=None, config=None,
                           num_folds=4, train_ratio=0.7):
    """Walk-Forward analysis: train on period A, test on period B, roll forward.

    This prevents overfitting by ensuring the strategy works on unseen data.

    Splits the timeline into num_folds windows, each with train_ratio% training
    and (1-train_ratio)% testing. Each fold only uses data available at that time.

    Returns:
        {
            folds: [{train_period, test_period, train_metrics, test_metrics}, ...],
            aggregate: {avg_test_return, avg_test_sharpe, consistency_score, ...}
        }
    """
    config = config or {}

    # Get all dates across all assets
    all_dates = set()
    for code, navs in asset_prices.items():
        for n in navs:
            all_dates.add(n['date'])
    all_dates = sorted(all_dates)
    total_days = len(all_dates)

    if total_days < 120:
        return {'error': f'Insufficient data for walk-forward: {total_days} days (need 120+)'}

    # Calculate fold size (with overlap for continuity)
    fold_step = total_days // num_folds
    folds = []

    for i in range(num_folds):
        fold_start = i * fold_step
        fold_end = min(fold_start + fold_step + int(fold_step * 0.3), total_days)

        split_point = fold_start + int((fold_end - fold_start) * train_ratio)

        train_dates = all_dates[fold_start:split_point]
        test_dates = all_dates[split_point:fold_end]

        if len(train_dates) < 60 or len(test_dates) < 10:
            continue

        # Slice asset prices for train and test periods
        train_navs = {}
        test_navs = {}
        for code, navs in asset_prices.items():
            train_navs[code] = [n for n in navs if train_dates[0] <= n['date'] <= train_dates[-1]]
            test_navs[code] = [n for n in navs if test_dates[0] <= n['date'] <= test_dates[-1]]

        train_bm = None
        test_bm = None
        if benchmark_navs:
            train_bm = [n for n in benchmark_navs if train_dates[0] <= n['date'] <= train_dates[-1]]
            test_bm = [n for n in benchmark_navs if test_dates[0] <= n['date'] <= test_dates[-1]]

        # Run backtest on train period
        train_engine = BacktestEngine(config)
        train_result = train_engine.run(train_navs, train_bm)

        # Run on test period (STRICT: no re-optimisation)
        test_engine = BacktestEngine(config)
        test_result = test_engine.run(test_navs, test_bm)

        fold_data = {
            'fold': i + 1,
            'train_period': {'start': train_dates[0], 'end': train_dates[-1], 'days': len(train_dates)},
            'test_period': {'start': test_dates[0], 'end': test_dates[-1], 'days': len(test_dates)},
        }

        if 'error' not in train_result:
            fold_data['train_metrics'] = train_result.get('summary', {})
        else:
            fold_data['train_metrics'] = {'error': train_result['error']}

        if 'error' not in test_result:
            fold_data['test_metrics'] = test_result.get('summary', {})
        else:
            fold_data['test_metrics'] = {'error': test_result['error']}

        folds.append(fold_data)

    # Aggregate results
    test_returns = [f['test_metrics'].get('total_return_pct', 0) for f in folds if 'error' not in f.get('test_metrics', {})]
    test_sharpes = [f['test_metrics'].get('sharpe_ratio', 0) for f in folds if 'error' not in f.get('test_metrics', {})]
    test_max_dds = [f['test_metrics'].get('max_drawdown_pct', 0) for f in folds if 'error' not in f.get('test_metrics', {})]

    n_valid = len(test_returns)
    profitable_folds = sum(1 for r in test_returns if r > 0)

    aggregate = {
        'num_folds': len(folds),
        'num_valid': n_valid,
        'avg_test_return_pct': round(sum(test_returns) / max(n_valid, 1), 2),
        'avg_test_sharpe': round(sum(test_sharpes) / max(n_valid, 1), 3),
        'avg_test_max_dd_pct': round(sum(test_max_dds) / max(n_valid, 1), 2),
        'consistency_score': round(profitable_folds / max(n_valid, 1) * 100, 1),
        'profitable_folds': profitable_folds,
        'best_fold_return': round(max(test_returns), 2) if test_returns else 0,
        'worst_fold_return': round(min(test_returns), 2) if test_returns else 0,
        'return_std': round(_std(test_returns), 2) if len(test_returns) > 1 else 0,
    }

    return {
        'folds': folds,
        'aggregate': aggregate,
        'strategy': config.get('strategy', 'signal_driven'),
    }


# ═══════════════════════════════════════════════════════════
#  Multi-Period Stress Testing
# ═══════════════════════════════════════════════════════════

def multi_period_backtest(asset_prices, benchmark_navs=None, config=None, periods=None):
    """Run backtest across multiple distinct time periods (market regimes).

    This validates that the strategy works in different market conditions:
      - Bull market
      - Bear market
      - Sideways / volatile
      - Recovery

    Args:
        asset_prices: {code: [{'date': str, 'nav': float}, ...]}
        benchmark_navs: [{'date': str, 'nav': float}, ...]
        config: strategy config dict
        periods: optional list of {'name': str, 'start': 'YYYY-MM-DD', 'end': 'YYYY-MM-DD'}
                 If None, auto-detect periods from data

    Returns:
        {
            periods: [{name, period, metrics, regime_detected}, ...],
            aggregate: {best_period, worst_period, all_profitable, ...}
        }
    """
    config = config or {}

    if periods is None:
        periods = _auto_detect_periods(asset_prices)

    results = []
    for period in periods:
        p_start = period.get('start')
        p_end = period.get('end')
        p_name = period.get('name', f"{p_start} to {p_end}")

        # Slice data for this period
        period_navs = {}
        for code, navs in asset_prices.items():
            sliced = [n for n in navs if p_start <= n['date'] <= p_end]
            if sliced:
                period_navs[code] = sliced

        p_bm = None
        if benchmark_navs:
            p_bm = [n for n in benchmark_navs if p_start <= n['date'] <= p_end]

        if not period_navs or all(len(v) < 30 for v in period_navs.values()):
            results.append({
                'name': p_name,
                'period': {'start': p_start, 'end': p_end},
                'metrics': {'error': 'Insufficient data for this period'},
            })
            continue

        engine = BacktestEngine(config)
        result = engine.run(period_navs, p_bm)

        entry = {
            'name': p_name,
            'period': {'start': p_start, 'end': p_end},
        }

        if 'error' not in result:
            entry['metrics'] = result.get('summary', {})
            entry['benchmark'] = result.get('benchmark', {})
            entry['equity_curve'] = result.get('equity_curve', [])
            entry['trade_count'] = result['summary'].get('total_trades', 0)
        else:
            entry['metrics'] = {'error': result['error']}

        results.append(entry)

    # Aggregate across periods
    valid_results = [r for r in results if 'error' not in r.get('metrics', {})]
    returns = [r['metrics'].get('total_return_pct', 0) for r in valid_results]
    sharpes = [r['metrics'].get('sharpe_ratio', 0) for r in valid_results]
    max_dds = [r['metrics'].get('max_drawdown_pct', 0) for r in valid_results]

    n_valid = len(returns)
    all_profitable = all(r > 0 for r in returns) if returns else False

    aggregate = {
        'num_periods': len(results),
        'num_valid': n_valid,
        'all_profitable': all_profitable,
        'avg_return_pct': round(sum(returns) / max(n_valid, 1), 2),
        'avg_sharpe': round(sum(sharpes) / max(n_valid, 1), 3),
        'avg_max_dd_pct': round(sum(max_dds) / max(n_valid, 1), 2),
        'best_period': max(valid_results, key=lambda x: x['metrics'].get('total_return_pct', -999))['name'] if valid_results else None,
        'worst_period': min(valid_results, key=lambda x: x['metrics'].get('total_return_pct', 999))['name'] if valid_results else None,
        'return_dispersion': round(max(returns) - min(returns), 2) if len(returns) >= 2 else 0,
    }

    return {
        'periods': results,
        'aggregate': aggregate,
        'strategy': config.get('strategy', 'signal_driven'),
    }


# ═══════════════════════════════════════════════════════════
#  Helpers (private)
# ═══════════════════════════════════════════════════════════

def _auto_detect_periods(asset_prices):
    """Auto-detect market periods from asset price data.

    Splits the full data range into roughly equal segments.
    """
    all_dates = set()
    for code, navs in asset_prices.items():
        for n in navs:
            all_dates.add(n['date'])
    all_dates = sorted(all_dates)

    if len(all_dates) < 60:
        return [{'name': 'Full Period', 'start': all_dates[0], 'end': all_dates[-1]}]

    # Split into ~6-month segments
    segment_size = max(120, len(all_dates) // 4)
    periods = []
    i = 0
    segment_num = 1
    while i < len(all_dates):
        end_idx = min(i + segment_size, len(all_dates) - 1)
        periods.append({
            'name': f'Period {segment_num} ({all_dates[i][:7]} to {all_dates[end_idx][:7]})',
            'start': all_dates[i],
            'end': all_dates[end_idx],
        })
        i = end_idx + 1
        segment_num += 1

    return periods


def _std(values):
    """Standard deviation of a list of numbers."""
    if len(values) < 2:
        return 0
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))
