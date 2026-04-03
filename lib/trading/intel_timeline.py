"""lib/trading/intel_timeline.py — Time-Locked Intelligence Queries.

Provides the crucial simulation-grade intelligence access layer:
when backtesting at time T, only intel published BEFORE T is visible.
This prevents future-data leakage in the decision-making pipeline.

Key functions:
  build_intel_context_at   — Build intel context as it would have appeared at a past date
  query_intel_window       — Query raw intel items within a time window
  get_intel_snapshot_dates — Get all dates that have intel coverage (for backtest stepping)
  build_regime_intel_features — Extract numeric features from intel for regime detection

Design:
  The existing ``build_intel_context`` always queries "now".
  This module mirrors that logic but accepts an ``as_of`` datetime,
  filtering on ``published_date <= as_of`` instead of ``fetched_at >= cutoff``.
  This is the ONLY correct way to simulate real decision conditions.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from lib.log import get_logger
from lib.trading.intel import INTEL_SOURCES

logger = get_logger(__name__)

__all__ = [
    'build_intel_context_at',
    'query_intel_window',
    'get_intel_snapshot_dates',
    'build_regime_intel_features',
]


# ═══════════════════════════════════════════════════════════
#  Time-Locked Intel Context (for backtesting)
# ═══════════════════════════════════════════════════════════

def build_intel_context_at(
    db: Any,
    as_of: str,
    max_items: int = 60,
    only_confident_dates: bool = True,
) -> tuple[str, int]:
    """Build a structured intel context as it would have appeared at ``as_of``.

    This is the backtesting-safe version of ``build_intel_context``.
    It ONLY includes intel items with ``published_date <= as_of``,
    ensuring zero future-data leakage.

    The time-layering logic mirrors the live version:
      - "Recent" = published within last 3 days of as_of
      - "Earlier" = published before that, within decision_window

    Args:
        db:        Database connection (row_factory=Row).
        as_of:     Cutoff date string 'YYYY-MM-DD'.  Only intel published
                   on or before this date is included.
        max_items: Reserved for future per-category capping.
        only_confident_dates: If True (default), exclude items with
                   date_source='fetched_at_fallback' to prevent temporal
                   leakage from items whose dates were guessed.

    Returns:
        (intel_text, item_count) where intel_text is Markdown-formatted.
    """
    try:
        as_of_dt = datetime.strptime(as_of, '%Y-%m-%d')
    except (ValueError, TypeError) as e:
        logger.warning('[IntelTimeline] Invalid as_of date %r: %s', as_of, e)
        return '', 0

    sections = []
    total_items = 0

    for cat, src in sorted(INTEL_SOURCES.items(), key=lambda x: x[1]['priority']):
        window = src.get('decision_window_days', 7)
        window_start = (as_of_dt - timedelta(days=window)).strftime('%Y-%m-%d')

        # CRITICAL: published_date <= as_of  (NO future data)
        #           published_date >= window_start (within decision window)
        #           date_source != 'fetched_at_fallback' (no guessed dates in backtest)
        if only_confident_dates:
            rows = db.execute(
                '''SELECT title, summary, analysis, relevance_score,
                          published_date, fetched_at, category
                   FROM trading_intel_cache
                   WHERE category = ?
                     AND published_date != ''
                     AND published_date >= ?
                     AND published_date <= ?
                     AND (date_source IS NULL OR date_source != 'fetched_at_fallback')
                   ORDER BY published_date DESC''',
                (cat, window_start, as_of)
            ).fetchall()
        else:
            rows = db.execute(
                '''SELECT title, summary, analysis, relevance_score,
                          published_date, fetched_at, category
                   FROM trading_intel_cache
                   WHERE category = ?
                     AND published_date != ''
                     AND published_date >= ?
                     AND published_date <= ?
                   ORDER BY published_date DESC''',
                (cat, window_start, as_of)
            ).fetchall()

        if not rows:
            continue

        items = [dict(r) for r in rows]
        total_items += len(items)

        # Split into time buckets relative to as_of
        recent = []
        earlier = []
        recent_cutoff = (as_of_dt - timedelta(days=3)).strftime('%Y-%m-%d')

        for it in items:
            bucket = recent if it['published_date'] >= recent_cutoff else earlier
            bucket.append(it)

        lines = [f"### {src['label']}（{as_of}前{window}天，共{len(items)}条）"]

        if recent:
            lines.append(f"**近3天（{len(recent)}条）：**")
            for it in recent[:10]:
                score = it.get('relevance_score', 0)
                marker = '🔴' if score >= 0.8 else '🟡' if score >= 0.5 else '⚪'
                lines.append(f"- {marker} [{it['published_date']}] "
                             f"{it['title']}: {it['summary'][:120]}")

        if earlier:
            lines.append(f"**更早（{len(earlier)}条）：**")
            for it in earlier[:8]:
                lines.append(f"- [{it['published_date']}] "
                             f"{it['title']}: {it['summary'][:100]}")

        sections.append((src['priority'], '\n'.join(lines)))

    if not sections:
        return '', 0

    header = (
        f"## 情报中心 (截至 {as_of}，覆盖{len(sections)}个分类，共{total_items}条)\n"
        f"_⚠️ 时间锁定模式：仅包含 {as_of} 及之前发布的情报_\n"
    )
    body = '\n\n'.join(text for _, text in sections)
    return header + '\n' + body, total_items


# ═══════════════════════════════════════════════════════════
#  Raw Intel Window Query
# ═══════════════════════════════════════════════════════════

def query_intel_window(
    db: Any,
    start_date: str,
    end_date: str,
    categories: list[str] | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query raw intel items within a time window.

    Returns list of dicts with all intel fields, sorted by published_date DESC.

    Args:
        db:          Database connection.
        start_date:  Window start 'YYYY-MM-DD' (inclusive).
        end_date:    Window end 'YYYY-MM-DD' (inclusive).
        categories:  Optional list of category names to filter.
        limit:       Max items to return.
    """
    if categories:
        placeholders = ','.join('?' * len(categories))
        rows = db.execute(
            f'''SELECT * FROM trading_intel_cache
                WHERE published_date >= ? AND published_date <= ?
                  AND published_date != ''
                  AND category IN ({placeholders})
                ORDER BY published_date DESC
                LIMIT ?''',
            [start_date, end_date] + categories + [limit]
        ).fetchall()
    else:
        rows = db.execute(
            '''SELECT * FROM trading_intel_cache
               WHERE published_date >= ? AND published_date <= ?
                 AND published_date != ''
               ORDER BY published_date DESC
               LIMIT ?''',
            (start_date, end_date, limit)
        ).fetchall()

    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════
#  Intel Coverage Dates (for backtest stepping)
# ═══════════════════════════════════════════════════════════

def get_intel_snapshot_dates(
    db: Any,
    start_date: str = '',
    end_date: str = '',
) -> list[str]:
    """Get all distinct published_dates that have intel coverage.

    Useful for backtest engines to know which dates have intel data
    available, so they can step through time meaningfully.

    Returns sorted list of 'YYYY-MM-DD' strings.
    """
    query = '''SELECT DISTINCT published_date FROM trading_intel_cache
               WHERE published_date != '' AND published_date IS NOT NULL'''
    params: list = []

    if start_date:
        query += ' AND published_date >= ?'
        params.append(start_date)
    if end_date:
        query += ' AND published_date <= ?'
        params.append(end_date)

    query += ' ORDER BY published_date ASC'
    rows = db.execute(query, params).fetchall()
    return [r['published_date'] for r in rows if r['published_date']]


# ═══════════════════════════════════════════════════════════
#  Intel → Numeric Features (for regime detection)
# ═══════════════════════════════════════════════════════════

def build_regime_intel_features(
    db: Any,
    as_of: str,
    lookback_days: int = 14,
    only_confident_dates: bool = True,
) -> dict[str, Any]:
    """Extract numeric features from intel for market regime detection.

    This converts qualitative intel into quantitative signals that
    the meta-strategy selector can use:
      - Sentiment balance (ratio of positive/negative intel)
      - Intel velocity (how fast news is flowing)
      - Category concentration (is news concentrated in one area?)
      - Keyword signal strength (policy change, risk, opportunity keywords)

    Args:
        db:             Database connection.
        as_of:          Date cutoff 'YYYY-MM-DD'.
        lookback_days:  How many days back to analyze.
        only_confident_dates: If True (default), exclude items with
                   date_source='fetched_at_fallback' to prevent temporal leakage.

    Returns:
        Dict with numeric features suitable for regime detection.
    """
    try:
        as_of_dt = datetime.strptime(as_of, '%Y-%m-%d')
    except (ValueError, TypeError) as _e:
        logger.debug('[Timeline] Invalid as_of date %r: %s', as_of, _e)
        return {'error': f'Invalid date: {as_of}'}

    window_start = (as_of_dt - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

    items = query_intel_window(db, window_start, as_of, limit=500)

    # Filter out items with guessed dates if only_confident_dates is True
    if only_confident_dates and items:
        items = [
            it for it in items
            if it.get('date_source') != 'fetched_at_fallback'
        ]

    if not items:
        return {
            'item_count': 0,
            'sentiment_score': 0.0,
            'intel_velocity': 0.0,
            'category_concentration': 0.0,
            'policy_signal': 0.0,
            'risk_signal': 0.0,
            'opportunity_signal': 0.0,
        }

    # ── Sentiment balance ──
    # Count positive/negative signal keywords in titles + summaries
    _POSITIVE_KW = frozenset([
        '利好', '上涨', '增长', '回暖', '突破', '创新高', '加仓', '净流入',
        '复苏', '扩张', '降息', '刺激', '宽松', '超预期', '涨停', '暴涨',
        '牛市', '反弹', '企稳', '新高', '加速', '景气',
    ])
    _NEGATIVE_KW = frozenset([
        '利空', '下跌', '暴跌', '风险', '衰退', '收紧', '流出', '净流出',
        '下调', '萎缩', '违约', '暴雷', '跌停', '恐慌', '危机', '制裁',
        '熊市', '崩盘', '减持', '退市', '亏损', '警告',
    ])

    pos_count = 0
    neg_count = 0
    category_counts: dict[str, int] = {}

    for it in items:
        text = f"{it.get('title', '')} {it.get('summary', '')}"
        for kw in _POSITIVE_KW:
            if kw in text:
                pos_count += 1
                break
        for kw in _NEGATIVE_KW:
            if kw in text:
                neg_count += 1
                break
        cat = it.get('category', 'unknown')
        category_counts[cat] = category_counts.get(cat, 0) + 1

    total = pos_count + neg_count
    sentiment_score = (pos_count - neg_count) / max(total, 1)  # [-1, +1]

    # ── Intel velocity (items per day) ──
    intel_velocity = len(items) / max(lookback_days, 1)

    # ── Category concentration (Herfindahl index) ──
    # High = news concentrated in few categories (unusual)
    # Low = evenly spread (normal)
    total_items = len(items)
    hhi = sum((c / total_items) ** 2 for c in category_counts.values()) if total_items > 0 else 0

    # ── Policy signal (macro_policy + policy_regulation volume) ──
    policy_count = category_counts.get('macro_policy', 0) + category_counts.get('policy_regulation', 0)
    policy_signal = policy_count / max(total_items, 1)

    # ── Risk signal (global_market events + negative sentiment) ──
    risk_count = category_counts.get('global_market', 0)
    risk_signal = (risk_count / max(total_items, 1)) + max(0, -sentiment_score)

    # ── Opportunity signal (sector_rotation + fund_flow + positive sentiment) ──
    opp_count = category_counts.get('sector_rotation', 0) + category_counts.get('fund_flow', 0)
    opportunity_signal = (opp_count / max(total_items, 1)) + max(0, sentiment_score)

    return {
        'item_count': len(items),
        'sentiment_score': round(sentiment_score, 3),
        'intel_velocity': round(intel_velocity, 2),
        'category_concentration': round(hhi, 3),
        'policy_signal': round(policy_signal, 3),
        'risk_signal': round(risk_signal, 3),
        'opportunity_signal': round(opportunity_signal, 3),
        'category_distribution': category_counts,
        'positive_count': pos_count,
        'negative_count': neg_count,
        'lookback_days': lookback_days,
        'as_of': as_of,
    }
