"""lib/trading/portfolio/trade_queue.py — T+1 Trade Queue & Morning Orders.

Manages the transition from overnight analysis to market-hours execution:
  - Overnight: Brain generates orders → they enter the pending queue
  - 07:00: Morning summary consolidates all pending orders
  - 08:30: Notification pushed to user
  - 09:15: Last confirmation window
  - 09:30: Orders submitted (user must confirm)

This module handles the queue logic. Actual execution is in the route layer.
"""

from datetime import datetime, timedelta

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'get_morning_orders',
    'generate_morning_summary',
]


def get_morning_orders(db):
    """Get all pending trade orders for this morning's session.

    Returns orders grouped by batch, with urgency and confidence.
    """
    # Get pending trades ordered by creation time
    rows = db.execute('''
        SELECT * FROM trading_trade_queue
        WHERE status = 'pending'
        ORDER BY created_at DESC
    ''').fetchall()

    orders = [dict(r) for r in rows]

    # Group by batch_id
    batches = {}
    for order in orders:
        bid = order.get('batch_id', 'manual')
        if bid not in batches:
            batches[bid] = {
                'batch_id': bid,
                'created_at': order.get('created_at', ''),
                'orders': [],
                'total_buy_amount': 0,
                'total_sell_amount': 0,
            }
        batches[bid]['orders'].append(order)
        if order.get('action') == 'buy':
            batches[bid]['total_buy_amount'] += order.get('amount', 0)
        elif order.get('action') == 'sell':
            batches[bid]['total_sell_amount'] += order.get('amount', 0)

    return {
        'orders': orders,
        'batches': list(batches.values()),
        'total_pending': len(orders),
        'timestamp': datetime.now().isoformat(),
    }


def generate_morning_summary(db):
    """Generate a morning briefing summary combining:
    - Overnight alerts
    - Pending trade orders
    - Market preview
    - Key holdings at risk

    Returns a markdown string suitable for display in the UI.
    """
    lines = [f"# 📋 晨间操作简报 ({datetime.now().strftime('%Y-%m-%d %H:%M')})\n"]

    # ── Pending orders ──
    morning = get_morning_orders(db)
    if morning['total_pending'] > 0:
        lines.append(f"## 📋 待执行交易 ({morning['total_pending']}笔)\n")
        for batch in morning['batches']:
            lines.append(f"### 批次: {batch['batch_id'][:20]}")
            for order in batch['orders']:
                action_emoji = '🟢' if order['action'] == 'buy' else '🔴'
                lines.append(
                    f"  {action_emoji} {order['action'].upper()} "
                    f"{order.get('symbol', '')} {order.get('asset_name', '')} "
                    f"¥{order.get('amount', 0):,.0f} — {order.get('reason', '')[:60]}"
                )
            lines.append("")
    else:
        lines.append("## ✅ 今日暂无待执行交易\n")

    # ── Overnight alerts ──
    try:
        from lib.trading.radar.alert import get_pending_alerts
        alerts = get_pending_alerts()
        if alerts:
            lines.append(f"## ⚡ 未处理预警 ({len(alerts)}条)\n")
            for a in alerts[:5]:
                lines.append(f"- [{a.get('type', '')}] {a.get('title', '')} (紧急度: {'🔴' * a.get('urgency', 1)})")
            lines.append("")
    except Exception as e:
        logger.debug('[Portfolio] Alert check failed in morning summary: %s', e, exc_info=True)

    # ── Holdings at risk ──
    try:
        from lib.trading import get_latest_price
        holdings = db.execute('SELECT * FROM trading_holdings').fetchall()
        at_risk = []
        for h in holdings:
            h = dict(h)
            nav, _ = get_latest_price(h['symbol'])
            if nav and h.get('buy_price', 0) > 0:
                pnl_pct = (nav - h['buy_price']) / h['buy_price'] * 100
                if pnl_pct < -5:
                    at_risk.append((h, pnl_pct))

        if at_risk:
            lines.append("## ⚠️ 关注持仓 (亏损>5%)\n")
            for h, pnl in sorted(at_risk, key=lambda x: x[1]):
                lines.append(
                    f"- {h['symbol']} {h.get('asset_name', '')}: "
                    f"亏损 {pnl:.1f}%"
                )
            lines.append("")
    except Exception as e:
        logger.debug('[Portfolio] Holdings risk check failed: %s', e, exc_info=True)

    lines.append("---\n*[确认执行] 或 [暂不操作] 请在 AI操盘 → 执行队列 中操作*")
    return "\n".join(lines)
