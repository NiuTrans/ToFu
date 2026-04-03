"""lib/trading_strategy_engine/pipeline — Full production analysis pipeline.

Orchestrates all strategy-engine sub-modules into one comprehensive report:
signals → comparison → ensemble → walk-forward → Monte Carlo → portfolio → assessment.
"""

from datetime import datetime

from lib.log import get_logger
from lib.trading_backtest_engine import compare_strategies
from lib.trading_signals import compute_signal_snapshot

from .ensemble import run_ensemble_backtest
from .monte_carlo import monte_carlo_simulation
from .optimization import rolling_walk_forward_optimize
from .portfolio import optimize_portfolio_allocation
from .signals import compute_multi_timeframe_signal

logger = get_logger(__name__)

__all__ = [
    'run_full_analysis',
]


def run_full_analysis(asset_prices, benchmark_navs=None, config=None,
                       run_monte_carlo=True, run_walk_forward=True,
                       run_ensemble=True, run_optimization=True):
    """Run the complete production analysis pipeline.

    This is the single entry point that orchestrates everything:
    1. Multi-timeframe signals for each asset
    2. Strategy comparison
    3. Ensemble backtest
    4. Walk-forward optimization
    5. Monte Carlo simulation
    6. Portfolio optimization
    7. Advanced risk metrics

    Returns a comprehensive analysis report suitable for making
    real investment decisions.
    """
    config = config or {}
    report = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'assets_analyzed': list(asset_prices.keys()),
        'data_coverage': {},
    }

    # Data coverage
    for code, navs in asset_prices.items():
        if navs:
            report['data_coverage'][code] = {
                'start': navs[0]['date'],
                'end': navs[-1]['date'],
                'trading_days': len(navs),
            }

    # 1. Multi-timeframe signals
    report['signals'] = {}
    for code, navs in asset_prices.items():
        if len(navs) >= 120:
            mtf = compute_multi_timeframe_signal(navs)
            report['signals'][code] = mtf
        elif len(navs) >= 60:
            snap = compute_signal_snapshot(navs)
            report['signals'][code] = snap

    # 2. Strategy comparison
    try:
        comparison = compare_strategies(asset_prices, benchmark_navs, config)
        report['strategy_comparison'] = comparison
    except Exception as e:
        logger.error('Strategy comparison failed: %s', e, exc_info=True)
        report['strategy_comparison'] = {'error': str(e)}

    # 3. Ensemble backtest
    if run_ensemble:
        try:
            ensemble = run_ensemble_backtest(asset_prices, benchmark_navs, config)
            report['ensemble'] = ensemble
        except Exception as e:
            logger.error('Ensemble backtest failed: %s', e, exc_info=True)
            report['ensemble'] = {'error': str(e)}

    # 4. Walk-forward optimization
    if run_walk_forward:
        try:
            wf = rolling_walk_forward_optimize(asset_prices, benchmark_navs)
            report['walk_forward'] = wf
        except Exception as e:
            logger.error('Walk-forward optimization failed: %s', e, exc_info=True)
            report['walk_forward'] = {'error': str(e)}

    # 5. Monte Carlo simulation
    if run_monte_carlo:
        try:
            mc = monte_carlo_simulation(asset_prices, config)
            report['monte_carlo'] = mc
        except Exception as e:
            logger.error('Monte Carlo simulation failed: %s', e, exc_info=True)
            report['monte_carlo'] = {'error': str(e)}

    # 6. Portfolio optimization (if multiple assets)
    if run_optimization and len(asset_prices) > 1:
        try:
            optimization = optimize_portfolio_allocation(asset_prices)
            report['portfolio_optimization'] = optimization
        except Exception as e:
            logger.error('Portfolio optimization failed: %s', e, exc_info=True)
            report['portfolio_optimization'] = {'error': str(e)}

    # 7. Overall assessment
    report['assessment'] = _generate_assessment(report)

    return report


def _generate_assessment(report):
    """Generate a human-readable overall assessment from all analysis results."""
    assessment = {
        'overall_confidence': 0,
        'recommendation': 'hold',
        'key_findings': [],
        'risks': [],
        'opportunities': [],
    }

    confidence_factors = []

    # From signals
    signals = report.get('signals', {})
    for code, sig in signals.items():
        if 'error' in sig:
            continue
        if 'confirmed_signal' in sig:
            # Multi-timeframe signal
            cs = sig['confirmed_signal']
            reliability = sig.get('reliability_rating', 50)
            alignment = sig.get('timeframe_alignment', 'unknown')
            score = sig.get('composite_multi_tf_score', 0)

            if cs in ('strong_buy', 'buy'):
                assessment['opportunities'].append(
                    f"{code}: {cs} (MTF score={score}, reliability={reliability}%, alignment={alignment})")
                confidence_factors.append(reliability * 0.8)
            elif cs in ('strong_sell', 'sell'):
                assessment['risks'].append(
                    f"{code}: {cs} (MTF score={score}, reliability={reliability}%)")
                confidence_factors.append(reliability * 0.3)
            else:
                confidence_factors.append(50)
        elif 'composite_score' in sig:
            score = sig['composite_score']
            if score > 20:
                assessment['opportunities'].append(f"{code}: composite score {score}")
                confidence_factors.append(60)
            elif score < -20:
                assessment['risks'].append(f"{code}: composite score {score}")
                confidence_factors.append(30)

    # From Monte Carlo
    mc = report.get('monte_carlo', {})
    if 'error' not in mc and mc:
        prob_profit = mc.get('probability_of_profit_pct', 50)
        expected_return = mc.get('expected_return_pct', 0)
        var_95 = mc.get('var_95_pct', 0)

        assessment['key_findings'].append(
            f"Monte Carlo: {prob_profit}% probability of profit, "
            f"expected return {expected_return}%, VaR95={var_95}%")

        if prob_profit > 65:
            confidence_factors.append(prob_profit)
        else:
            confidence_factors.append(prob_profit * 0.8)

    # From walk-forward
    wf = report.get('walk_forward', {})
    if 'error' not in wf and wf:
        agg = wf.get('aggregate', {})
        consistency = agg.get('consistency_score', 0)
        avg_sharpe = agg.get('avg_test_sharpe', 0)

        assessment['key_findings'].append(
            f"Walk-forward: consistency={consistency}%, avg test Sharpe={avg_sharpe}")

        if consistency > 60:
            confidence_factors.append(consistency)
        else:
            assessment['risks'].append(
                f"Strategy inconsistency: only {consistency}% profitable folds")

    # From ensemble
    ensemble = report.get('ensemble', {})
    if 'error' not in ensemble and ensemble:
        ens_summary = ensemble.get('ensemble', {}).get('summary', {})
        ens_sharpe = ens_summary.get('sharpe_ratio', 0)
        ens_return = ens_summary.get('total_return_pct', 0)

        if ens_sharpe > 0.5:
            assessment['key_findings'].append(
                f"Ensemble: Sharpe={ens_sharpe}, return={ens_return}%")

    # Overall confidence
    if confidence_factors:
        assessment['overall_confidence'] = round(
            sum(confidence_factors) / len(confidence_factors), 1)

    # Overall recommendation
    conf = assessment['overall_confidence']
    if conf >= 70 and len(assessment['opportunities']) > len(assessment['risks']):
        assessment['recommendation'] = 'buy'
    elif conf >= 55:
        assessment['recommendation'] = 'cautious_buy'
    elif conf <= 30 or len(assessment['risks']) > len(assessment['opportunities']):
        assessment['recommendation'] = 'reduce'
    else:
        assessment['recommendation'] = 'hold'

    return assessment
