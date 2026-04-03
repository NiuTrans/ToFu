"""lib/trading_backtest_engine/reporting.py — Metrics Computation & Reporting

Computes comprehensive backtest performance metrics from simulation state:
  - Total / annualised return
  - Sharpe, Sortino, Calmar ratios
  - Max drawdown analysis
  - Win rate & profit factor
  - Benchmark comparison
  - Equity curve, trade log, final positions
"""

import math

__all__ = [
    "compute_metrics",
]


def compute_metrics(state, bm_lookup, all_dates, initial_capital):
    """Compute comprehensive backtest metrics from simulation state.

    Args:
        state: BacktestState with daily_values, trade_log, positions, etc.
        bm_lookup: {date_str: nav} benchmark lookup (may be empty dict).
        all_dates: sorted list of date strings.
        initial_capital: starting capital value.

    Returns:
        dict with summary, benchmark, equity_curve, trade_log, final_positions,
        drawdown_events keys.
    """
    if not state.daily_values:
        return {'error': 'No daily values recorded'}

    values = [d['value'] for d in state.daily_values]
    dates = [d['date'] for d in state.daily_values]
    n = len(values)

    initial = values[0] if values else initial_capital
    final = values[-1] if values else initial

    # ── Total return ──────────────────────────────────────
    total_return_pct = (final - initial) / initial * 100 if initial > 0 else 0

    # ── Annualised return ─────────────────────────────────
    days = n
    years = days / 252.0
    if years > 0 and final > 0 and initial > 0:
        annualized_return = (math.pow(final / initial, 1 / years) - 1) * 100
    else:
        annualized_return = 0

    # ── Daily returns ─────────────────────────────────────
    daily_returns = []
    for i in range(1, n):
        if values[i - 1] > 0:
            daily_returns.append((values[i] - values[i - 1]) / values[i - 1])
        else:
            daily_returns.append(0)

    # ── Volatility (annualised) ───────────────────────────
    if daily_returns:
        avg_ret = sum(daily_returns) / len(daily_returns)
        variance = sum((r - avg_ret) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)
        daily_vol = math.sqrt(variance)
        annualized_vol = daily_vol * math.sqrt(252) * 100
    else:
        avg_ret = 0
        daily_vol = 0
        annualized_vol = 0

    # ── Sharpe ratio (risk-free rate = 2.5 % for CNY) ────
    rf_daily = 0.025 / 252
    if daily_vol > 0:
        sharpe = (avg_ret - rf_daily) / daily_vol * math.sqrt(252)
    else:
        sharpe = 0

    # ── Sortino ratio (downside deviation only) ───────────
    downside_rets = [r for r in daily_returns if r < rf_daily]
    if downside_rets:
        downside_std = math.sqrt(
            sum((r - rf_daily) ** 2 for r in downside_rets) / max(len(downside_rets) - 1, 1)
        )
        sortino = (avg_ret - rf_daily) / downside_std * math.sqrt(252) if downside_std > 0 else 0
    else:
        sortino = 0

    # ── Max drawdown ──────────────────────────────────────
    peak = values[0]
    max_dd = 0
    max_dd_start = max_dd_end = dates[0]
    dd_start = dates[0]
    for i in range(1, n):
        if values[i] > peak:
            peak = values[i]
            dd_start = dates[i]
        dd = (peak - values[i]) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_start = dd_start
            max_dd_end = dates[i]

    # ── Calmar ratio ──────────────────────────────────────
    calmar = annualized_return / (max_dd * 100) if max_dd > 0 else 0

    # ── Win rate ──────────────────────────────────────────
    winning_trades = [
        t for t in state.trade_log
        if t['type'] == 'sell'
        and t.get('net_proceeds', t.get('amount', 0)) > t.get('amount', 0) * 0.95
    ]
    sell_trades = [t for t in state.trade_log if t['type'] == 'sell']
    win_rate = len(winning_trades) / max(len(sell_trades), 1) * 100

    # ── Benchmark comparison ──────────────────────────────
    bm_return = None
    bm_values = []
    if bm_lookup:
        common_dates = [d for d in dates if d in bm_lookup]
        if len(common_dates) >= 2:
            first_bm = bm_lookup[common_dates[0]]
            last_bm = bm_lookup[common_dates[-1]]
            if first_bm > 0:
                bm_return = (last_bm - first_bm) / first_bm * 100
            bm_values = [
                {'date': d, 'value': bm_lookup[d] / first_bm * initial}
                for d in common_dates
            ]

    # ── Profit factor ─────────────────────────────────────
    gross_profit = sum(max(r, 0) for r in daily_returns) if daily_returns else 0
    gross_loss = sum(abs(min(r, 0)) for r in daily_returns) if daily_returns else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

    return {
        'summary': {
            'initial_capital': round(initial, 2),
            'final_value': round(final, 2),
            'total_return_pct': round(total_return_pct, 2),
            'annualized_return_pct': round(annualized_return, 2),
            'annualized_volatility_pct': round(annualized_vol, 2),
            'sharpe_ratio': round(sharpe, 3),
            'sortino_ratio': round(sortino, 3),
            'calmar_ratio': round(calmar, 3),
            'max_drawdown_pct': round(max_dd * 100, 2),
            'max_drawdown_period': {'start': max_dd_start, 'end': max_dd_end},
            'total_trades': len(state.trade_log),
            'total_fees': round(state.total_fees, 2),
            'win_rate_pct': round(win_rate, 1),
            'profit_factor': round(profit_factor, 3),
        },
        'benchmark': {
            'return_pct': round(bm_return, 2) if bm_return is not None else None,
            'alpha_pct': round(total_return_pct - bm_return, 2) if bm_return is not None else None,
        },
        'equity_curve': [
            {'date': d['date'], 'value': round(d['value'], 2)}
            for d in state.daily_values[::max(1, n // 500)]
        ],
        'benchmark_curve': bm_values[::max(1, len(bm_values) // 500)] if bm_values else [],
        'trade_log': state.trade_log[-200:],  # Last 200 trades
        'final_positions': {
            code: {
                'shares': round(pos['shares'], 4),
                'cost': round(pos['cost'], 2),
                'current_value': round(pos['current_value'], 2),
                'pnl_pct': round(
                    (pos['current_value'] - pos['cost']) / pos['cost'] * 100, 2
                ) if pos['cost'] > 0 else 0,
            }
            for code, pos in state.positions.items()
        },
        'drawdown_events': [
            d for d in state.drawdown_levels if d.get('level') != 'normal'
        ][-20:],
    }
