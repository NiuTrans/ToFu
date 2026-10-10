"""lib/trading/portfolio_analytics.py — Portfolio value calculation and rebalance alerts.

Extracted from ``lib/trading/backtest.py`` to separate live portfolio analytics
from the deprecated v1 backtesting code.

Functions:
  calculate_portfolio_value  — enriched holdings with current NAV, PnL
  check_rebalance_alerts     — detect allocation drift beyond threshold
  calculate_avg_cost_after_add — position sizing / cost dilution calculator
"""


from lib.log import get_logger
from tofu_trading.trading._common import _get_default_client
from tofu_trading.trading.info import calc_buy_fee, fetch_asset_info
from tofu_trading.trading.nav import _prewarm_price_cache, get_latest_price

logger = get_logger(__name__)

__all__ = [
    'calculate_portfolio_value',
    'check_rebalance_alerts',
    'calculate_avg_cost_after_add',
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
    holding_rows = [dict(h) if not isinstance(h, dict) else h for h in holdings]
    # Pre-warm NAV cache in parallel (all at once, ~1-2s max)
    _prewarm_price_cache([h['symbol'] for h in holding_rows], client=client)
    for h in holding_rows:
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
