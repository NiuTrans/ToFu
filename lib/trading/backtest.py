"""lib/trading/backtest.py — Backtesting, correlation analysis, and position sizing."""

import math
from datetime import datetime, timedelta

from lib.log import get_logger
from lib.trading._common import TradingClient, _get_default_client
from lib.trading.info import calc_buy_fee, fetch_asset_info
from lib.trading.nav import _prewarm_price_cache, fetch_price_history, get_latest_price

logger = get_logger(__name__)

__all__ = [
    'backtest_hold',
    'backtest_dca',
    'backtest_portfolio',
    'analyze_correlation',
    'calculate_avg_cost_after_add',
    'calculate_portfolio_value',
    'check_rebalance_alerts',
]


# ═══════════════════════════════════════════════════════════
#  Portfolio Analytics
# ═══════════════════════════════════════════════════════════

def calculate_portfolio_value(holdings, *, client=None):
    """Calculate current portfolio value with latest price.
    Uses parallel fetching with fast-fail — never blocks for more than 2s total.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    total_value = 0
    total_cost = 0
    enriched = []
    codes = [dict(h) if not isinstance(h, dict) else h for h in holdings]
    # Pre-warm NAV cache in parallel (all at once, ~1-2s max)
    _prewarm_price_cache([h['symbol'] for h in codes], client=client)
    for h in codes:
        code = h['symbol']
        nav_val, nav_date = get_latest_price(code, client=client)
        info = fetch_asset_info(code, client=client)
        # Fallback: use buy_price as NAV if all sources fail
        if not nav_val:
            nav_val = h.get('buy_price', 0)
            nav_date = h.get('buy_date', '')
        current_value = h['shares'] * nav_val if nav_val else 0
        cost = h['shares'] * h['buy_price']
        pnl = current_value - cost if nav_val else 0
        pnl_pct = (pnl / cost * 100) if cost > 0 else 0
        enriched.append({
            **h,
            'asset_name': info.get('name', h.get('asset_name', '')) if info else h.get('asset_name', ''),
            'current_nav': nav_val,
            'nav_date': nav_date,
            'est_nav': float(info.get('est_nav', 0)) if info and info.get('est_nav') else None,
            'est_change': info.get('est_change', '') if info else '',
            'current_value': round(current_value, 2),
            'cost': round(cost, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
        })
        total_value += current_value
        total_cost += cost

    return {
        'holdings': enriched,
        'total_value': round(total_value, 2),
        'total_cost': round(total_cost, 2),
        'total_pnl': round(total_value - total_cost, 2),
        'total_pnl_pct': round((total_value - total_cost) / total_cost * 100, 2) if total_cost > 0 else 0,
    }


def check_rebalance_alerts(holdings, target_allocations, threshold=5.0, *, client=None):
    """Check if portfolio needs rebalancing.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    portfolio = calculate_portfolio_value(holdings, client=client)
    total_value = portfolio['total_value']
    if total_value <= 0:
        return {'need_rebalance': False, 'details': []}

    alerts = []
    for h in portfolio['holdings']:
        code = h['symbol']
        actual_pct = (h['current_value'] / total_value * 100) if total_value > 0 else 0
        target_pct = target_allocations.get(code, actual_pct)
        deviation = actual_pct - target_pct
        if abs(deviation) > threshold:
            action = '减持' if deviation > 0 else '加仓'
            alerts.append({
                'symbol': code,
                'asset_name': h.get('asset_name', ''),
                'actual_pct': round(actual_pct, 2),
                'target_pct': round(target_pct, 2),
                'deviation': round(deviation, 2),
                'action': action,
            })
    return {
        'need_rebalance': len(alerts) > 0,
        'details': alerts,
    }


# ═══════════════════════════════════════════════════════════
#  Backtesting Engine
# ═══════════════════════════════════════════════════════════

def backtest_portfolio(asset_allocations, start_date, end_date, initial_amount=100000,
                       rebalance_interval='monthly', dip_buy_threshold=-3.0,
                       benchmark_index='000300', *, client=None):
    """Full portfolio backtesting with rebalancing and dip-buying.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    # Fetch data
    asset_data = {}
    all_dates = set()
    for alloc in asset_allocations:
        code = alloc['symbol']
        hist = fetch_price_history(code, start_date, end_date, client=client)
        asset_data[code] = {item['date']: item['nav'] for item in hist}
        all_dates.update(asset_data[code].keys())

    # Benchmark
    benchmark_data = {}
    try:
        bm_hist = fetch_price_history(benchmark_index, start_date, end_date, client=client)
        benchmark_data = {item['date']: item['nav'] for item in bm_hist}
    except Exception as e:
        logger.warning('[Backtest] benchmark index %s NAV fetch failed, running without benchmark: %s', benchmark_index, e, exc_info=True)

    all_dates = sorted(all_dates)
    if len(all_dates) < 5:
        return {'error': 'Insufficient data points for backtesting'}

    # Initialize
    target_weights = {}
    portfolio = {}
    operations = []
    total_invested = initial_amount

    for alloc in asset_allocations:
        code = alloc['symbol']
        weight = float(alloc['weight'])
        target_weights[code] = weight
        allocated = initial_amount * weight
        first_nav = None
        for d in all_dates:
            if d in asset_data.get(code, {}):
                first_nav = asset_data[code][d]
                break
        if first_nav and first_nav > 0:
            shares = allocated / first_nav
            portfolio[code] = {'shares': shares, 'cost': allocated, 'last_nav': first_nav}
            operations.append({'date': all_dates[0], 'type': 'initial_buy', 'asset': code,
                               'amount': allocated, 'nav': first_nav, 'shares': shares})

    # Calculate NAV curve
    nav_curve = []
    drawdown_curve = []
    benchmark_curve = []
    peak = 0
    bm_start = None
    last_rebalance_month = None

    for d in all_dates:
        total_val = 0
        for code, pos in portfolio.items():
            if d in asset_data.get(code, {}):
                pos['last_nav'] = asset_data[code][d]
            total_val += pos['shares'] * pos['last_nav']

        nav_curve.append({'date': d, 'value': round(total_val, 2)})
        peak = max(peak, total_val)
        dd = ((total_val - peak) / peak * 100) if peak > 0 else 0
        drawdown_curve.append({'date': d, 'drawdown': round(dd, 2)})

        if d in benchmark_data:
            if bm_start is None:
                bm_start = benchmark_data[d]
            bm_ret = ((benchmark_data[d] - bm_start) / bm_start * 100) if bm_start else 0
            benchmark_curve.append({'date': d, 'return': round(bm_ret, 2)})

        # Rebalancing
        current_month = d[:7]
        if rebalance_interval == 'monthly' and current_month != last_rebalance_month and total_val > 0:
            last_rebalance_month = current_month
            for code in portfolio:
                actual_w = (portfolio[code]['shares'] * portfolio[code]['last_nav']) / total_val
                target_w = target_weights.get(code, actual_w)
                if abs(actual_w - target_w) > 0.05:
                    target_val = total_val * target_w
                    current_val = portfolio[code]['shares'] * portfolio[code]['last_nav']
                    diff = target_val - current_val
                    if portfolio[code]['last_nav'] > 0:
                        share_diff = diff / portfolio[code]['last_nav']
                        portfolio[code]['shares'] += share_diff
                        operations.append({'date': d, 'type': 'rebalance', 'asset': code,
                                           'amount': round(abs(diff), 2),
                                           'action': 'buy' if diff > 0 else 'sell',
                                           'shares': round(abs(share_diff), 4)})

        # Dip buying
        if dip_buy_threshold and total_val > 0:
            for code in portfolio:
                if d in asset_data.get(code, {}):
                    hist_slice = [asset_data[code][dd] for dd in all_dates if dd <= d and dd in asset_data.get(code, {})]
                    if len(hist_slice) >= 2:
                        daily_change = (hist_slice[-1] - hist_slice[-2]) / hist_slice[-2] * 100
                        if daily_change <= dip_buy_threshold:
                            dip_amount = total_invested * 0.02
                            if portfolio[code]['last_nav'] > 0:
                                dip_shares = dip_amount / portfolio[code]['last_nav']
                                portfolio[code]['shares'] += dip_shares
                                total_invested += dip_amount
                                operations.append({'date': d, 'type': 'dip_buy', 'asset': code,
                                                   'amount': round(dip_amount, 2),
                                                   'nav': portfolio[code]['last_nav'],
                                                   'shares': round(dip_shares, 4)})

    # Metrics
    if len(nav_curve) >= 2:
        start_val = nav_curve[0]['value']
        end_val = nav_curve[-1]['value']
        total_return = ((end_val - start_val) / start_val * 100) if start_val > 0 else 0
        days = (datetime.strptime(nav_curve[-1]['date'], '%Y-%m-%d') -
                datetime.strptime(nav_curve[0]['date'], '%Y-%m-%d')).days
        annual_return = ((1 + total_return / 100) ** (365 / max(days, 1)) - 1) * 100 if days > 0 else 0
        max_dd = min(d['drawdown'] for d in drawdown_curve) if drawdown_curve else 0
        # Sharpe
        daily_returns = []
        for i in range(1, len(nav_curve)):
            prev = nav_curve[i - 1]['value']
            curr = nav_curve[i]['value']
            if prev > 0:
                daily_returns.append((curr - prev) / prev)
        avg_r = sum(daily_returns) / len(daily_returns) if daily_returns else 0
        std_r = (sum((r - avg_r) ** 2 for r in daily_returns) / max(len(daily_returns) - 1, 1)) ** 0.5
        sharpe = (avg_r / std_r * math.sqrt(252)) if std_r > 0 else None
        # Benchmark
        bm_return = benchmark_curve[-1]['return'] if benchmark_curve else None
    else:
        total_return = annual_return = max_dd = 0
        sharpe = bm_return = None

    metrics = {
        'total_return': round(total_return, 2),
        'annual_return': round(annual_return, 2),
        'max_drawdown': round(max_dd, 2),
        'sharpe_ratio': round(sharpe, 3) if sharpe is not None else None,
        'benchmark_return': round(bm_return, 2) if bm_return is not None else None,
        'num_rebalances': sum(1 for op in operations if op['type'] == 'rebalance'),
        'num_dip_buys': sum(1 for op in operations if op['type'] == 'dip_buy'),
    }

    return {
        'nav_curve': nav_curve,
        'drawdown_curve': drawdown_curve,
        'benchmark_curve': benchmark_curve,
        'metrics': metrics,
        'operations': operations,
    }


def backtest_dca(symbol, monthly_amount, start_date, end_date, benchmark_index='000300',
                 *, client=None):
    """Backtest Dollar Cost Averaging (定投) strategy.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    hist = fetch_price_history(symbol, start_date, end_date, client=client)
    if len(hist) < 5:
        return {'error': 'Insufficient data for DCA backtest'}

    nav_by_date = {item['date']: item['nav'] for item in hist}
    all_dates = sorted(nav_by_date.keys())

    shares = 0
    total_invested = 0
    nav_curve = []
    operations = []
    last_invest_month = None

    for d in all_dates:
        month = d[:7]
        if month != last_invest_month:
            last_invest_month = month
            nav = nav_by_date[d]
            if nav > 0:
                bought = monthly_amount / nav
                shares += bought
                total_invested += monthly_amount
                operations.append({'date': d, 'type': 'dca_buy', 'amount': monthly_amount,
                                   'nav': nav, 'shares': round(bought, 4)})
        current_val = shares * nav_by_date[d]
        nav_curve.append({'date': d, 'value': round(current_val, 2), 'invested': round(total_invested, 2)})

    if len(nav_curve) >= 2:
        end_val = nav_curve[-1]['value']
        total_return = ((end_val - total_invested) / total_invested * 100) if total_invested > 0 else 0
    else:
        total_return = 0

    return {
        'nav_curve': nav_curve,
        'metrics': {
            'total_invested': round(total_invested, 2),
            'final_value': round(nav_curve[-1]['value'], 2) if nav_curve else 0,
            'total_return': round(total_return, 2),
            'total_shares': round(shares, 4),
            'avg_cost': round(total_invested / shares, 4) if shares > 0 else 0,
            'num_investments': len(operations),
        },
        'operations': operations,
    }


def backtest_hold(symbol, start_date, end_date, initial_amount=100000, benchmark_index='000300',
                  *, client=None):
    """Backtest buy-and-hold strategy.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    hist = fetch_price_history(symbol, start_date, end_date, client=client)
    if len(hist) < 5:
        return {'error': 'Insufficient data for hold backtest'}

    nav_by_date = {item['date']: item['nav'] for item in hist}
    all_dates = sorted(nav_by_date.keys())
    first_nav = nav_by_date[all_dates[0]]
    shares = initial_amount / first_nav if first_nav > 0 else 0

    nav_curve = []
    peak = 0
    drawdown_curve = []
    for d in all_dates:
        val = shares * nav_by_date[d]
        nav_curve.append({'date': d, 'value': round(val, 2)})
        peak = max(peak, val)
        dd = ((val - peak) / peak * 100) if peak > 0 else 0
        drawdown_curve.append({'date': d, 'drawdown': round(dd, 2)})

    end_val = nav_curve[-1]['value'] if nav_curve else 0
    total_return = ((end_val - initial_amount) / initial_amount * 100) if initial_amount > 0 else 0
    max_dd = min(d['drawdown'] for d in drawdown_curve) if drawdown_curve else 0

    return {
        'nav_curve': nav_curve,
        'drawdown_curve': drawdown_curve,
        'metrics': {
            'total_return': round(total_return, 2),
            'max_drawdown': round(max_dd, 2),
            'final_value': round(end_val, 2),
        },
    }


# ═══════════════════════════════════════════════════════════
#  Correlation Analysis
# ═══════════════════════════════════════════════════════════

def analyze_correlation(codes, days=90, *, client=None):
    """Analyze NAV correlation between multiple assets over a period.
    Returns correlation matrix and analysis.

    Args:
        client: Optional ``TradingClient`` instance for dependency injection.
    """
    if client is None:
        client = _get_default_client()
    if len(codes) < 2:
        return {'error': 'Need at least 2 asset codes'}

    end_date = datetime.now().strftime('%Y-%m-%d')
    start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')

    # Fetch price histories
    asset_prices = {}
    for code in codes:
        hist = fetch_price_history(code, start_date, end_date, client=client)
        if hist:
            asset_prices[code] = {item['date']: item['nav'] for item in hist}

    if len(asset_prices) < 2:
        return {
            'error': '无法获取足够的历史价格数据，至少需要2只标的的数据。',
            'available': list(asset_prices.keys()),
        }

    # Find common dates
    all_dates = set()
    for navs in asset_prices.values():
        all_dates.update(navs.keys())
    common_dates = sorted(all_dates)

    # Build daily return series (only on dates where ALL assets have data)
    returns = {code: [] for code in asset_prices}
    prev_navs = {}
    for dt in common_dates:
        all_present = all(dt in asset_prices[c] for c in asset_prices)
        if not all_present:
            continue
        if prev_navs:
            for code in asset_prices:
                prev = prev_navs.get(code)
                curr = asset_prices[code][dt]
                if prev and prev > 0:
                    returns[code].append((curr - prev) / prev)
        prev_navs = {code: asset_prices[code][dt] for code in asset_prices}

    # Calculate correlation matrix
    code_list = list(asset_prices.keys())
    n = len(code_list)
    matrix = [[0.0] * n for _ in range(n)]

    for i in range(n):
        for j in range(n):
            if i == j:
                matrix[i][j] = 1.0
            elif i < j:
                ri = returns[code_list[i]]
                rj = returns[code_list[j]]
                min_len = min(len(ri), len(rj))
                if min_len < 5:
                    matrix[i][j] = matrix[j][i] = 0.0
                    continue
                ri, rj = ri[:min_len], rj[:min_len]
                mean_i = sum(ri) / len(ri)
                mean_j = sum(rj) / len(rj)
                cov = sum((a - mean_i) * (b - mean_j) for a, b in zip(ri, rj)) / len(ri)
                std_i = math.sqrt(sum((a - mean_i) ** 2 for a in ri) / len(ri))
                std_j = math.sqrt(sum((b - mean_j) ** 2 for b in rj) / len(rj))
                if std_i > 0 and std_j > 0:
                    corr = cov / (std_i * std_j)
                else:
                    corr = 0.0
                matrix[i][j] = round(corr, 4)
                matrix[j][i] = round(corr, 4)

    # Build result
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append({
                'asset_a': code_list[i],
                'asset_b': code_list[j],
                'correlation': matrix[i][j],
                'data_points': min(len(returns[code_list[i]]), len(returns[code_list[j]])),
                'level': (
                    '高度正相关' if matrix[i][j] > 0.7 else
                    '中度正相关' if matrix[i][j] > 0.4 else
                    '弱相关' if matrix[i][j] > -0.2 else
                    '负相关'
                ),
            })

    return {
        'codes': code_list,
        'matrix': matrix,
        'pairs': pairs,
        'period_days': days,
        'common_data_points': min(len(r) for r in returns.values()) if returns else 0,
    }


# ═══════════════════════════════════════════════════════════
#  Position Sizing / Cost Dilution Calculator
# ═══════════════════════════════════════════════════════════

def calculate_avg_cost_after_add(current_shares=0, current_avg_cost=0, add_amount=0,
                                  current_nav=None, symbol=None,
                                  current_avg_price=None, *, client=None, **kwargs):
    """Calculate new average cost after adding to an existing position.
    Useful for DCA / dip-buying cost dilution analysis.

    Args:
        current_shares: existing shares held (number or string)
        current_avg_cost: current average cost per share
        current_avg_price: alias for current_avg_cost (server compat)
        add_amount: amount (in ¥) to add
        current_nav: current NAV per share (if None, will fetch)
        symbol: symbol (used to fetch price if current_nav is None)
        client: Optional ``TradingClient`` instance for dependency injection.

    Returns dict with: new_shares, new_avg_cost, cost_reduction_pct, total_cost, total_shares
    """
    if client is None:
        client = _get_default_client()
    # Accept either param name
    if current_avg_price is not None and not current_avg_cost:
        current_avg_cost = current_avg_price

    # Ensure numeric types
    current_shares = float(current_shares or 0)
    current_avg_cost = float(current_avg_cost or 0)
    add_amount = float(add_amount or 0)
    if current_nav is not None:
        current_nav = float(current_nav)

    if (current_nav is None or current_nav <= 0) and symbol:
        nav, _ = get_latest_price(symbol, client=client)
        current_nav = nav if nav else current_avg_cost  # fallback to old cost

    if not current_nav or current_nav <= 0:
        return {'error': 'Cannot determine current NAV'}
    if add_amount <= 0:
        return {'error': 'add_amount must be > 0'}

    # Calculate fee
    fee_info = calc_buy_fee(symbol or '', add_amount, client=client)
    net_amount = fee_info['net_amount']

    new_shares = net_amount / current_nav
    total_shares = current_shares + new_shares
    total_cost = (current_shares * current_avg_cost) + add_amount

    new_avg_cost = total_cost / total_shares if total_shares > 0 else 0
    cost_reduction = ((current_avg_cost - new_avg_cost) / current_avg_cost * 100) if current_avg_cost > 0 else 0

    return {
        'current_shares': round(current_shares, 2),
        'current_avg_cost': round(current_avg_cost, 4),
        'add_amount': add_amount,
        'buy_nav': round(current_nav, 4),
        'buy_fee': fee_info['fee_amount'],
        'new_shares_bought': round(new_shares, 2),
        'total_shares': round(total_shares, 2),
        'new_avg_cost': round(new_avg_cost, 4),
        'cost_reduction_pct': round(cost_reduction, 2),
        'total_cost': round(total_cost, 2),
    }


# Aliases for backward-compatibility
run_portfolio_backtest = backtest_portfolio
