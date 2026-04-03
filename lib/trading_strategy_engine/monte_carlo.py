"""lib/trading_strategy_engine/monte_carlo — Monte Carlo simulation.

Block-bootstrap Monte Carlo to estimate future return distributions
with confidence intervals, VaR, CVaR, and probability metrics.
"""

import math
import random

__all__ = [
    'monte_carlo_simulation',
]


def monte_carlo_simulation(asset_prices, config=None, num_simulations=1000,
                            forward_days=252, confidence_levels=None):
    """Run Monte Carlo simulation to estimate future return distribution.

    Method:
    1. Compute historical daily return distribution from actual price data
    2. Run N simulated paths using bootstrapped returns (block bootstrap
       to preserve autocorrelation)
    3. Optionally apply a strategy and simulate forward
    4. Report percentile-based confidence intervals

    This answers: "If I invest using this strategy, what range of outcomes
    should I expect over the next N days?"

    Args:
        asset_prices: {code: [{'date': str, 'nav': float}, ...]}
        config: backtest config (strategy, params)
        num_simulations: number of Monte Carlo paths
        forward_days: how many trading days to simulate forward
        confidence_levels: list of percentiles, default [5, 25, 50, 75, 95]

    Returns:
        {
            percentiles: {5: return%, 25: ..., 50: ..., 75: ..., 95: ...},
            expected_return: mean return%,
            probability_of_profit: percentage,
            probability_of_loss_gt_10pct: percentage,
            var_95: Value at Risk 95%,
            cvar_95: Conditional VaR 95%,
            max_expected_drawdown: expected worst drawdown,
            simulation_paths: [{percentile: 5, values: [...]}, ...],
            historical_stats: {mean_daily_return, daily_volatility, skewness, kurtosis},
        }
    """
    if confidence_levels is None:
        confidence_levels = [5, 10, 25, 50, 75, 90, 95]

    # Collect all historical daily returns across assets
    all_daily_returns = []
    for code, navs in asset_prices.items():
        for i in range(1, len(navs)):
            prev = navs[i - 1]['nav']
            curr = navs[i]['nav']
            if prev > 0:
                all_daily_returns.append((curr - prev) / prev)

    if len(all_daily_returns) < 60:
        return {'error': f'Insufficient data: {len(all_daily_returns)} daily returns (need 60+)'}

    # Compute historical statistics
    n_rets = len(all_daily_returns)
    mean_ret = sum(all_daily_returns) / n_rets
    variance = sum((r - mean_ret) ** 2 for r in all_daily_returns) / (n_rets - 1)
    daily_vol = math.sqrt(variance)

    # Skewness
    skewness = (sum((r - mean_ret) ** 3 for r in all_daily_returns) /
                (n_rets * daily_vol ** 3)) if daily_vol > 0 else 0

    # Kurtosis (excess)
    kurtosis = (sum((r - mean_ret) ** 4 for r in all_daily_returns) /
                (n_rets * daily_vol ** 4) - 3) if daily_vol > 0 else 0

    # Block bootstrap: use blocks of 5-20 days to preserve autocorrelation
    block_size = min(20, max(5, n_rets // 20))

    # Run simulations
    final_returns = []
    max_drawdowns = []
    all_paths = []  # Store subset for visualization
    store_every = max(1, num_simulations // 20)  # Store ~20 paths

    rng = random.Random(42)  # Fixed seed for reproducibility

    for sim in range(num_simulations):
        # Generate one simulated path using block bootstrap
        path_returns = []
        while len(path_returns) < forward_days:
            # Pick a random block start
            start = rng.randint(0, max(0, n_rets - block_size))
            block = all_daily_returns[start:start + block_size]
            path_returns.extend(block)
        path_returns = path_returns[:forward_days]

        # Compute cumulative return
        cumulative = 1.0
        peak = 1.0
        max_dd = 0
        path_values = [1.0]

        for ret in path_returns:
            cumulative *= (1 + ret)
            path_values.append(cumulative)
            if cumulative > peak:
                peak = cumulative
            dd = (peak - cumulative) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

        final_return = (cumulative - 1) * 100
        final_returns.append(final_return)
        max_drawdowns.append(max_dd * 100)

        if sim % store_every == 0:
            # Store every Nth path for visualization (downsampled)
            step = max(1, len(path_values) // 50)
            all_paths.append(path_values[::step])

    # Sort for percentile calculation
    final_returns.sort()
    max_drawdowns.sort()

    # Compute percentiles
    percentiles = {}
    for p in confidence_levels:
        idx = max(0, min(int(num_simulations * p / 100), num_simulations - 1))
        percentiles[p] = round(final_returns[idx], 2)

    # Probability calculations
    prob_profit = sum(1 for r in final_returns if r > 0) / num_simulations * 100
    prob_loss_gt_10 = sum(1 for r in final_returns if r < -10) / num_simulations * 100
    prob_loss_gt_20 = sum(1 for r in final_returns if r < -20) / num_simulations * 100

    # VaR and CVaR (95%)
    var_95_idx = max(0, int(num_simulations * 0.05))
    var_95 = final_returns[var_95_idx]
    cvar_95_returns = final_returns[:var_95_idx + 1]
    cvar_95 = sum(cvar_95_returns) / len(cvar_95_returns) if cvar_95_returns else var_95

    expected_return = sum(final_returns) / num_simulations
    avg_max_dd = sum(max_drawdowns) / num_simulations

    # Build percentile paths for visualization
    percentile_paths = []
    for p in [5, 25, 50, 75, 95]:
        if all_paths:
            idx = max(0, min(int(len(all_paths) * p / 100), len(all_paths) - 1))
            # Sort paths by final value at this percentile
            sorted_paths = sorted(all_paths, key=lambda path: path[-1])
            path_idx = max(0, min(int(len(sorted_paths) * p / 100), len(sorted_paths) - 1))
            percentile_paths.append({
                'percentile': p,
                'values': [round(v * 100 - 100, 2) for v in sorted_paths[path_idx]],
            })

    return {
        'percentiles': percentiles,
        'expected_return_pct': round(expected_return, 2),
        'probability_of_profit_pct': round(prob_profit, 1),
        'probability_of_loss_gt_10pct': round(prob_loss_gt_10, 1),
        'probability_of_loss_gt_20pct': round(prob_loss_gt_20, 1),
        'var_95_pct': round(var_95, 2),
        'cvar_95_pct': round(cvar_95, 2),
        'expected_max_drawdown_pct': round(avg_max_dd, 2),
        'simulation_paths': percentile_paths,
        'historical_stats': {
            'mean_daily_return_pct': round(mean_ret * 100, 4),
            'daily_volatility_pct': round(daily_vol * 100, 4),
            'annualized_volatility_pct': round(daily_vol * math.sqrt(252) * 100, 2),
            'skewness': round(skewness, 3),
            'kurtosis_excess': round(kurtosis, 3),
            'data_points': n_rets,
        },
        'config': {
            'num_simulations': num_simulations,
            'forward_days': forward_days,
            'block_size': block_size,
        },
    }
