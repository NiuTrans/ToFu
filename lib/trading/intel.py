"""lib/trading/intel.py — Intelligence crawler, backfill engine, and intel context building."""

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

from lib.log import get_logger

if TYPE_CHECKING:
    from lib.protocols import LLMService

logger = get_logger(__name__)

__all__ = [
    'INTEL_SOURCES',
    'crawl_intel_source',
    'cleanup_stale_intel',
    'build_intel_context',
    'run_backfill',
    'get_intel_coverage_report',
    'compute_content_fingerprint',
]


# ═══════════════════════════════════════════════════════════
#  Intel Source Definitions
# ═══════════════════════════════════════════════════════════

# Source definitions for comprehensive coverage
# data_mode: 'snapshot' = new replaces old (e.g. ratings, rates)
#            'trend'    = historical data has value (e.g. policy, market moves)
# ttl_hours      → how often to re-crawl (freshness for the *crawler*)
# decision_window → how far back the AI can see when making decisions
#                   This is deliberately MUCH longer than ttl_hours so that
#                   the AI can spot weekly/monthly trends and policy shifts.
# data_mode       → 'snapshot' (new replaces old) vs 'trend' (history valuable)
INTEL_SOURCES = {
    'hot_news': {
        'label': '热点新闻',
        'queries': [
            '今日财经要闻 ETF股市',
            '财经新闻 重大事件 今日',
            '投资市场 最新消息',
            '股市热点 投资者关注',
            'A股新闻 重要公告 今日',
        ],
        'priority': 1,
        'ttl_hours': 4,
        'decision_window_days': 7,
        'data_mode': 'trend',
    },
    'macro_policy': {
        'label': '宏观政策',
        'queries': [
            '中国央行货币政策 最新',
            '财政政策 经济刺激 最新',
            '美联储利率决议 影响',
            'CPI PPI 经济数据 中国',
            '中国GDP增速 经济形势',
        ],
        'priority': 1,
        'ttl_hours': 12,
        'decision_window_days': 30,   # 政策趋势需要月级视野
        'data_mode': 'trend',
    },
    'market_trend': {
        'label': '市场趋势',
        'queries': [
            'A股大盘走势 分析',
            '上证指数 深证成指 行情',
            '港股 恒生指数 走势',
            '美股 纳斯达克 标普500',
            '北向资金 流入流出',
        ],
        'priority': 1,
        'ttl_hours': 6,
        'decision_window_days': 14,   # 看两周趋势
        'data_mode': 'trend',
    },
    'sector_rotation': {
        'label': '板块轮动',
        'queries': [
            '热门板块 资金流向 今日',
            '行业板块 涨幅排名',
            '科技 新能源 医药 消费 板块',
            '板块轮动 趋势分析',
        ],
        'priority': 2,
        'ttl_hours': 8,
        'decision_window_days': 21,   # 轮动周期约2-3周
        'data_mode': 'trend',
    },
    'fund_flow': {
        'label': '资金流向',
        'queries': [
            '公募 发行规模 最新',
            'ETF 净申购 赎回 数据',
            'ETF 资金流入 排行',
            '机构资金 调仓动向',
        ],
        'priority': 2,
        'ttl_hours': 12,
        'decision_window_days': 30,   # 资金流月级趋势
        'data_mode': 'trend',
    },
    'policy_regulation': {
        'label': '监管政策',
        'queries': [
            '证监会 最新政策 证券',
            '证券监管 法规变化',
            '公募 费率改革',
        ],
        'priority': 3,
        'ttl_hours': 24,
        'decision_window_days': 60,   # 监管政策慢周期，看2个月
        'data_mode': 'trend',
    },
    'global_market': {
        'label': '全球市场',
        'queries': [
            '全球股市 行情 综述',
            '大宗商品 黄金 原油 价格',
            '汇率 人民币 美元',
            '地缘政治 风险 市场影响',
        ],
        'priority': 2,
        'ttl_hours': 8,
        'decision_window_days': 14,   # 两周全球市场视野
        'data_mode': 'trend',
    },
    'bond_rate': {
        'label': '债券利率',
        'queries': [
            '国债收益率 最新',
            '债券市场 利率走势',
            'Shibor LPR 利率',
        ],
        'priority': 3,
        'ttl_hours': 12,
        'decision_window_days': 7,    # 快照型，但保留一周看利率趋势
        'data_mode': 'snapshot',
    },
    'fund_rating': {
        'label': '资产评级',
        'queries': [
            '资产评级 晨星 天天基金网',
            '标的排名 业绩 季度',
            '明星投资经理 调仓',
        ],
        'priority': 3,
        'ttl_hours': 48,
        'decision_window_days': 7,    # 快照型，最近一周的评级
        'data_mode': 'snapshot',
    },
}


# ═══════════════════════════════════════════════════════════
#  Intel Context Building
# ═══════════════════════════════════════════════════════════

def build_intel_context(db, max_items=60):  # noqa: ARG001 — max_items reserved for future per-category capping
    """Build a structured, time-layered intel context for AI decision-making.

    Instead of a flat list filtered by expires_at, this organizes intel by
    category and time period so the AI can reason about trends.

    Args:
        db: Database connection.
        max_items: Reserved — per-category limits are currently hardcoded in INTEL_SOURCES.

    Returns (intel_text, item_count) where intel_text is Markdown-formatted.
    """
    now = datetime.now()
    sections = []       # [(priority, label, lines)]
    total_items = 0

    for cat, src in sorted(INTEL_SOURCES.items(), key=lambda x: x[1]['priority']):
        window = src.get('decision_window_days', 7)
        cutoff = (now - timedelta(days=window)).strftime('%Y-%m-%d %H:%M:%S')
        rows = db.execute(
            '''SELECT title, summary, analysis, relevance_score, fetched_at, category
               FROM trading_intel_cache
               WHERE category = ? AND fetched_at >= ?
               ORDER BY fetched_at DESC''',
            (cat, cutoff)
        ).fetchall()
        if not rows:
            continue

        items = [dict(r) for r in rows]
        total_items += len(items)

        # Split into time buckets for trend awareness
        recent = []     # last 3 days
        earlier = []    # 3 days ~ window
        t_3d = (now - timedelta(days=3)).strftime('%Y-%m-%d %H:%M:%S')
        for it in items:
            bucket = recent if it['fetched_at'] >= t_3d else earlier
            bucket.append(it)

        lines = [f"### {src['label']}（近{window}天，共{len(items)}条）"]

        if recent:
            lines.append(f"**近3天（{len(recent)}条）：**")
            for it in recent[:10]:   # cap per bucket
                score = it.get('relevance_score', 0)
                marker = '🔴' if score >= 0.8 else '🟡' if score >= 0.5 else '⚪'
                lines.append(f"- {marker} {it['title']}: {it['summary'][:120]}")

        if earlier:
            lines.append(f"**更早（{len(earlier)}条）：**")
            for it in earlier[:8]:
                lines.append(f"- {it['title']}: {it['summary'][:100]}")

        sections.append((src['priority'], '\n'.join(lines)))

    if not sections:
        return '', 0

    header = f"## 情报中心（覆盖{len(sections)}个分类，共{total_items}条）\n"
    header += "_情报按时间分层展示：近3天为重点，更早条目供趋势参考。_\n"
    body = '\n\n'.join(text for _, text in sections)
    return header + '\n' + body, total_items


# ═══════════════════════════════════════════════════════════
#  Crawl Coverage & Deduplication
# ═══════════════════════════════════════════════════════════

def get_backfill_date_range():
    """Calculate date range: from 3 months ago to today."""
    today = date.today()
    three_months_ago = today - timedelta(days=90)
    return three_months_ago.strftime('%Y-%m-%d'), today.strftime('%Y-%m-%d')


def check_crawl_coverage(db, category, source_key):
    """Check which dates are already covered for a given source.
    Returns set of dates that have already been crawled."""
    rows = db.execute(
        'SELECT crawl_date FROM trading_intel_crawl_log WHERE category=? AND source_key=? AND status=?',
        (category, source_key, 'ok')
    ).fetchall()
    return {r['crawl_date'] for r in rows}


def get_missing_dates(db, category, source_key, start_date, end_date):
    """Find dates that haven't been crawled yet within the range."""
    covered = check_crawl_coverage(db, category, source_key)
    current = datetime.strptime(start_date, '%Y-%m-%d').date()
    end = datetime.strptime(end_date, '%Y-%m-%d').date()
    missing = []
    while current <= end:
        date_str = current.strftime('%Y-%m-%d')
        if date_str not in covered:
            missing.append(date_str)
        current += timedelta(days=1)
    return missing


def record_crawl(db, crawl_date, category, source_key, items_fetched, status='ok'):
    """Record that a crawl was performed for deduplication."""
    from lib.database import db_execute_with_retry
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        db_execute_with_retry(
            db,
            '''INSERT OR REPLACE INTO trading_intel_crawl_log
               (crawl_date, category, source_key, items_fetched, status, started_at, finished_at)
               VALUES (?,?,?,?,?,?,?)''',
            (crawl_date, category, source_key, items_fetched, status, now, now),
        )
    except Exception as e:
        # ON CONFLICT upsert can fail with UniqueViolation under certain
        # PG transaction states.  Fall back to a plain UPDATE.
        logger.warning('record_crawl INSERT failed (%s), trying UPDATE fallback', e)
        try:
            db.rollback()   # clear aborted transaction state before UPDATE
            db_execute_with_retry(
                db,
                '''UPDATE trading_intel_crawl_log
                   SET items_fetched=?, status=?, finished_at=?
                   WHERE crawl_date=? AND category=? AND source_key=?''',
                (items_fetched, status, now, crawl_date, category, source_key),
            )
        except Exception as e2:
            logger.error('record_crawl UPDATE fallback also failed: %s', e2, exc_info=True)


def _strip_source_attribution(title: str) -> str:
    """Strip common source attribution suffixes/prefixes from titles.

    Many outlets append " - 新浪财经", " | 东方财富", "【快讯】" etc.
    Stripping these before fingerprinting ensures the same article
    syndicated across outlets produces the same SimHash.
    """
    if not title:
        return title
    # Strip trailing " - Source" / " | Source" / " _ Source"
    title = re.sub(r'\s*[-–—|_]\s*[\w\u4e00-\u9fff]{2,8}$', '', title)
    # Strip common prefixes: 【快讯】/ 快讯：/ 重磅：/ [快讯] etc.
    title = re.sub(r'^[\[【〖]?(?:快讯|重磅|突发|独家|最新|头条|热点)[\]】〗]?\s*[：:]\s*', '', title)
    return title.strip()


def compute_content_fingerprint(title, snippet=''):
    """Compute a 64-bit SimHash fingerprint for dedup.

    Pre-processing:
    1. Strip source attribution (- 新浪财经, 【快讯】 etc.)
    2. Weight title 3× so title similarity dominates the fingerprint

    Near-duplicate articles (same event, syndicated across outlets)
    will have fingerprints within Hamming distance ≤ 6.
    """
    from lib.trading.simhash import compute_simhash
    clean_title = _strip_source_attribution(title)
    # Weight title 3× — it's the most reliable dedup signal
    text = f"{clean_title} {clean_title} {clean_title} {snippet}".strip()
    return compute_simhash(text)


def deduplicate_intel(db, title, source_url, category, snippet=''):
    """Check if we already have this intel item.

    v7: Three-layer dedup — URL → SimHash fingerprint → title prefix.

    Layer 1: URL dedup (cross-category, exact + normalized).
    Layer 2: SimHash content fingerprint — detects semantically similar
             articles even when reworded by different outlets.  Compares
             the candidate's 64-bit SimHash against all recent items;
             Hamming distance ≤ 3 = near-duplicate.
    Layer 3: Title prefix fallback (within same category).
    """
    from lib.trading.simhash import hamming_distance

    # ── Layer 1: URL dedup — cross-category ──
    if source_url:
        url_clean = source_url.lower().rstrip('/').replace('https://', '').replace('http://', '')
        existing = db.execute(
            'SELECT id FROM trading_intel_cache WHERE source_url=?',
            (source_url,)
        ).fetchone()
        if existing:
            return True
        existing = db.execute(
            "SELECT id FROM trading_intel_cache WHERE REPLACE(REPLACE(LOWER(source_url), 'https://', ''), 'http://', '') = ?",
            (url_clean,)
        ).fetchone()
        if existing:
            return True

    # ── Layer 2: SimHash content fingerprint — the interesting part ──
    # SimHash is a locality-sensitive hash: similar texts → similar hashes.
    # We compute a 64-bit fingerprint from character 3-grams of title+snippet,
    # then count differing bits (Hamming distance) against recent DB entries.
    #
    # Calibrated thresholds (from real Chinese financial news tests):
    #   dist=0   → identical text (whitespace/formatting only)
    #   dist≤5   → syndicated copy (same article + source attribution changes)
    #   dist≤6   → near-duplicate (minor edits, prefix/suffix additions)
    #   dist≤15  → same-topic rewrites (different journalist, same event)
    #   dist≥25  → completely different articles
    #
    # We use threshold=6 to catch syndicated copies without false-positiving
    # on legitimately different coverage of the same event.
    _SIMHASH_THRESHOLD = 6

    candidate_hash = compute_content_fingerprint(title, snippet)
    if candidate_hash != 0:
        # Only compare against items from the last 7 days (performance + relevance)
        cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
        recent_hashes = db.execute(
            'SELECT id, content_simhash FROM trading_intel_cache '
            'WHERE content_simhash != 0 AND fetched_at >= ?',
            (cutoff,)
        ).fetchall()
        for row in recent_hashes:
            existing_hash = row['content_simhash']
            # Convert signed (from DB) back to unsigned for Hamming distance
            from lib.trading.simhash import to_unsigned64
            existing_hash = to_unsigned64(existing_hash)
            dist = hamming_distance(candidate_hash, existing_hash)
            if dist <= _SIMHASH_THRESHOLD:
                logger.debug('🔍 SimHash near-dup: "%s" ↔ existing id=%d (dist=%d/64 bits)',
                             title[:50], row['id'], dist)
                return True

    # ── Layer 3: Title prefix fallback ──
    if title:
        existing = db.execute(
            "SELECT id FROM trading_intel_cache WHERE category=? AND title=?",
            (category, title)
        ).fetchone()
        if existing:
            return True
        title_core = re.sub(r'\s*[-–—|_]\s*[\w\u4e00-\u9fff]+$', '', title).strip()
        if title_core and len(title_core) >= 10:
            existing = db.execute(
                "SELECT id FROM trading_intel_cache WHERE category=? AND title LIKE ?",
                (category, f'{title_core}%')
            ).fetchone()
            if existing:
                return True

    return False


def _purge_expired_snapshot(db, category):
    """For 'snapshot' categories, delete expired records to avoid stale/contradictory data.
    Keeps only fresh (non-expired) items. Called after each crawl for snapshot sources."""
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        cur = db.execute(
            'DELETE FROM trading_intel_cache WHERE category=? AND expires_at < ? AND expires_at != ?',
            (category, now_str, '')
        )
        deleted = cur.rowcount
        if deleted:
            db.commit()
            logger.info('🗑️  Purged %d expired snapshot items from [%s]', deleted, category)
        return deleted
    except Exception as e:
        logger.error('Snapshot purge error for %s: %s', category, e, exc_info=True)
        try:
            db.rollback()   # clear aborted transaction state
        except Exception as _rb_err:
            logger.debug('[Intel] Rollback after purge error failed: %s', _rb_err)
        return 0


def cleanup_stale_intel(db):
    """Periodic housekeeping: remove very old intel records.

    Retention = decision_window_days × 1.5 (safety buffer so we never
    delete data that the AI decision window might still reference).

    Call from scheduler or background thread (e.g. daily).
    Returns total number of deleted rows.
    """
    now = datetime.now()
    total_deleted = 0
    for cat, src in INTEL_SOURCES.items():
        window = src.get('decision_window_days', 7)
        max_days = int(window * 1.5)          # keep 1.5× the decision window
        max_days = max(max_days, 7)           # minimum 7 days retention
        cutoff = (now - timedelta(days=max_days)).strftime('%Y-%m-%d %H:%M:%S')
        try:
            cur = db.execute(
                'DELETE FROM trading_intel_cache WHERE category=? AND fetched_at < ? AND fetched_at != ?',
                (cat, cutoff, '')
            )
            deleted = cur.rowcount
            if deleted:
                total_deleted += deleted
                logger.info('🧹 Housekeeping: removed %d old [%s] records (>%dd)', deleted, cat, max_days)
        except Exception as e:
            logger.error('Housekeeping error for %s: %s', cat, e, exc_info=True)
            try:
                db.rollback()   # clear aborted transaction state
            except Exception as _rb_err:
                logger.debug('[Intel] Rollback after housekeeping error failed: %s', _rb_err)
    if total_deleted:
        db.commit()
        logger.info('🧹 Total housekeeping: %d stale records removed', total_deleted)
    return total_deleted


# ═══════════════════════════════════════════════════════════
#  Publish Date Extraction
# ═══════════════════════════════════════════════════════════

def _extract_publish_date(title, snippet, url=''):
    """Try to extract actual publication date from content/title/URL.

    Returns (date_str, source) where date_str is 'YYYY-MM-DD' (day-level only)
    and source is one of 'regex', 'url_path', '' (not found).
    """
    text = f"{title} {snippet}"
    now = datetime.now()

    def _fmt(dt):
        return dt.strftime('%Y-%m-%d')

    # ── Pattern 0: Chinese relative time — "X分钟前", "X小时前", "X天前", "昨天", "前天" ──
    m = re.search(r'(\d+)\s*分钟前', text)
    if m:
        return _fmt(now - timedelta(minutes=int(m.group(1)))), 'regex'
    m = re.search(r'(\d+)\s*小时前', text)
    if m:
        return _fmt(now - timedelta(hours=int(m.group(1)))), 'regex'
    m = re.search(r'(\d+)\s*天前', text)
    if m:
        return _fmt(now - timedelta(days=int(m.group(1)))), 'regex'
    if '前天' in text:
        return _fmt(now - timedelta(days=2)), 'regex'
    if '昨天' in text:
        return _fmt(now - timedelta(days=1)), 'regex'

    # ── Pattern 0b: Chinese date — "2024年3月10日", "3月10日" ──
    m = re.search(r'(20[12]\d)\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if dt <= now:
                return _fmt(dt), 'regex'
        except ValueError:
            logger.debug('[Trading] Chinese date parse failed for YYYY年M月D日 match: %s', m.group(0) if m else '?', exc_info=True)
    m = re.search(r'(\d{1,2})\s*月\s*(\d{1,2})\s*日', text)
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
            if dt > now:
                dt = dt.replace(year=now.year - 1)
            return _fmt(dt), 'regex'
        except ValueError:
            logger.debug('[Trading] Chinese date parse failed for M月D日 match', exc_info=True)
    # Pattern 1: 2024-03-10, 2024/03/10, 2024.03.10  in text
    m = re.search(r'(20[12]\d)[/\-\.](0[1-9]|1[0-2])[/\-\.]([0-2]\d|3[01])', text)
    if m:
        try:
            dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if dt <= datetime.now():
                return _fmt(dt), 'regex'
        except ValueError:
            logger.debug('[Trading] ISO-style date parse failed for match: %s', m.group(0) if m else '?', exc_info=True)
    if '今天' in text or '刚刚' in text:
        return _fmt(now), 'regex'
    # Pattern 4: "Mar 10, 2024" style
    m = re.search(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(20[12]\d)', text, re.I)
    if m:
        from calendar import month_abbr
        months = {v.lower(): k for k, v in enumerate(month_abbr) if v}
        mon = months.get(m.group(1)[:3].lower())
        if mon:
            try:
                dt = datetime(int(m.group(3)), mon, int(m.group(2)))
                if dt <= datetime.now():
                    return _fmt(dt), 'regex'
            except ValueError:
                logger.debug('[Trading] date parse failed for DD-MON-YYYY pattern match', exc_info=True)

    # ── Pattern 5: date embedded in URL path ──
    # Matches: /2024/03/10/, /2024-03-10/, /20240310, etc.
    if url:
        m = re.search(r'/(20[12]\d)[/\-](0[1-9]|1[0-2])[/\-]([0-2]\d|3[01])(?:[/\.\-]|\b)', url)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if dt <= now:
                    return _fmt(dt), 'url_path'
            except ValueError:
                logger.debug('Invalid date in URL path: %s', url, exc_info=True)
        m = re.search(r'/(20[12]\d)(0[1-9]|1[0-2])([0-2]\d|3[01])(?:\D|$)', url)
        if m:
            try:
                dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                if dt <= now:
                    return _fmt(dt), 'url_path'
            except ValueError:
                logger.debug('Invalid compact date in URL path: %s', url, exc_info=True)

    return '', ''


def _llm_fill_publish_dates(db, items, *, llm: 'LLMService | None' = None):
    """Use a cheap LLM to extract publication dates for items missing them.

    Sends a batch prompt with titles + rich_content (full article text from DB)
    + URL, asks for JSON with YYYY-MM-DD dates.  The rich_content is fetched
    from trading_intel_cache.raw_content, which was populated by
    fetch_page_content() + filter_web_content() during crawl — giving the LLM
    far more date clues than the original snippet[:200].

    Updates published_date and date_source='llm' in the database.
    Uses Gemini Flash Lite (very cheap) or Qwen.

    Args:
        db:    Database connection.
        items: List of intel item dicts with 'title', 'snippet', 'url' keys.
        llm:   Optional :class:`~lib.protocols.LLMService` for LLM calls.
               Defaults to ``lib.llm_dispatch.smart_chat`` (production).
               Pass a mock/stub for testing.
    """
    if not items:
        return
    if llm is not None:
        _chat_fn = llm.chat
    else:
        from lib.llm_dispatch import smart_chat
        _chat_fn = smart_chat

    # Build batch prompt — up to 10 items per call
    batch = items[:10]
    content = None  # initialise before try to avoid NameError on exception paths
    lines = []
    for i, item in enumerate(batch):
        # Fetch rich_content from DB (populated by fetch_page_content during crawl)
        # This gives the LLM full article text instead of just a 200-char snippet.
        rich_text = item.get('snippet', '')[:200]  # fallback
        if item.get('url'):
            row = db.execute(
                'SELECT raw_content FROM trading_intel_cache WHERE source_url = ?',
                (item['url'],)
            ).fetchone()
            if row and row['raw_content']:
                # Use first 800 chars of rich content — enough for date clues
                # but keeps the batch prompt within reasonable token limits
                rich_text = row['raw_content'][:800]

        lines.append(f"{i+1}. Title: {item['title']}")
        lines.append(f"   Content: {rich_text}")
        lines.append(f"   URL: {item['url']}")

    prompt = f"""从以下新闻条目中提取每条的实际发布日期（不是抓取时间）。
每条已提供标题、文章正文内容（最多800字）和URL，请综合利用：
- 正文中出现的日期（如"2026年3月23日发布"、"03-23 14:30"）
- 正文中的相对时间（如"X天前"、"昨天"、"上周"）→ 根据今天日期推算
- URL路径中的日期模式（如 /2026/03/23/）
只需精确到天，返回 YYYY-MM-DD 格式。
如果完全无法确定，返回 "unknown"。

今天是 {datetime.now().strftime('%Y-%m-%d')}。

{chr(10).join(lines)}

请只返回JSON数组，每个元素对应一条:
[{{"index": 1, "date": "YYYY-MM-DD"}}, ...]"""

    try:
        content, _ = _chat_fn(
            messages=[{'role': 'user', 'content': prompt}],
            max_tokens=512, temperature=0,
            capability='cheap',
            log_prefix='[Intel-Date]',
        )
        if not content:
            return

        # Parse JSON from response — remove markdown code fence if any
        content = content.strip()
        if content.startswith('```'):
            content = re.sub(r'^```\w*\n?', '', content)
            content = re.sub(r'\n?```$', '', content)

        # Robust JSON extraction — handle truncated/malformed responses
        try:
            dates = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            # Try to extract individual entries via regex fallback
            dates = []
            for m in re.finditer(r'\{\s*"index"\s*:\s*(\d+)\s*,\s*"date"\s*:\s*"(\d{4}-\d{2}-\d{2}|unknown)"', content):
                dates.append({'index': int(m.group(1)), 'date': m.group(2)})
            if not dates:
                logger.warning('[Intel] LLM date backfill: no parseable entries from response (len=%d)', len(content))
                return
            logger.debug('[Intel] LLM date backfill: regex fallback extracted %d entries', len(dates))

        updated = 0
        for entry in dates:
            idx = entry.get('index', 0) - 1
            date_str = entry.get('date', 'unknown')
            if 0 <= idx < len(batch) and date_str != 'unknown' and re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
                url = batch[idx]['url']
                db.execute(
                    """UPDATE trading_intel_cache
                       SET published_at = ?, published_date = ?, date_source = 'llm'
                       WHERE source_url = ?
                         AND (published_date = '' OR published_date IS NULL
                              OR date_source = 'fetched_at_fallback')""",
                    (f"{date_str} 00:00:00", date_str, url)
                )
                updated += 1
        if updated:
            db.commit()
            logger.info('🤖 LLM extracted dates for %d/%d items', updated, len(batch))

    except (KeyError, TypeError) as e:
        logger.warning('LLM date extraction parse error: %s', e, exc_info=True)
    except Exception as e:
        logger.error('LLM date extraction error: %s', e, exc_info=True)


def _web_search_fill_dates(db, items, search_fn, *, llm=None):
    """Layer 4: Use web search to determine approximate publication dates.

    For items where title/URL/meta/LLM couldn't determine a date, we search
    for the item title to find when it was published. This catches cases like:
    - Data portal pages with no date markup
    - News aggregation pages where the original date is buried
    - Content that was re-published / syndicated

    Strategy: search for the exact title, look at the search result dates/snippets
    to infer when the original article was published.
    """
    if not items:
        return

    # Process items that still have no date or only a fallback date after LLM pass
    still_undated = []
    for item in items:
        url = item.get('url', '')
        row = db.execute(
            "SELECT published_date, date_source FROM trading_intel_cache WHERE source_url=?",
            (url,)
        ).fetchone()
        if row and (not row['published_date'] or row['published_date'] == ''
                    or row['date_source'] == 'fetched_at_fallback'):
            still_undated.append(item)

    if not still_undated:
        return

    logger.info('[Intel] Layer 4: web search date verification for %d undated items', len(still_undated))

    updated = 0
    for item in still_undated[:5]:  # limit to 5 searches to avoid rate limits
        title = item['title'][:60]
        try:
            search_results = search_fn(f'"{title}"', max_results=3)
            if not search_results:
                continue

            # Analyze search results for date clues
            for sr in search_results:
                sr_title = sr.get('title', '')
                sr_snippet = sr.get('snippet', sr.get('body', ''))
                sr_url = sr.get('url', '')

                # Try to extract date from the search result
                pd, ds = _extract_publish_date(sr_title, sr_snippet, sr_url)
                if pd:
                    url = item.get('url', '')
                    db.execute(
                        """UPDATE trading_intel_cache
                           SET published_at = ?, published_date = ?, date_source = 'search_verify'
                           WHERE source_url = ?
                             AND (published_date = '' OR published_date IS NULL
                                  OR date_source = 'fetched_at_fallback')""",
                        (f"{pd} 00:00:00", pd, url)
                    )
                    updated += 1
                    logger.debug('🔍 Web search date for "%s": %s', title[:40], pd)
                    break

            time.sleep(0.3)  # Rate limiting between searches
        except Exception as e:
            logger.debug('[Intel] Web search date check failed for "%s": %s',
                         title[:40], e, exc_info=True)

    if updated:
        db.commit()
        logger.info('🔍 Web search determined dates for %d/%d items', updated, len(still_undated))


def _clean_intel_text(s):
    """Clean intel text: strip HTML, control chars, zero-width chars, normalize whitespace."""
    if not s:
        return ''
    import unicodedata
    # Strip HTML tags
    s = re.sub(r'<[^>]+>', ' ', s)
    # Decode HTML entities
    from html import unescape
    s = unescape(s)
    # Remove zero-width characters (U+200B, U+200C, U+200D, U+FEFF, etc.)
    s = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060\ufeff\ufff9-\ufffc]', '', s)
    # Remove other control characters (keep newlines/tabs for now)
    s = ''.join(c for c in s if c in '\n\t' or not unicodedata.category(c).startswith('C'))
    # Normalize unicode (NFC)
    s = unicodedata.normalize('NFC', s)
    # Collapse whitespace
    s = re.sub(r'[ \t]+', ' ', s).strip()
    # Remove lines that are pure punctuation/symbols (common garble pattern)
    lines = s.split('\n')
    lines = [l for l in lines if re.search(r'[\w\u4e00-\u9fff]', l)]
    return '\n'.join(lines).strip()


# ═══════════════════════════════════════════════════════════
#  Crawl & Backfill
# ═══════════════════════════════════════════════════════════

def _reconnect_if_dead(db, exc):
    """If ``exc`` signals a dead PG connection, return a fresh one.

    Otherwise return ``db`` unchanged.  This prevents a single connection
    drop from cascading into errors for every remaining query in a loop.
    """
    import psycopg2
    is_dead = isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
    if not is_dead:
        msg = str(exc).lower()
        is_dead = 'connection already closed' in msg or 'server closed the connection' in msg
    if is_dead:
        from lib.database import DOMAIN_TRADING, get_thread_db
        logger.warning('[Intel] PG connection dead (%s), obtaining fresh connection', type(exc).__name__)
        return get_thread_db(DOMAIN_TRADING)
    return db


def crawl_intel_source(db, category, query, search_fn, analyze_fn=None, crawl_date=None,
                       *, use_multi_source: bool = True):
    """Crawl a single intel source query using multi-source fetching.

    v6 upgrade: fans out to Google News RSS + CLS telegraph + DDG time-filtered
    concurrently.  Falls back to legacy single-source (search_fn) if multi-source
    is disabled or fails.

    When a result has published_date pre-set by the source (e.g. Google News
    pubDate, CLS ctime), the expensive HTML meta / LLM date extraction layers
    are skipped entirely — this is the main performance win.

    For items where timeliness cannot be determined by text/meta alone,
    we use the project's web search tools to do a targeted search and infer
    an approximate publication date.

    Returns number of items fetched.
    """
    if crawl_date is None:
        crawl_date = date.today().strftime('%Y-%m-%d')

    source_key = hashlib.md5(query.encode()).hexdigest()[:12]
    ttl_hours = INTEL_SOURCES.get(category, {}).get('ttl_hours', 12)
    expires_at = (datetime.now() + timedelta(hours=ttl_hours)).strftime('%Y-%m-%d %H:%M:%S')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    items_fetched = 0
    _pending_date_items = []   # items needing LLM date extraction

    try:
        # ── v6: Multi-source fetch ──
        results = []
        if use_multi_source:
            try:
                from lib.trading.sources import multi_source_search
                results = multi_source_search(query, category=category, max_results=20)
                logger.debug('[Crawl] multi_source_search returned %d results for %s/%s',
                             len(results), category, query[:40])
            except Exception as e:
                logger.warning('[Crawl] multi_source_search failed, falling back to legacy: %s', e, exc_info=True)
                results = []

        # Fallback to legacy single-source if multi-source returned nothing
        if not results:
            results = search_fn(query, max_results=8)

        if not results:
            record_crawl(db, crawl_date, category, source_key, 0, 'empty')
            return 0

        for r in results:
            title = _clean_intel_text(r.get('title', ''))
            url = r.get('url', r.get('href', ''))
            snippet = _clean_intel_text(r.get('snippet', r.get('body', '')))

            # Skip items with no meaningful content after cleaning
            if not title or len(title) < 4:
                continue

            # ── Blocklist check (skip data portals) ──
            from lib.trading.sources import _is_blocked_url
            if _is_blocked_url(url):
                continue

            if deduplicate_intel(db, title, url, category, snippet):
                continue

            # AI analysis if function provided
            analysis = ''
            relevance = 0.5
            if analyze_fn and snippet:
                try:
                    analysis, relevance = analyze_fn(title, snippet, category)
                except Exception as e:
                    logger.debug('[Trading] AI analyze_fn failed for title=%s, using raw snippet: %s', title[:60], e, exc_info=True)
                    analysis = snippet
                    relevance = 0.5

            # ── Step 1: Fetch full page content FIRST ──
            # This populates _html_head_cache so Layer 2 (meta date) gets a
            # free cache hit instead of making a redundant HTTP request.
            # It also gives us rich_content for LLM date extraction (Layer 3).
            rich_content = snippet  # fallback = search snippet
            raw_page = None
            if url and items_fetched < 10:  # limit expensive fetches per query
                try:
                    from lib.fetch import fetch_page_content
                    raw_page = fetch_page_content(url, max_chars=6000, timeout=10)
                    if raw_page and len(raw_page) > len(snippet) + 100:
                        # LLM-based content filtering for clean extraction
                        try:
                            from lib.fetch.content_filter import filter_web_content
                            rich_content = filter_web_content(
                                raw_page, url=url,
                                query=title or INTEL_SOURCES.get(category, {}).get('label', ''),
                            )
                        except Exception as e:
                            rich_content = raw_page  # use unfiltered if LLM filter fails
                            logger.debug('[Trading] LLM content filter failed, using unfiltered: %s', e)
                        logger.debug('📰 Rich content for "%s": %d→%d chars',
                                     title[:40], len(raw_page), len(rich_content))
                except Exception as e:
                    logger.debug('Rich content fetch failed for %s: %s', url[:60], e, exc_info=True)

            # ── Step 2: Multi-layer publish date extraction ──
            # Now that fetch_page_content has run, _html_head_cache is warm
            # and we have rich_content for LLM date extraction.
            published_date = ''   # YYYY-MM-DD day-level
            date_source = ''      # regex / url_path / meta / llm / fallback
            published_at = ''     # legacy column (backward compat)

            # Layer 0: Source-provided date (Google News pubDate, CLS ctime)
            # This is the most reliable — skip all other layers if present.
            src_date = r.get('published_date', '')
            src_at = r.get('published_at', '')
            if src_date and re.match(r'^\d{4}-\d{2}-\d{2}$', src_date):
                published_date = src_date
                date_source = 'source_pubdate'   # HOW the date was determined, not WHERE
                published_at = src_at or f"{src_date} 00:00:00"
            else:
                # Layer 1: regex from title/snippet/URL (cheapest, instant)
                pd, ds = _extract_publish_date(title, snippet, url)
                if pd:
                    published_date, date_source = pd, ds
                    published_at = f"{pd} 00:00:00"

                # Layer 1b: regex on rich_content (fetched page has more date clues)
                if not published_date and rich_content and rich_content != snippet:
                    pd, ds = _extract_publish_date('', rich_content[:2000], '')
                    if pd:
                        published_date, date_source = pd, f'{ds}_rich'
                        published_at = f"{pd} 00:00:00"
                        logger.debug('📅 Rich-content regex date for "%s": %s', title[:40], pd)

                # Layer 2: HTML meta tags (article:published_time, JSON-LD etc.)
                # After fetch_page_content(), _html_head_cache is warm → FREE cache hit
                if not published_date and url:
                    try:
                        from lib.fetch import get_publish_date_from_url
                        raw_meta = get_publish_date_from_url(url, timeout=6)
                        if raw_meta:
                            published_date = raw_meta[:10]
                            date_source = 'meta'
                            published_at = f"{published_date} 00:00:00"
                            logger.debug('📅 HTML meta date for %s: %s', url[:60], published_date)
                    except Exception as e:
                        logger.debug('HTML date extraction failed for %s: %s', url[:60], e, exc_info=True)

            # Layers 3-4 (LLM / web search) run as batch post-processing below
            # for items that still have no date.

            # Layer 5: fetched_at fallback — if no date from any source, use crawl date
            # This ensures 100% date coverage.  date_source='fetched_at_fallback'.
            if not published_date:
                published_date = crawl_date
                published_at = f"{crawl_date} 00:00:00"
                date_source = 'fetched_at_fallback'

            # Compute SimHash fingerprint for future dedup comparisons
            content_hash = compute_content_fingerprint(title, snippet)
            # Convert to signed 64-bit for DB storage (unsigned overflows)
            from lib.trading.simhash import to_signed64
            content_hash = to_signed64(content_hash)

            db.execute(
                '''INSERT INTO trading_intel_cache
                   (category,title,summary,raw_content,source_url,source_name,analysis,
                    relevance_score,published_at,published_date,date_source,fetched_at,analyzed_at,expires_at,
                    content_simhash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (category, title, snippet[:500], rich_content, url,
                 r.get('source', INTEL_SOURCES.get(category, {}).get('label', category)),
                 analysis, relevance, published_at, published_date, date_source,
                 now_str, now_str if analysis else '', expires_at,
                 content_hash)
            )
            items_fetched += 1

            # Track items that only got a fallback date — need LLM/search verification
            if date_source == 'fetched_at_fallback':
                _pending_date_items.append({
                    'title': title, 'snippet': snippet[:300],
                    'url': url, 'category': category
                })

        db.commit()

        # ── Layer 3: LLM batch date extraction for items missing dates ──
        if _pending_date_items:
            _llm_fill_publish_dates(db, _pending_date_items)

        # ── Layer 4: web search timeliness verification for items still undated ──
        if _pending_date_items:
            _web_search_fill_dates(db, _pending_date_items, search_fn)
            _pending_date_items.clear()

        # ── Snapshot cleanup: purge expired records so only fresh data remains ──
        src_cfg = INTEL_SOURCES.get(category, {})
        if src_cfg.get('data_mode') == 'snapshot' and items_fetched > 0:
            _purge_expired_snapshot(db, category)

        record_crawl(db, crawl_date, category, source_key, items_fetched)
    except Exception as e:
        logger.error('Crawl error for %s/%s: %s', category, query, e, exc_info=True)
        record_crawl(db, crawl_date, category, source_key, 0, f'error:{str(e)[:100]}')

    return items_fetched


def run_backfill(db, search_fn, analyze_fn=None, progress_callback=None):
    """Run backfill to ensure 3 months of coverage.
    Checks existing crawl log and only fetches missing dates."""
    start_date, end_date = get_backfill_date_range()
    total_missing = 0
    total_fetched = 0
    categories_status = {}

    for cat, src in INTEL_SOURCES.items():
        for query in src['queries']:
            source_key = hashlib.md5(query.encode()).hexdigest()[:12]
            missing = get_missing_dates(db, cat, source_key, start_date, end_date)
            total_missing += len(missing)

    if total_missing == 0:
        return {'status': 'up_to_date', 'message': '所有数据源已覆盖最近3个月', 'fetched': 0}

    processed = 0
    for cat, src in sorted(INTEL_SOURCES.items(), key=lambda x: x[1]['priority']):
        cat_fetched = 0
        for query in src['queries']:
            source_key = hashlib.md5(query.encode()).hexdigest()[:12]
            try:
                missing = get_missing_dates(db, cat, source_key, start_date, end_date)
            except Exception as e:
                logger.warning('[Backfill] get_missing_dates failed for %s/%s: %s', cat, query[:40], e)
                db = _reconnect_if_dead(db, e)
                continue
            if not missing:
                continue
            # For backfill, we batch dates — no need to crawl each date individually
            # Instead, crawl once with date-modified query and mark all dates covered
            modified_query = f"{query} {missing[0]}至{missing[-1]}"
            try:
                n = crawl_intel_source(db, cat, modified_query, search_fn, analyze_fn,
                                       crawl_date=missing[-1])
            except Exception as e:
                logger.warning('[Backfill] crawl_intel_source failed for %s/%s: %s', cat, query[:40], e)
                db = _reconnect_if_dead(db, e)
                n = 0
            cat_fetched += n
            total_fetched += n
            # Mark intermediate dates as covered
            for d in missing:
                record_crawl(db, d, cat, source_key, 0 if d != missing[-1] else n, 'backfill')
            processed += len(missing)

            if progress_callback:
                progress_callback(processed, total_missing, cat, query)

            time.sleep(0.5)  # Rate limiting

        categories_status[cat] = {
            'label': src['label'],
            'fetched': cat_fetched,
        }

    return {
        'status': 'completed',
        'total_missing_dates': total_missing,
        'total_fetched': total_fetched,
        'categories': categories_status,
    }


def get_intel_coverage_report(db):
    """Generate a coverage report showing what data we have."""
    start_date, end_date = get_backfill_date_range()
    report = {
        'range': {'start': start_date, 'end': end_date},
        'categories': {},
        'total_items': 0,
        'fresh_items': 0,
        'stale_items': 0,
    }
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for cat, src in INTEL_SOURCES.items():
        items = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_intel_cache WHERE category=?', (cat,)
        ).fetchone()
        fresh = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_intel_cache WHERE category=? AND expires_at > ?',
            (cat, now)
        ).fetchone()
        crawl_days = db.execute(
            'SELECT COUNT(DISTINCT crawl_date) as cnt FROM trading_intel_crawl_log WHERE category=? AND crawl_date>=? AND crawl_date<=?',
            (cat, start_date, end_date)
        ).fetchone()
        total_days = (datetime.strptime(end_date, '%Y-%m-%d') - datetime.strptime(start_date, '%Y-%m-%d')).days + 1

        report['categories'][cat] = {
            'label': src['label'],
            'total': items['cnt'],
            'fresh': fresh['cnt'],
            'stale': items['cnt'] - fresh['cnt'],
            'coverage_days': crawl_days['cnt'],
            'total_days': total_days,
            'coverage_pct': round(crawl_days['cnt'] / total_days * 100, 1) if total_days > 0 else 0,
        }
        report['total_items'] += items['cnt']
        report['fresh_items'] += fresh['cnt']
        report['stale_items'] += items['cnt'] - fresh['cnt']

    return report
