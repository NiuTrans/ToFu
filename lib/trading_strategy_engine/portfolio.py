"""lib/trading_strategy_engine/portfolio — Portfolio construction optimizer.

Risk-parity + signal overlay portfolio weight optimization with
constraint handling (max/min weights, volatility targets).

Uses the Strategy pattern from ``strategy.py`` for allocation methods.
Custom strategies can be registered via ``StrategyRegistry``.
"""

from lib.trading_signals import compute_signal_snapshot, rolling_volatility

from .signals import compute_multi_timeframe_signal
from .strategy import get_allocation_strategy

__all__ = [
    'optimize_portfolio_allocation',
]


def optimize_portfolio_allocation(asset_prices, target_volatility=0.15,
                                   max_single_weight=0.35,
                                   min_weight=0.05, method='risk_signal'):
    """Optimize portfolio weights using risk-parity + signal overlay.

    Methods (built-in — register custom ones via StrategyRegistry):
    - 'risk_parity': Weights inversely proportional to volatility
    - 'equal': Equal weight across all assets
    - 'risk_signal': Risk-parity base adjusted by signal strength
    - 'min_vol': Minimize portfolio volatility (simplified)

    Args:
        asset_prices: {code: [{'date': str, 'nav': float}, ...]}
        target_volatility: annual target vol
        max_single_weight: max weight per asset
        min_weight: minimum meaningful weight
        method: allocation method name (or AllocationStrategy instance)

    Returns:
        {
            weights: {code: weight},
            expected_vol: portfolio expected volatility,
            method: str,
            asset_analysis: {code: {vol, signal, raw_weight, final_weight}},
        }
    """
    if not asset_prices or len(asset_prices) < 1:
        return {'error': 'Need at least 1 asset'}

    # Compute volatility and signals for each asset
    asset_analysis = {}
    for code, navs in asset_prices.items():
        if len(navs) < 60:
            continue
        # Volatility
        vol = rolling_volatility(navs, 20)
        latest_vol = next((v for v in reversed(vol) if v is not None), 0.20)

        # Signal
        signal = compute_signal_snapshot(navs)
        composite_score = signal.get('composite_score', 0) if 'error' not in signal else 0

        # Multi-timeframe if enough data
        mtf_score = 0
        if len(navs) >= 120:
            mtf = compute_multi_timeframe_signal(navs)
            if 'error' not in mtf:
                mtf_score = mtf.get('composite_multi_tf_score', 0)

        asset_analysis[code] = {
            'volatility': latest_vol,
            'composite_score': composite_score,
            'mtf_score': mtf_score,
            'signal': signal.get('signal', 'neutral') if 'error' not in signal else 'unknown',
        }

    if not asset_analysis:
        return {'error': 'No assets with sufficient data'}

    codes = list(asset_analysis.keys())
    n_assets = len(codes)

    # ── Delegate to Strategy pattern ──
    strategy = get_allocation_strategy(method)
    raw_weights = strategy.compute_weights(codes, asset_analysis)

    # Apply constraints
    final_weights = _apply_weight_constraints(raw_weights, max_single_weight, min_weight)

    # Compute expected portfolio volatility (simplified — no correlation)
    port_vol = sum(final_weights.get(c, 0) * asset_analysis[c]['volatility'] for c in codes)

    # Compute expected annual return from price data
    weighted_annual_return = 0.0
    for c in codes:
        w = final_weights.get(c, 0)
        if w > 0 and c in asset_prices and len(asset_prices[c]) >= 60:
            navs_c = asset_prices[c]
            daily_rets = [(navs_c[i]['nav'] - navs_c[i-1]['nav']) / navs_c[i-1]['nav']
                         for i in range(1, len(navs_c)) if navs_c[i-1]['nav'] > 0]
            if daily_rets:
                avg_daily = sum(daily_rets) / len(daily_rets)
                ann_return = avg_daily * 252 * 100  # annualized %
                weighted_annual_return += w * ann_return
                asset_analysis[c]['annual_return_pct'] = round(ann_return, 2)

    # Add analysis to each asset
    for c in codes:
        asset_analysis[c]['raw_weight'] = round(raw_weights.get(c, 0), 4)
        asset_analysis[c]['final_weight'] = round(final_weights.get(c, 0), 4)
        asset_analysis[c]['volatility'] = round(asset_analysis[c]['volatility'], 4)

    return {
        'weights': {c: round(w, 4) for c, w in final_weights.items()},
        'expected_portfolio_vol': round(port_vol, 4),
        'expected_portfolio_vol_pct': round(port_vol * 100, 2),
        'expected_annual_return_pct': round(weighted_annual_return, 2),
        'method': method,
        'num_assets': n_assets,
        'asset_analysis': asset_analysis,
    }


def _apply_weight_constraints(weights, max_weight, min_weight):
    """Apply max/min weight constraints and renormalize.

    Uses iterative capping to preserve relative ordering even when
    max_weight < 1/n_assets (e.g. 2 assets with max_weight=0.35).
    Falls back to proportional renormalization when constraints
    make equal-capping the only option.
    """
    if not weights:
        return weights

    # Remove below-minimum first
    active = {c: w for c, w in weights.items() if w >= min_weight}
    if not active:
        active = dict(weights)  # keep all if everything is below min

    # Effective max: if fewer assets than 1/max_weight allows, relax max
    n_active = len(active)
    effective_max = max(max_weight, 1.0 / n_active) if n_active > 0 else max_weight

    # Iterative capping (converges in ≤ n iterations)
    for _ in range(n_active + 1):
        total = sum(active.values())
        if total <= 0:
            return {c: 1.0 / n_active for c in active} if n_active > 0 else weights
        normed = {c: w / total for c, w in active.items()}

        capped_any = False
        for c, w in normed.items():
            if w > effective_max:
                active[c] = effective_max * total  # will be renormed next iter
                capped_any = True
        if not capped_any:
            return {c: round(w, 6) for c, w in normed.items()}

    # Final renormalize
    total = sum(active.values())
    if total > 0:
        return {c: round(w / total, 6) for c, w in active.items() if w > 0}
    return weights
