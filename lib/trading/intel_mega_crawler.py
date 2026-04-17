"""lib/trading/intel_mega_crawler.py — Ultra-Large Scale Intelligence Crawler.

Industrial-grade crawling engine that:
  1. Fans out to 100+ query templates across 15+ categories
  2. Runs massively concurrent (configurable worker pools)
  3. Enforces strict time-categorization (published_date is MANDATORY)
  4. Tracks progress with callback hooks for UI/SSE
  5. Supports incremental backfill across arbitrary date ranges
  6. Deduplicates across all sources via SimHash + URL

Architecture:
  MEGA_INTEL_SOURCES     — expanded source definitions (50+ queries, 15 categories)
  MegaCrawlConfig        — configuration for scale, concurrency, date range
  run_mega_crawl         — main entry point: orchestrates the full crawl
  _crawl_category_batch  — parallel crawl within a single category
  get_mega_coverage_report — coverage statistics across all categories + dates
  estimate_crawl_scope   — preview how many queries/dates need crawling

Design principle:
  Every intel item MUST have a published_date.  Items without dates are
  assigned date_source='fetched_at_fallback' and flagged for LLM/search
  date resolution.  The backtest engine ONLY uses items with confident
  dates (date_source != 'fetched_at_fallback') to prevent temporal leakage.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any

from lib.log import get_logger
from lib.trading.intel import (
    INTEL_SOURCES,
    crawl_intel_source,
    get_missing_dates,
)

logger = get_logger(__name__)

__all__ = [
    'MEGA_INTEL_SOURCES',
    'MegaCrawlConfig',
    'run_mega_crawl',
    'get_mega_coverage_report',
    'estimate_crawl_scope',
]


# ═══════════════════════════════════════════════════════════
#  Expanded Intel Source Definitions (Ultra-Large Scale)
# ═══════════════════════════════════════════════════════════

# Extends INTEL_SOURCES with many more queries per category
# + additional categories for deeper coverage.
MEGA_INTEL_SOURCES = {
    **INTEL_SOURCES,

    # ── Extend existing categories with more queries ──
    'hot_news': {
        **INTEL_SOURCES.get('hot_news', {}),
        'queries': [
            *INTEL_SOURCES.get('hot_news', {}).get('queries', []),
            '证券市场 重大新闻',
            '基金 净值 异动',
            '上市公司 财报 超预期',
            '股权 并购 重组 最新',
            '退市 风险警示 公告',
            'IPO 新股 申购',
            '大宗交易 龙虎榜 今日',
            '融资融券 余额 变化',
        ],
    },
    'macro_policy': {
        **INTEL_SOURCES.get('macro_policy', {}),
        'queries': [
            *INTEL_SOURCES.get('macro_policy', {}).get('queries', []),
            '中国 制造业PMI 最新数据',
            '社会融资 规模 信贷数据',
            '国务院 经济政策 会议',
            '财政部 税收政策 改革',
            '外汇储备 国际收支',
            'MLF LPR 操作 央行',
            '中美 贸易关系 最新',
            '欧洲 经济 影响 中国',
        ],
    },
    'market_trend': {
        **INTEL_SOURCES.get('market_trend', {}),
        'queries': [
            *INTEL_SOURCES.get('market_trend', {}).get('queries', []),
            '两市 成交额 变化 趋势',
            '涨停 跌停 家数 统计',
            '券商 策略 周报 月报',
            '股指期货 基差 升水贴水',
            '市场 情绪指标 恐慌指数',
            '新增投资者 开户数',
        ],
    },
    'sector_rotation': {
        **INTEL_SOURCES.get('sector_rotation', {}),
        'queries': [
            *INTEL_SOURCES.get('sector_rotation', {}).get('queries', []),
            'AI 人工智能 芯片 半导体 板块',
            '新能源 光伏 风电 储能 板块',
            '医药 CXO 创新药 板块',
            '消费 白酒 食品 零售 板块',
            '军工 国防 航天 板块',
            '房地产 建材 板块 政策',
            '银行 保险 券商 金融 板块',
            '数字经济 信创 网络安全',
        ],
    },
    'fund_flow': {
        **INTEL_SOURCES.get('fund_flow', {}),
        'queries': [
            *INTEL_SOURCES.get('fund_flow', {}).get('queries', []),
            '基金 持仓 季报 披露',
            '社保基金 持仓 动向',
            '保险 资金 投资 配置',
            '游资 席位 操盘 特征',
            'QFII RQFII 持仓 变化',
            'ETF 份额 变化 跟踪',
        ],
    },

    # ── NEW categories for deeper coverage ──
    'industry_research': {
        'label': '行业研报',
        'queries': [
            '券商 研报 推荐 目标价',
            '行业 深度报告 最新',
            '卖方 分析师 观点 变化',
            '投资 评级 调整 升级降级',
            '盈利预测 调整 上调下调',
        ],
        'priority': 2,
        'ttl_hours': 12,
        'decision_window_days': 14,
        'data_mode': 'trend',
    },
    'sentiment_indicator': {
        'label': '市场情绪',
        'queries': [
            '市场 恐慌 指数 VIX',
            '投资者 信心指数',
            '基金 仓位 测算',
            '两融 余额 变化趋势',
            '股票 社交媒体 讨论热度',
            '散户 情绪 调查',
        ],
        'priority': 2,
        'ttl_hours': 8,
        'decision_window_days': 14,
        'data_mode': 'trend',
    },
    'commodity_fx': {
        'label': '大宗商品外汇',
        'queries': [
            '铜价 走势 经济指标',
            '铁矿石 价格 钢铁',
            '原油 OPEC 减产增产',
            '黄金 避险 地缘政治',
            '人民币 汇率 走势 央行',
            '美元指数 DXY 走势',
            '大宗商品 通胀 预期',
        ],
        'priority': 2,
        'ttl_hours': 8,
        'decision_window_days': 14,
        'data_mode': 'trend',
    },
    'tech_innovation': {
        'label': '科技创新',
        'queries': [
            'AI 大模型 应用 商业化',
            '半导体 芯片 国产替代',
            '量子计算 进展 投资',
            '自动驾驶 智能汽车',
            '机器人 人形机器人 产业',
            '低空经济 无人机 eVTOL',
        ],
        'priority': 3,
        'ttl_hours': 12,
        'decision_window_days': 21,
        'data_mode': 'trend',
    },
    'esg_green': {
        'label': 'ESG绿色金融',
        'queries': [
            'ESG 评级 基金 投资',
            '碳交易 碳市场 价格',
            '绿色债券 发行 规模',
            '新能源 补贴 政策 变化',
        ],
        'priority': 3,
        'ttl_hours': 24,
        'decision_window_days': 30,
        'data_mode': 'trend',
    },
    'geopolitical_risk': {
        'label': '地缘政治风险',
        'queries': [
            '地缘政治 冲突 风险 升级',
            '中美 科技 制裁 实体清单',
            '台海 局势 军事 动态',
            '中东 冲突 石油 影响',
            '俄乌 局势 全球 影响',
            '供应链 脱钩 去全球化',
        ],
        'priority': 2,
        'ttl_hours': 8,
        'decision_window_days': 30,
        'data_mode': 'trend',
    },
}


# ═══════════════════════════════════════════════════════════
#  Crawl Configuration
# ═══════════════════════════════════════════════════════════

class MegaCrawlConfig:
    """Configuration for ultra-large scale crawling."""

    def __init__(
        self,
        *,
        max_workers: int = 6,
        queries_per_category: int = 0,       # 0 = all queries
        categories: list[str] | None = None,  # None = all
        backfill_days: int = 180,            # 6 months default
        start_date: str = '',
        end_date: str = '',
        rate_limit_delay: float = 0.3,       # seconds between queries
        skip_fallback_dates: bool = False,    # if True, only accept confident dates
        use_multi_source: bool = True,
        max_items_per_query: int = 20,
    ):
        self.max_workers = max_workers
        self.queries_per_category = queries_per_category
        self.categories = categories
        self.backfill_days = backfill_days
        self.rate_limit_delay = rate_limit_delay
        self.skip_fallback_dates = skip_fallback_dates
        self.use_multi_source = use_multi_source
        self.max_items_per_query = max_items_per_query

        if start_date:
            self.start_date = start_date
        else:
            self.start_date = (date.today() - timedelta(days=backfill_days)).strftime('%Y-%m-%d')

        self.end_date = end_date or date.today().strftime('%Y-%m-%d')


# ═══════════════════════════════════════════════════════════
#  Scope Estimation (preview before crawling)
# ═══════════════════════════════════════════════════════════

def estimate_crawl_scope(
    db: Any,
    config: MegaCrawlConfig | None = None,
) -> dict[str, Any]:
    """Preview how many queries/dates need crawling without actually crawling.

    Returns:
        {
            total_queries:     total query count to execute,
            total_missing_days: total category×date pairs with no coverage,
            categories:        {cat: {queries, missing_days, label}},
            estimated_time_minutes: rough estimate,
        }
    """
    config = config or MegaCrawlConfig()
    sources = _get_active_sources(config)

    total_queries = 0
    total_missing = 0
    cat_details = {}

    for cat, src in sources.items():
        queries = src['queries']
        if config.queries_per_category > 0:
            queries = queries[:config.queries_per_category]

        missing_days = 0
        for query in queries:
            source_key = hashlib.md5(query.encode()).hexdigest()[:12]
            missing = get_missing_dates(db, cat, source_key,
                                        config.start_date, config.end_date)
            missing_days += len(missing)

        total_queries += len(queries)
        total_missing += missing_days
        cat_details[cat] = {
            'label': src.get('label', cat),
            'queries': len(queries),
            'missing_days': missing_days,
        }

    # Rough estimate: ~3 seconds per query (network + processing)
    est_seconds = total_queries * 3 + total_missing * 0.5
    est_minutes = est_seconds / 60

    return {
        'total_queries': total_queries,
        'total_missing_days': total_missing,
        'categories': cat_details,
        'estimated_time_minutes': round(est_minutes, 1),
        'config': {
            'start_date': config.start_date,
            'end_date': config.end_date,
            'max_workers': config.max_workers,
        },
    }


# ═══════════════════════════════════════════════════════════
#  Main Crawl Engine
# ═══════════════════════════════════════════════════════════

def run_mega_crawl(
    db: Any,
    search_fn: Callable,
    config: MegaCrawlConfig | None = None,
    analyze_fn: Callable | None = None,
    progress_callback: Callable | None = None,
) -> dict[str, Any]:
    """Execute ultra-large scale intelligence crawl.

    Orchestrates concurrent crawling across all categories and queries,
    with progress tracking and time-categorized storage.

    Args:
        db:                Database connection.
        search_fn:         Web search function (for DDG fallback).
        config:            MegaCrawlConfig (defaults to full crawl).
        analyze_fn:        Optional AI analysis function per item.
        progress_callback: Optional fn(done, total, category, message)
                           called after each query completes.

    Returns:
        {
            status:           'completed' | 'partial' | 'error',
            total_fetched:    total new items,
            total_queries:    queries executed,
            categories:       {cat: {fetched, queries, errors}},
            duration_seconds: wall-clock time,
            date_confidence:  {confident, fallback, pct_confident},
        }
    """
    config = config or MegaCrawlConfig()
    sources = _get_active_sources(config)
    start_time = time.time()

    total_fetched = 0
    total_queries = 0
    total_errors = 0
    cat_results: dict[str, dict[str, Any]] = {}

    # Count total work for progress
    total_work = sum(
        len(src['queries'][:config.queries_per_category] if config.queries_per_category > 0
            else src['queries'])
        for src in sources.values()
    )
    done_count = 0

    for cat, src in sorted(sources.items(), key=lambda x: x[1].get('priority', 5)):
        queries = src['queries']
        if config.queries_per_category > 0:
            queries = queries[:config.queries_per_category]

        cat_fetched = 0
        cat_errors = 0

        # ── Batch crawl queries in parallel within category ──
        query_results = _crawl_category_batch(
            db, cat, queries, search_fn, analyze_fn, config,
        )

        for qr in query_results:
            done_count += 1
            total_queries += 1

            if qr.get('error'):
                cat_errors += 1
                total_errors += 1
            else:
                cat_fetched += qr.get('fetched', 0)
                total_fetched += qr.get('fetched', 0)

            if progress_callback:
                try:
                    progress_callback(
                        done_count, total_work, cat,
                        f"{src.get('label', cat)}: {qr.get('query', '')[:40]}... "
                        f"({qr.get('fetched', 0)} items)"
                    )
                except Exception as _cb_err:
                    logger.debug('[MegaCrawl] Progress callback failed: %s', _cb_err)

        cat_results[cat] = {
            'label': src.get('label', cat),
            'fetched': cat_fetched,
            'queries': len(queries),
            'errors': cat_errors,
        }

        if cat_fetched > 0:
            logger.info(
                '[MegaCrawl] ✅ %s: %d items from %d queries (%d errors)',
                src.get('label', cat), cat_fetched, len(queries), cat_errors,
            )

    # ── Date confidence statistics ──
    date_stats = _compute_date_confidence(db, config.start_date, config.end_date)

    duration = time.time() - start_time
    status = 'completed' if total_errors == 0 else 'partial'

    logger.info(
        '[MegaCrawl] %s: %d items from %d queries in %.1fs '
        '(%d errors, %.1f%% confident dates)',
        status, total_fetched, total_queries, duration,
        total_errors, date_stats.get('pct_confident', 0),
    )

    return {
        'status': status,
        'total_fetched': total_fetched,
        'total_queries': total_queries,
        'total_errors': total_errors,
        'categories': cat_results,
        'duration_seconds': round(duration, 1),
        'date_confidence': date_stats,
    }


# ═══════════════════════════════════════════════════════════
#  Category Batch Crawler (parallel within category)
# ═══════════════════════════════════════════════════════════

def _crawl_category_batch(
    db: Any,
    category: str,
    queries: list[str],
    search_fn: Callable,
    analyze_fn: Callable | None,
    config: MegaCrawlConfig,
) -> list[dict[str, Any]]:
    """Crawl multiple queries within a category using a thread pool.

    Returns list of {query, fetched, error?} dicts.

    IMPORTANT: Each worker thread gets its own thread-local DB connection
    via get_thread_db().  DB connections are NOT thread-safe — sharing
    the caller's ``db`` across ThreadPoolExecutor workers causes
    ``InterfaceError: connection already closed``.
    """
    from lib.database import get_thread_db

    results = []

    def _do_one(query: str) -> dict[str, Any]:
        # Each thread must use its own connection — never share `db` across threads
        thread_db = get_thread_db('trading')
        try:
            n = crawl_intel_source(
                thread_db, category, query, search_fn, analyze_fn,
                use_multi_source=config.use_multi_source,
            )
            return {'query': query, 'fetched': n}
        except Exception as e:
            logger.warning(
                '[MegaCrawl] Query error in %s: %s — %s',
                category, query[:40], e, exc_info=True,
            )
            return {'query': query, 'fetched': 0, 'error': str(e)}

    # Use thread pool for concurrent queries within category
    # (each query fans out to multiple sources internally)
    max_w = min(config.max_workers, len(queries))
    if max_w <= 1:
        # Single-threaded: safe to use caller's db directly
        for q in queries:
            results.append(_do_one(q))
            time.sleep(config.rate_limit_delay)
    else:
        with ThreadPoolExecutor(max_workers=max_w) as pool:
            futures = {pool.submit(_do_one, q): q for q in queries}
            try:
                for fut in as_completed(futures, timeout=120):
                    try:
                        results.append(fut.result())
                    except Exception as e:
                        q = futures[fut]
                        logger.warning('[MegaCrawl] Future failed for query %s: %s', q, e)
                        results.append({'query': q, 'fetched': 0, 'error': str(e)})
                    time.sleep(config.rate_limit_delay)
            except TimeoutError:
                timed_out = [futures[f] for f in futures if not f.done()]
                logger.warning('[MegaCrawl] %d/%d queries timed out in %s: %s',
                               len(timed_out), len(queries), category, timed_out[:3])
                for q in timed_out:
                    results.append({'query': q, 'fetched': 0, 'error': 'timeout'})

    return results


# ═══════════════════════════════════════════════════════════
#  Coverage Report
# ═══════════════════════════════════════════════════════════

def get_mega_coverage_report(
    db: Any,
    config: MegaCrawlConfig | None = None,
) -> dict[str, Any]:
    """Comprehensive coverage report across all mega sources.

    Returns per-category and per-date coverage statistics.
    """
    config = config or MegaCrawlConfig()
    sources = _get_active_sources(config)
    datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    report = {
        'range': {'start': config.start_date, 'end': config.end_date},
        'categories': {},
        'total_items': 0,
        'date_distribution': {},
    }

    for cat, src in sources.items():
        items = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_intel_cache WHERE category=?',
            (cat,),
        ).fetchone()

        confident = db.execute(
            "SELECT COUNT(*) as cnt FROM trading_intel_cache "
            "WHERE category=? AND date_source != 'fetched_at_fallback' "
            "AND date_source != '' AND published_date != ''",
            (cat,),
        ).fetchone()

        report['categories'][cat] = {
            'label': src.get('label', cat),
            'total': items['cnt'],
            'confident_dates': confident['cnt'],
            'priority': src.get('priority', 5),
        }
        report['total_items'] += items['cnt']

    # Date distribution (how many items per date)
    date_rows = db.execute(
        "SELECT published_date, COUNT(*) as cnt FROM trading_intel_cache "
        "WHERE published_date != '' AND published_date >= ? AND published_date <= ? "
        "GROUP BY published_date ORDER BY published_date ASC",
        (config.start_date, config.end_date),
    ).fetchall()

    for r in date_rows:
        report['date_distribution'][r['published_date']] = r['cnt']

    return report


# ═══════════════════════════════════════════════════════════
#  Private Helpers
# ═══════════════════════════════════════════════════════════

def _get_active_sources(config: MegaCrawlConfig) -> dict[str, Any]:
    """Get the active sources based on config filters."""
    sources = MEGA_INTEL_SOURCES
    if config.categories:
        sources = {k: v for k, v in sources.items() if k in config.categories}
    return sources


def _compute_date_confidence(
    db: Any,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Compute date assignment confidence statistics."""
    total = db.execute(
        "SELECT COUNT(*) as cnt FROM trading_intel_cache "
        "WHERE published_date >= ? AND published_date <= ?",
        (start_date, end_date),
    ).fetchone()

    confident = db.execute(
        "SELECT COUNT(*) as cnt FROM trading_intel_cache "
        "WHERE published_date >= ? AND published_date <= ? "
        "AND date_source != 'fetched_at_fallback' "
        "AND date_source != '' AND published_date != ''",
        (start_date, end_date),
    ).fetchone()

    fallback = db.execute(
        "SELECT COUNT(*) as cnt FROM trading_intel_cache "
        "WHERE published_date >= ? AND published_date <= ? "
        "AND date_source = 'fetched_at_fallback'",
        (start_date, end_date),
    ).fetchone()

    total_cnt = total['cnt'] if total else 0
    confident_cnt = confident['cnt'] if confident else 0
    fallback_cnt = fallback['cnt'] if fallback else 0

    return {
        'total': total_cnt,
        'confident': confident_cnt,
        'fallback': fallback_cnt,
        'pct_confident': round(confident_cnt / max(total_cnt, 1) * 100, 1),
    }
