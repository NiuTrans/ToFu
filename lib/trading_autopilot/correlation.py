"""lib/trading_autopilot/correlation.py — Intelligence Correlation Engine.

Analyzes correlations between recent intel items across categories
(macro, sector, bond, capital flow, etc.) and builds textual context
for the LLM reasoning chain.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from lib.trading_autopilot._constants import CORRELATION_WINDOW_DAYS

__all__ = [
    'CORRELATION_CATEGORIES',
    'correlate_intel_items',
    'build_correlation_context',
]

# Actual DB categories: bond_rate, fund_flow, fund_rating, global_market,
#                       macro_policy, market_trend, policy_regulation, sector_rotation
CORRELATION_CATEGORIES = {
    'macro_to_sector': {
        'description': '宏观政策如何传导到板块轮动 (macro→sector)',
        'from': ['macro_policy', 'policy_regulation'],
        'to': ['sector_rotation', 'market_trend'],
    },
    'policy_to_bond': {
        'description': '监管政策对债券利率的影响 (policy→bond)',
        'from': ['macro_policy', 'policy_regulation'],
        'to': ['bond_rate', 'fund_flow'],
    },
    'global_to_domestic': {
        'description': '全球市场对国内板块和资金流的传导 (global→domestic)',
        'from': ['global_market'],
        'to': ['market_trend', 'sector_rotation', 'fund_flow'],
    },
    'rate_to_flow': {
        'description': '利率变动影响资金流向和资产交易 (rate→flow)',
        'from': ['bond_rate', 'macro_policy'],
        'to': ['fund_flow', 'fund_rating'],
    },
    'trend_to_rating': {
        'description': '市场趋势影响资产评级和资金流入 (trend→rating)',
        'from': ['market_trend', 'sector_rotation'],
        'to': ['fund_rating', 'fund_flow'],
    },
    'sentiment_feedback': {
        'description': '资金流向和资产评级的自我强化反馈环 (flow↔rating)',
        'from': ['fund_flow', 'market_trend'],
        'to': ['sector_rotation', 'fund_rating'],
    },
}


def correlate_intel_items(db, window_days=CORRELATION_WINDOW_DAYS):
    """Analyze correlations between recent intel items across categories.

    Returns a list of correlation objects:
      { from_ids, to_ids, correlation_type, strength, reasoning }
    """
    cutoff = (datetime.now() - timedelta(days=window_days)).strftime('%Y-%m-%d %H:%M:%S')
    items = db.execute(
        'SELECT * FROM trading_intel_cache WHERE fetched_at >= ? ORDER BY fetched_at DESC',
        (cutoff,)
    ).fetchall()
    items = [dict(r) for r in items]

    if len(items) < 2:
        return []

    # Group by category
    by_cat = defaultdict(list)
    for it in items:
        by_cat[it.get('category', 'unknown')].append(it)

    correlations = []
    for corr_type, cfg in CORRELATION_CATEGORIES.items():
        from_items = []
        for cat in cfg['from']:
            from_items.extend(by_cat.get(cat, []))
        to_items = []
        for cat in cfg['to']:
            to_items.extend(by_cat.get(cat, []))

        if not from_items or not to_items:
            continue

        # Build correlation record
        correlations.append({
            'type': corr_type,
            'description': cfg['description'],
            'from_count': len(from_items),
            'to_count': len(to_items),
            'from_summaries': [f"[{it['category']}] {it['title']}" for it in from_items[:5]],
            'to_summaries': [f"[{it['category']}] {it['title']}" for it in to_items[:5]],
        })

    return correlations


def build_correlation_context(correlations):
    """Build a textual context block from correlations for the LLM."""
    if not correlations:
        return ""
    lines = ["## 情报关联分析 (Intelligence Cross-Correlation)"]
    for c in correlations:
        lines.append(f"\n### {c['type']}: {c['description']}")
        lines.append(f"  驱动因素 ({c['from_count']}条):")
        for s in c['from_summaries']:
            lines.append(f"    → {s}")
        lines.append(f"  受影响领域 ({c['to_count']}条):")
        for s in c['to_summaries']:
            lines.append(f"    ← {s}")
    return "\n".join(lines)
