"""lib/trading_strategy_engine/optimization — Rolling walk-forward optimization.

Optimizes strategy parameters on training sets and validates on test sets
across multiple rolling folds for robust parameter selection.
"""

from collections import defaultdict

from lib.log import get_logger
from lib.trading_backtest_engine import BacktestEngine

logger = get_logger(__name__)

__all__ = [
    'rolling_walk_forward_optimize',
]


def rolling_walk_forward_optimize(asset_prices, benchmark_navs=None,
                                   num_folds=5, train_ratio=0.7):
    """Rolling walk-forward with parameter optimization.

    Unlike plain walk_forward_backtest which just tests one config,
    this actually OPTIMIZES parameters on the training set and then
    validates on the test set.

    Optimizes:
    - buy_threshold
    - sell_threshold
    - strategy selection

    Returns:
        {
            folds: [{train_period, test_period, best_params, test_metrics}, ...],
            aggregate: {avg_test_return, avg_test_sharpe, consistency, ...},
            recommended_params: the best parameter set across all folds,
        }
    """
    # Get all dates
    all_dates = set()
    for code, navs in asset_prices.items():
        for n in navs:
            all_dates.add(n['date'])
    all_dates = sorted(all_dates)
    total_days = len(all_dates)

    if total_days < 120:
        return {'error': f'Insufficient data: {total_days} days (need 120+ for optimization)'}

    # Adapt folds to data size — avoid empty/tiny folds
    max_folds = max(2, total_days // 60)
    num_folds = min(num_folds, max_folds)

    # Parameter grid to search
    param_grid = [
        {'strategy': 'signal_driven', 'buy_threshold': 5, 'sell_threshold': -5},
        {'strategy': 'signal_driven', 'buy_threshold': 8, 'sell_threshold': -8},
        {'strategy': 'signal_driven', 'buy_threshold': 12, 'sell_threshold': -12},
        {'strategy': 'adaptive', 'buy_threshold': 5, 'sell_threshold': -5},
        {'strategy': 'adaptive', 'buy_threshold': 8, 'sell_threshold': -8},
        {'strategy': 'adaptive', 'buy_threshold': 12, 'sell_threshold': -12},
        {'strategy': 'trend_following', 'buy_threshold': 5, 'sell_threshold': -5},
        {'strategy': 'trend_following', 'buy_threshold': 10, 'sell_threshold': -10},
        {'strategy': 'mean_reversion', 'buy_threshold': 5, 'sell_threshold': -5},
        {'strategy': 'dca_signal', 'buy_threshold': 5, 'sell_threshold': -5},
        {'strategy': 'dca_signal', 'buy_threshold': 10, 'sell_threshold': -10},
    ]

    fold_step = total_days // num_folds
    folds = []
    param_scores = defaultdict(list)  # param_key → list of test Sharpes

    for fold_i in range(num_folds):
        fold_start = fold_i * fold_step
        # Extend each fold by 50% overlap to ensure enough data for train+test
        fold_end = min(fold_start + fold_step + int(fold_step * 0.5), total_days)
        split_point = fold_start + int((fold_end - fold_start) * train_ratio)

        train_dates = all_dates[fold_start:split_point]
        test_dates = all_dates[split_point:fold_end]

        if len(train_dates) < 40 or len(test_dates) < 10:
            continue

        # Slice data
        train_navs = {}
        test_navs = {}
        for code, navs in asset_prices.items():
            train_navs[code] = [n for n in navs if train_dates[0] <= n['date'] <= train_dates[-1]]
            test_navs[code] = [n for n in navs if test_dates[0] <= n['date'] <= test_dates[-1]]

        # Optimize on training set
        best_sharpe = -999
        best_params = None
        best_train_result = None

        for params in param_grid:
            train_config = dict(params)
            # Relax signal history requirement for shorter fold windows
            train_config['min_signal_history'] = min(20, len(train_dates) // 3)
            try:
                engine = BacktestEngine(train_config)
                result = engine.run(train_navs)
                if 'error' in result:
                    continue
                sharpe = result.get('summary', {}).get('sharpe_ratio', -999)
                if sharpe > best_sharpe:
                    best_sharpe = sharpe
                    best_params = params
                    best_train_result = result
            except Exception as e:
                logger.warning('Walk-forward backtest failed for params %s: %s', params, e, exc_info=True)
                continue

        if best_params is None:
            continue

        # Validate on test set with best params
        # For test folds: use lower signal history requirement since we
        # include a lookback overlap window for signal warmup
        test_config = dict(best_params)
        test_config['min_signal_history'] = 20  # relaxed for short test folds

        # Include signal warmup data: prepend last N days of training to test
        warmup_days = 60
        warmup_start = max(0, len(train_dates) - warmup_days)
        warmup_dates_set = set(train_dates[warmup_start:])
        test_navs_with_warmup = {}
        for code, navs in asset_prices.items():
            warmup = [n for n in navs if n['date'] in warmup_dates_set]
            test_only = [n for n in navs if test_dates[0] <= n['date'] <= test_dates[-1]]
            test_navs_with_warmup[code] = warmup + test_only

        test_engine = BacktestEngine(test_config)
        test_result = test_engine.run(test_navs_with_warmup)

        fold_data = {
            'fold': fold_i + 1,
            'train_period': {'start': train_dates[0], 'end': train_dates[-1],
                            'days': len(train_dates)},
            'test_period': {'start': test_dates[0], 'end': test_dates[-1],
                           'days': len(test_dates)},
            'best_params': best_params,
            'train_sharpe': round(best_sharpe, 3),
            'train_metrics': best_train_result.get('summary', {}) if best_train_result else {},
        }

        if 'error' not in test_result:
            fold_data['test_metrics'] = test_result.get('summary', {})
            test_sharpe = test_result['summary'].get('sharpe_ratio', 0)
            # Track param performance
            param_key = f"{best_params['strategy']}_{best_params.get('buy_threshold', 0)}"
            param_scores[param_key].append(test_sharpe)
        else:
            fold_data['test_metrics'] = {'error': test_result['error']}

        folds.append(fold_data)

    # Aggregate
    test_returns = [f['test_metrics'].get('total_return_pct', 0) for f in folds
                    if 'error' not in f.get('test_metrics', {})]
    test_sharpes = [f['test_metrics'].get('sharpe_ratio', 0) for f in folds
                    if 'error' not in f.get('test_metrics', {})]
    n_valid = len(test_returns)
    profitable_folds = sum(1 for r in test_returns if r > 0)

    # Find best overall params
    best_overall_param = None
    best_avg_sharpe = -999
    for param_key, sharpes in param_scores.items():
        avg = sum(sharpes) / len(sharpes) if sharpes else -999
        if avg > best_avg_sharpe:
            best_avg_sharpe = avg
            best_overall_param = param_key

    # Extract recommended params from best param_key
    recommended_params = None
    if best_overall_param:
        for params in param_grid:
            pk = f"{params['strategy']}_{params.get('buy_threshold', 0)}"
            if pk == best_overall_param:
                recommended_params = params
                break

    return {
        'folds': folds,
        'aggregate': {
            'num_folds': len(folds),
            'num_valid': n_valid,
            'avg_test_return_pct': round(sum(test_returns) / max(n_valid, 1), 2),
            'avg_test_sharpe': round(sum(test_sharpes) / max(n_valid, 1), 3),
            'consistency_score': round(profitable_folds / max(n_valid, 1) * 100, 1),
            'profitable_folds': profitable_folds,
            'best_fold_return': round(max(test_returns), 2) if test_returns else 0,
            'worst_fold_return': round(min(test_returns), 2) if test_returns else 0,
        },
        'recommended_params': recommended_params,
        'param_performance': {
            k: {'avg_sharpe': round(sum(v) / len(v), 3), 'n_folds': len(v)}
            for k, v in param_scores.items()
        },
    }
