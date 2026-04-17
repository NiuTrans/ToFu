"""routes/trading_intel.py — Intel CRUD, crawl, backfill, coverage, analyze."""

import json
import threading
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from lib.database import DOMAIN_TRADING, get_db
from lib.log import get_logger
from lib.rate_limiter import rate_limit

logger = get_logger(__name__)


def _is_conn_dead(exc: Exception) -> bool:
    """Check if an exception indicates the PG connection is dead.

    Used to break the cascade where a single connection drop causes
    errors for ALL remaining queries in a crawl loop.
    """
    import sqlite3
    if isinstance(exc, sqlite3.OperationalError):
        return True
    msg = str(exc).lower()
    return 'database is locked' in msg or 'disk I/O error' in msg


def _parse_llm_json(raw_content: str) -> dict:
    """Parse LLM JSON response with repair for truncated output.

    Handles markdown code fences, trailing commas, unterminated strings,
    and missing closing braces/brackets — defensive against LLM quirks.

    Args:
        raw_content: Raw LLM response text.

    Returns:
        Parsed dict, or empty dict if completely unparsable.
    """
    content = (raw_content or '').strip()
    if not content:
        return {}

    # Strip markdown code fences
    if content.startswith('```'):
        content = '\n'.join(content.split('\n')[1:])
    if content.endswith('```'):
        content = content[:content.rfind('```')]
    content = content.strip()

    # 1. Try direct parse first
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError) as _e:
        logger.debug('[Intel] Direct JSON parse failed, trying repair: %s', _e)

    # 2. Try repair via orchestrator utility (handles truncated strings,
    #    missing braces, trailing commas)
    try:
        from lib.utils import repair_json as _repair_json
        result = _repair_json(content)
        logger.info('[Intel] Repaired truncated LLM JSON (%d chars)', len(content))
        return result
    except (json.JSONDecodeError, TypeError, Exception) as e:
        logger.debug('[Intel] _repair_json also failed: %s', e)

    return {}

trading_intel_bp = Blueprint('trading_intel', __name__)

# ── Shared intel state (singleton) ──
_intel_state = {'last_crawl': None, 'next_crawl': None, 'status': 'idle',
                'last_count': 0, 'error': None}
_INTEL_INTERVAL = 2 * 3600   # 2 hours
_DAILY_CRAWL_HOUR = 7

# ── Sort order whitelist (prevents SQL injection via sort param) ──
_SORT_ORDERS = {
    'relevance': 'relevance_score DESC',
    'time': ("CASE WHEN published_date != '' "
             "THEN published_date ELSE SUBSTR(fetched_at, 1, 10) END DESC, fetched_at DESC"),
}
_SORT_DEFAULT = 'time'


def get_intel_state():
    """Expose intel state for other modules."""
    return _intel_state


# ── Intel crawl helpers ──

def _do_intel_crawl():
    """Intel crawl v6 — multi-source: Google News RSS + CLS + DDG time-filtered.

    Each query fans out to all available sources concurrently via
    ``multi_source_search()`` in ``lib/trading/sources.py``.
    Sources return pre-dated results (pubDate from RSS, ctime from CLS),
    so expensive HTML meta / LLM date extraction is skipped for most items.
    """
    from lib.database import DOMAIN_TRADING, get_thread_db
    from lib.search import perform_web_search
    from lib.trading import INTEL_SOURCES, _check_external_network, cleanup_stale_intel, crawl_intel_source

    if not _check_external_network():
        _intel_state['status'] = 'idle'
        _intel_state['error'] = 'external_network_unreachable'
        logger.info('[Intel v6] Skipping crawl: external network unreachable')
        return

    _intel_state['status'] = 'crawling'
    _intel_state['error'] = None
    logger.info('[Intel v6] Starting multi-source crawl across %d categories...', len(INTEL_SOURCES))

    try:
        db = get_thread_db(DOMAIN_TRADING)
        count = 0
        source_stats = {}  # category → items_fetched

        for cat_key, cat_info in INTEL_SOURCES.items():
            cat_count = 0
            for query in cat_info.get('queries', []):
                try:
                    # v6: use multi-source by default
                    n = crawl_intel_source(db, cat_key, query, perform_web_search,
                                           use_multi_source=True)
                    cat_count += n
                    count += n
                except Exception as e:
                    logger.warning('[Intel v6] Category %s query error: %s', cat_key, e, exc_info=True)
                    # ── Detect dead PG connection and reconnect ──
                    # Without this, a single connection drop cascades into
                    # errors for ALL remaining queries in the loop.
                    if _is_conn_dead(e):
                        logger.warning('[Intel v6] Connection dead, reconnecting before next query')
                        db = get_thread_db(DOMAIN_TRADING)

            source_stats[cat_key] = cat_count
            if cat_count > 0:
                logger.info('[Intel v6] ✅ %s: %d new items', cat_info.get('label', cat_key), cat_count)

        db.commit()
        _intel_state['last_crawl'] = datetime.now().isoformat()
        _intel_state['next_crawl'] = (datetime.now() + timedelta(seconds=_INTEL_INTERVAL)).isoformat()
        _intel_state['last_count'] = count
        _intel_state['last_source_stats'] = source_stats
        _intel_state['status'] = 'idle'
        logger.info('[Intel v6] Crawled %d new items across %d categories (breakdown: %s)',
                     count, len(INTEL_SOURCES),
                     ', '.join(f'{k}={v}' for k, v in source_stats.items() if v > 0))

        # ── Auto-analyze: queue batch analysis of newly crawled unanalyzed items ──
        if count > 0:
            _intel_state['status'] = 'analyzing'
            try:
                _auto_analyze_new_intel(None)
            except Exception as ae:
                logger.error('[Intel v6] Auto-analyze error: %s', ae, exc_info=True)
            finally:
                _intel_state['status'] = 'idle'

        # ── Housekeeping: purge very old records daily ──
        try:
            hk_db = get_thread_db(DOMAIN_TRADING)
            deleted = cleanup_stale_intel(hk_db)
            if deleted:
                logger.info('[Intel v6] Housekeeping removed %d stale records', deleted)
        except Exception as he:
            logger.error('[Intel v6] Housekeeping error: %s', he, exc_info=True)
    except Exception as e:
        _intel_state['status'] = 'error'
        _intel_state['error'] = str(e)
        logger.error('[Intel v6] Crawl error: %s', e, exc_info=True)


def _auto_analyze_new_intel(db_override=None):
    """Automatically analyze unanalyzed intel items after crawl.
    Runs in the same crawl thread — uses its own DB connection.

    ★ Uses smart_chat_batch to analyze ALL items concurrently across both
      API keys — typically 10-20× faster than the old sequential approach.
    """
    from lib.database import DOMAIN_TRADING, get_thread_db
    from lib.llm_dispatch import smart_chat_batch

    if db_override:
        db = db_override
    else:
        db = get_thread_db(DOMAIN_TRADING)

    unanalyzed = db.execute(
        """SELECT id, title, summary, raw_content, category, source_url, fetched_at
           FROM trading_intel_cache
           WHERE (analysis IS NULL OR analysis = '' OR analysis = '{}')
           ORDER BY fetched_at DESC LIMIT 20"""
    ).fetchall()

    if not unanalyzed:
        logger.info('[Intel AutoAnalyze] No unanalyzed items')
        return

    logger.info('[Intel AutoAnalyze] Analyzing %d items via parallel dispatch...', len(unanalyzed))

    # Get holdings context once
    holdings = db.execute('SELECT * FROM trading_holdings').fetchall()
    holdings_ctx = ''
    if holdings:
        holdings_ctx = '\n'.join([f"- {dict(h).get('asset_name','')}({dict(h).get('symbol','')})" for h in holdings])

    # ── Build all prompts at once ──
    items = [dict(row) for row in unanalyzed]
    prompts = []
    for item in items:
        content_preview = (item.get('raw_content', '') or item.get('summary', ''))[:1500]
        prompts.append(
            f"""你是一位资深金融分析师。请对以下新闻/情报进行快速分析。

标题: {item['title']}
内容: {content_preview}
分类: {item['category']}

{'当前持仓: ' + holdings_ctx if holdings_ctx else ''}

请严格按JSON格式回复（不要markdown标记）：
{{"sentiment": "bullish/bearish/neutral", "sentiment_label": "一句话原因(≤20字)", "impact_summary": "对投资交易的影响(2句话)", "affected_sectors": ["板块1"], "relevance_score": 0.0到1.0, "risk_level": "low/medium/high", "action_suggestion": "操作建议"}}""")

    # ── Fire all at once — dispatcher auto-balances across both keys ──
    results = smart_chat_batch(
        prompts=prompts,
        temperature=0.3,
        capability='cheap',
        log_prefix='[IntelAutoAnalyze]',
        max_concurrent=8,            # 4 per key ≈ safe RPM headroom
    )

    # ── Store results ──
    analyzed = 0
    for item, result in zip(items, results):
        if result is None:
            continue
        try:
            content, _usage = result
            content = content or ''
            analysis_json = _parse_llm_json(content)
            if not analysis_json:
                logger.warning('[Intel] Failed to parse LLM analysis JSON for item, using neutral defaults: %.200s', content)
                analysis_json = {'sentiment': 'neutral', 'impact_summary': content[:300], 'relevance_score': 0.5}

            relevance = float(analysis_json.get('relevance_score', 0.5))
            sentiment = analysis_json.get('sentiment', 'neutral')
            now = datetime.now().isoformat()
            db.execute(
                'UPDATE trading_intel_cache SET analysis=?, analyzed_at=?, relevance_score=?, sentiment=? WHERE id=?',
                (json.dumps(analysis_json, ensure_ascii=False), now, relevance, sentiment, item['id'])
            )
            db.commit()
            analyzed += 1
        except Exception as e:
            logger.error('[Intel AutoAnalyze] Item %s error: %s', item.get('id'), e, exc_info=True)
            continue

    logger.info('[Intel AutoAnalyze] Done: %d/%d analyzed (parallel)', analyzed, len(unanalyzed))


def _do_intel_backfill():
    """Backfill 3 months of intel data."""
    from lib.database import DOMAIN_TRADING, get_thread_db
    from lib.search import perform_web_search
    from lib.trading import run_backfill
    _intel_state['status'] = 'backfilling'
    try:
        db = get_thread_db(DOMAIN_TRADING)
        result = run_backfill(db, perform_web_search)
        _intel_state['status'] = 'idle'
        logger.info('[Intel] Backfill complete: %s', result)
    except Exception as e:
        _intel_state['status'] = 'error'
        _intel_state['error'] = str(e)
        logger.error('[Intel] Backfill error: %s', e, exc_info=True)


def seed_builtin_strategies():
    """Seed default strategies and groups if DB is empty — delegates to lib/trading.py."""
    from lib.database import DOMAIN_TRADING, get_thread_db
    from lib.trading import seed_builtin_strategies as _seed
    from lib.trading import seed_builtin_strategy_groups as _seed_groups
    db = get_thread_db(DOMAIN_TRADING)
    try:
        _seed(db)
        _seed_groups(db)
    except Exception as e:
        logger.error('[Trading] Seed error: %s', e, exc_info=True)


def migrate_intel_categories():
    """Ensure trading_intel_cache.category is populated correctly."""
    from lib.database import DOMAIN_TRADING, get_thread_db
    from lib.trading import INTEL_SOURCES
    db = get_thread_db(DOMAIN_TRADING)
    try:
        empty_cats = db.execute(
            "SELECT id, source_url, title FROM trading_intel_cache WHERE category IS NULL OR category = ''"
        ).fetchall()
        if not empty_cats:
            return
        for row in empty_cats:
            row = dict(row)
            url = row.get('source_url', '') or ''
            title = row.get('title', '') or ''
            best_cat = 'market_news'
            for cat_key, cat_info in INTEL_SOURCES.items():
                keywords = cat_info.get('keywords', [])
                if any(kw.lower() in title.lower() or kw.lower() in url.lower() for kw in keywords):
                    best_cat = cat_key
                    break
            db.execute('UPDATE trading_intel_cache SET category=? WHERE id=?', (best_cat, row['id']))
        db.commit()
        logger.info('[Intel] Migrated %d items with empty categories', len(empty_cats))
    except Exception as e:
        logger.error('[Intel] Category migration error: %s', e, exc_info=True)


def start_intel_worker(app):
    """Start the intel background worker thread."""
    import time as _t

    def _intel_worker():
        _t.sleep(30)  # 30s after startup
        last_daily = None
        while True:
            try:
                now_dt = datetime.now()
                if now_dt.hour == _DAILY_CRAWL_HOUR:
                    if last_daily != now_dt.date():
                        _do_intel_crawl()
                        last_daily = now_dt.date()
                else:
                    _do_intel_crawl()
            except Exception as e:
                logger.error('[Intel Worker] Unhandled error: %s', e, exc_info=True)
            _intel_state['next_crawl'] = (datetime.now() + timedelta(seconds=_INTEL_INTERVAL)).strftime('%Y-%m-%d %H:%M:%S')
            _t.sleep(_INTEL_INTERVAL)

    t = threading.Thread(target=_intel_worker, daemon=True)
    t.start()


# ── Route handlers ──

@trading_intel_bp.route('/api/trading/intel', methods=['GET'])
def trading_intel_list():
    db = get_db(DOMAIN_TRADING)
    category = request.args.get('category', '')
    sort = request.args.get('sort', 'time')
    sentiment = request.args.get('sentiment', '')
    q = request.args.get('q', '')

    conditions = ["expires_at > ?"]
    params = [datetime.now().isoformat()]

    if category and category != 'all':
        conditions.append("category = ?")
        params.append(category)
    if sentiment and sentiment != 'all':
        conditions.append("json_extract(analysis, '$.sentiment') = ?")
        params.append(sentiment)
    if q:
        conditions.append("(title LIKE ? OR summary LIKE ?)")
        params.extend([f'%{q}%', f'%{q}%'])

    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    # Sort by actual publication date (day-level published_date), fall back to fetched_at.
    # _SORT_ORDERS whitelist defined at module level — prevents SQL injection.
    if sort not in _SORT_ORDERS:
        logger.warning('[Intel] Unknown sort parameter: %s — falling back to %s', sort, _SORT_DEFAULT)
        sort = _SORT_DEFAULT
    order = _SORT_ORDERS[sort]

    # NOTE: `where` is built entirely from hardcoded condition templates with ? params.
    #       `order` is from _SORT_ORDERS whitelist. No user input in SQL structure.
    # Accept frontend limit param (default 200, max 500 — no artificial cap)
    try:
        limit = min(int(request.args.get('limit', '200')), 500)
    except (ValueError, TypeError):
        logger.debug('[Intel] Invalid limit param %r, defaulting to 200', request.args.get('limit'), exc_info=True)
        limit = 200

    query = 'SELECT * FROM trading_intel_cache' + where + ' ORDER BY ' + order + ' LIMIT ?'
    count_query = 'SELECT COUNT(*) FROM trading_intel_cache' + where
    query_params = params + [limit]

    try:
        rows = db.execute(query, query_params).fetchall()
    except Exception as e:
        # json_extract may fail on malformed analysis column — fall back to unfiltered query
        logger.warning('Intel list query failed (likely malformed JSON in analysis column): %s', e, exc_info=True)
        if sentiment and sentiment != 'all':
            conditions = [c for c in conditions if 'json_extract' not in c]
            params = [p for p in params if p != sentiment]
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
        query = 'SELECT * FROM trading_intel_cache' + where + ' ORDER BY ' + order + ' LIMIT ?'
        count_query = 'SELECT COUNT(*) FROM trading_intel_cache' + where
        query_params = params + [limit]
        rows = db.execute(query, query_params).fetchall()
    items = [dict(r) for r in rows]
    for item in items:
        if item.get('analysis') and isinstance(item['analysis'], str):
            try:
                item['analysis'] = json.loads(item['analysis'])
            except Exception as e:
                logger.warning('Failed to parse intel analysis JSON for item %s: %s', item.get('id', '?'), e, exc_info=True)
                item['analysis'] = {}

    total = db.execute(count_query, params).fetchone()[0]
    unanalyzed = db.execute(
        "SELECT COUNT(*) FROM trading_intel_cache WHERE (analysis IS NULL OR analysis = '' OR analysis = '{}') AND expires_at > ?",
        (datetime.now().isoformat(),)
    ).fetchone()[0]

    return jsonify({
        'items': items,
        'total': total,
        'unanalyzed': unanalyzed,
        'intel_state': _intel_state,
    })


@trading_intel_bp.route('/api/trading/intel/status', methods=['GET'])
def trading_intel_status():
    """Return current intel crawl status for frontend polling."""
    db = get_db(DOMAIN_TRADING)
    total = db.execute("SELECT COUNT(*) FROM trading_intel_cache WHERE expires_at > ?",
                       (datetime.now().isoformat(),)).fetchone()[0]
    state = dict(_intel_state)
    state['total_articles'] = total
    return jsonify(state)


@trading_intel_bp.route('/api/trading/intel/crawl', methods=['POST'])
@trading_intel_bp.route('/api/trading/intel/refresh', methods=['POST'])
@rate_limit(limit=2, per=3600)  # 2 requests per hour
def trading_intel_trigger_crawl():
    def _run():
        _do_intel_crawl()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'message': '情报爬取已触发'})


@trading_intel_bp.route('/api/trading/intel/coverage', methods=['GET'])
def trading_intel_coverage():
    db = get_db(DOMAIN_TRADING)
    total_days = int(request.args.get('days', '90'))
    cutoff = (datetime.now() - timedelta(days=total_days)).strftime('%Y-%m-%d')
    rows = db.execute('''
        SELECT crawl_date, category, items_fetched, status FROM trading_intel_crawl_log
        WHERE crawl_date >= ?
    ''', (cutoff,)).fetchall()

    fresh_counts = {}
    for r in db.execute('''
        SELECT category, COUNT(*) as cnt FROM trading_intel_cache
        WHERE expires_at > ? GROUP BY category
    ''', (datetime.now().isoformat(),)).fetchall():
        fresh_counts[dict(r)['category']] = dict(r)['cnt']

    total_counts = {}
    for r in db.execute('SELECT category, COUNT(*) as cnt FROM trading_intel_cache GROUP BY category').fetchall():
        total_counts[dict(r)['category']] = dict(r)['cnt']

    from lib.trading import INTEL_SOURCES
    cat_labels = {k: v['label'] for k, v in INTEL_SOURCES.items()}

    cat_days = {}
    for r in rows:
        r = dict(r)
        cat = r['category']
        if cat not in cat_days:
            cat_days[cat] = set()
        if r['status'] == 'ok' and r['items_fetched'] > 0:
            cat_days[cat].add(r['crawl_date'])

    categories = {}
    all_cats = set(cat_days.keys()) | set(fresh_counts.keys()) | set(total_counts.keys())
    for cat in sorted(all_cats):
        days_covered = len(cat_days.get(cat, set()))
        pct = round(days_covered / total_days * 100, 1) if total_days > 0 else 0
        categories[cat] = {
            'label': cat_labels.get(cat, cat),
            'coverage_pct': pct,
            'coverage_days': days_covered,
            'total_days': total_days,
            'fresh': fresh_counts.get(cat, 0),
            'total': total_counts.get(cat, 0),
        }

    raw_coverage = {}
    for r in rows:
        r = dict(r)
        d = r['crawl_date']
        if d not in raw_coverage:
            raw_coverage[d] = {}
        raw_coverage[d][r['category']] = {'items': r['items_fetched'], 'status': r['status']}

    return jsonify({'categories': categories, 'coverage': raw_coverage, 'total_days': total_days})


@trading_intel_bp.route('/api/trading/intel/backfill', methods=['POST'])
@rate_limit(limit=1, per=3600)  # 1 request per hour (very resource-intensive)
def trading_intel_backfill():
    def _run():
        _do_intel_backfill()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({'ok': True, 'message': '已触发3个月数据回填'})


@trading_intel_bp.route('/api/trading/intel/<int:iid>/preview', methods=['GET'])
@rate_limit(limit=30, per=60)  # 30 requests per minute
def trading_intel_preview(iid):
    """Fetch full content of an intel article for preview.

    Uses the web search pipeline: fetch_page_content (with Playwright fallback,
    SPA detection, etc.) + filter_web_content (LLM-based noise removal).
    Caches the result in raw_content so subsequent previews are instant.
    """
    db = get_db(DOMAIN_TRADING)
    item = db.execute('SELECT * FROM trading_intel_cache WHERE id=?', (iid,)).fetchone()
    if not item:
        return jsonify({'error': 'Not found'}), 404
    item = dict(item)

    # Return cached content if we already have rich content (not just the snippet)
    cached = item.get('raw_content', '')
    snippet = item.get('summary', '')
    # If raw_content is meaningfully longer than summary, it's already fetched
    if cached and len(cached) > len(snippet) + 200:
        return jsonify({
            'ok': True,
            'id': iid,
            'title': item.get('title', ''),
            'content': cached,
            'source_url': item.get('source_url', ''),
            'source': 'cache',
        })

    url = item.get('source_url', '')
    if not url:
        return jsonify({
            'ok': True,
            'id': iid,
            'title': item.get('title', ''),
            'content': snippet or '（无原文链接，仅有摘要）',
            'source_url': '',
            'source': 'snippet_only',
        })

    # ── Fetch full page content using the web search pipeline ──
    try:
        from lib.fetch import fetch_page_content
        raw = fetch_page_content(url, max_chars=8000)
        if not raw or len(raw) < 50:
            return jsonify({
                'ok': True,
                'id': iid,
                'title': item.get('title', ''),
                'content': snippet or '（页面内容抓取失败）',
                'source_url': url,
                'source': 'fetch_failed',
            })

        # ── LLM-based content filtering for clean extraction ──
        filtered = raw
        try:
            from lib.fetch.content_filter import filter_web_content
            query_hint = item.get('title', '') or item.get('category', '')
            filtered = filter_web_content(raw, url=url, query=query_hint)
        except Exception as fe:
            logger.warning('[IntelPreview] LLM filter failed for %s, using raw: %s', url[:80], fe, exc_info=True)

        # Cache the filtered content for future requests
        db.execute('UPDATE trading_intel_cache SET raw_content=? WHERE id=?', (filtered, iid))
        db.commit()

        return jsonify({
            'ok': True,
            'id': iid,
            'title': item.get('title', ''),
            'content': filtered,
            'source_url': url,
            'source': 'fetched',
        })
    except Exception as e:
        logger.error('[IntelPreview] Error fetching %s: %s', url[:80], e, exc_info=True)
        return jsonify({
            'ok': True,
            'id': iid,
            'title': item.get('title', ''),
            'content': snippet or '（抓取出错）',
            'source_url': url,
            'source': 'error',
        })


@trading_intel_bp.route('/api/trading/intel/<int:iid>/analyze', methods=['POST'])
@rate_limit(limit=10, per=60)  # 10 requests per minute
def trading_intel_analyze_item(iid):
    """Deep-analyze a single intel article."""
    db = get_db(DOMAIN_TRADING)
    item = db.execute('SELECT * FROM trading_intel_cache WHERE id=?', (iid,)).fetchone()
    if not item:
        return jsonify({'error': 'Not found'}), 404
    item = dict(item)

    full_content = item.get('raw_content', '')
    if not full_content and item.get('source_url'):
        try:
            from lib.fetch import fetch_page_content
            full_content = fetch_page_content(item['source_url'], max_chars=3000)
            db.execute('UPDATE trading_intel_cache SET raw_content=? WHERE id=?', (full_content, iid))
            db.commit()
        except Exception as e:
            logger.warning('[IntelAnalyze] Failed to fetch raw content for item %d: %s', iid, e, exc_info=True)

    holdings_ctx = _get_holdings_ctx(db)

    prompt = f"""你是一位资深金融分析师。请对以下新闻/情报进行深度分析。

## 新闻信息
标题: {item['title']}
摘要: {item['summary']}
{'正文(节选): ' + full_content[:2000] if full_content else ''}
分类: {item['category']}
日期: {item.get('fetched_at', '')[:10]}

## 用户当前持仓
{holdings_ctx if holdings_ctx else '（暂无持仓）'}

请严格按以下JSON格式回复（不要添加任何markdown标记）：
{{
  "sentiment": "bullish 或 bearish 或 neutral",
  "sentiment_label": "一句话概括看多/看空/中性的原因，不超过20字",
  "impact_summary": "这条信息对投资交易的具体影响分析，2-3句话",
  "affected_sectors": ["受影响的行业板块1", "板块2"],
  "holdings_relevance": "与用户当前持仓的关联分析",
  "relevance_score": 0.0到1.0的数字,
  "risk_level": "low 或 medium 或 high",
  "time_horizon": "short 或 medium 或 long",
  "action_suggestion": "基于此信息的具体投资操作建议"
}}"""

    from lib.llm_dispatch import smart_chat
    content, _ = smart_chat(messages=[{'role': 'user', 'content': prompt}],
                            temperature=0.3,
                            capability='cheap', log_prefix='[IntelAnalysis]')

    content = content or ''
    analysis_json = _parse_llm_json(content)
    if not analysis_json:
        logger.warning('[Intel] Failed to parse single-item LLM analysis JSON for item %d, using neutral defaults: %.200s', iid, content)
        analysis_json = {'sentiment': 'neutral', 'impact_summary': content[:500], 'relevance_score': 0.5}
    relevance = float(analysis_json.get('relevance_score', 0.5))

    now = datetime.now().isoformat()
    db.execute('UPDATE trading_intel_cache SET analysis=?, analyzed_at=?, relevance_score=? WHERE id=?',
               (json.dumps(analysis_json, ensure_ascii=False), now, relevance, iid))
    db.commit()
    return jsonify({'ok': True, 'analysis': analysis_json, 'relevance_score': relevance})


@trading_intel_bp.route('/api/trading/intel/batch-analyze', methods=['POST'])
def trading_intel_batch_analyze():
    """Batch-analyze unanalyzed articles."""
    db = get_db(DOMAIN_TRADING)
    data = request.get_json(silent=True) or {}
    batch_size = min(int(data.get('batch_size', 10)), 30)
    category = data.get('category', '')

    holdings_ctx = _get_holdings_ctx(db)

    conditions = ["(analysis IS NULL OR analysis = '' OR analysis = '{}' OR analysis = '{\"impact\": \"neutral\"}')"]
    params = []
    if category and category != 'all':
        conditions.append("category = ?")
        params.append(category)
    where = ' WHERE ' + ' AND '.join(conditions)
    params.append(batch_size)

    # NOTE: `where` built from hardcoded conditions with ? params — no user input in SQL structure.
    unanalyzed = db.execute(
        'SELECT id, title, summary, category, fetched_at FROM trading_intel_cache'
        + where + ' ORDER BY fetched_at DESC LIMIT ?',
        params
    ).fetchall()
    unanalyzed = [dict(r) for r in unanalyzed]

    if not unanalyzed:
        return jsonify({'ok': True, 'analyzed': 0, 'message': '所有文章已分析完毕'})

    # ── Build all sub-batch prompts ──
    from lib.llm_dispatch import smart_chat_batch

    sub_batches = []       # list of (sub_batch, prompt)
    for i in range(0, len(unanalyzed), 5):
        sub_batch = unanalyzed[i:i+5]
        articles_text = ""
        for idx, a in enumerate(sub_batch, 1):
            articles_text += f"\n### 文章{idx} [ID={a['id']}]\n标题: {a['title']}\n摘要: {a['summary']}\n分类: {a['category']}\n日期: {a['fetched_at'][:10] if a['fetched_at'] else '未知'}\n"

        prompt = f"""你是一位资深金融分析师。请对以下{len(sub_batch)}条新闻逐一分析情绪和投资影响。

## 用户当前持仓
{holdings_ctx if holdings_ctx else '（暂无持仓）'}

## 待分析新闻
{articles_text}

请严格按以下JSON数组格式回复（不要添加任何markdown标记）：
[
  {{
    "id": 文章ID数字,
    "sentiment": "bullish 或 bearish 或 neutral",
    "sentiment_label": "一句话概括原因(不超过15字)",
    "impact_summary": "对投资交易的影响(1-2句话)",
    "affected_sectors": ["板块1", "板块2"],
    "holdings_relevance": "与用户持仓的关联(1句话)",
    "relevance_score": 0.0到1.0,
    "risk_level": "low/medium/high",
    "time_horizon": "short/medium/long",
    "action_suggestion": "操作建议(1句话)"
  }}
]"""
        sub_batches.append((sub_batch, prompt))

    # ── Fire ALL sub-batch prompts concurrently across both keys ──
    prompts = [p for _, p in sub_batches]
    llm_results = smart_chat_batch(
        prompts=prompts,
        temperature=0.3,
        capability='text',
        log_prefix='[BatchAnalysis]',
        max_concurrent=6,
    )

    # ── Store results ──
    analyzed_count = 0
    results = []
    for (sub_batch, _prompt), llm_result in zip(sub_batches, llm_results):
        if llm_result is None:
            # fallback for failed sub-batches
            now = datetime.now().isoformat()
            for a in sub_batch:
                fallback = json.dumps({'sentiment': 'neutral', 'sentiment_label': '待分析', 'relevance_score': 0.3}, ensure_ascii=False)
                db.execute('UPDATE trading_intel_cache SET analysis=?, analyzed_at=?, relevance_score=? WHERE id=?',
                           (fallback, now, 0.3, a['id']))
                analyzed_count += 1
            db.commit()
            continue

        try:
            content, _usage = llm_result
            content = content or ''
            batch_results = _parse_llm_json(content)
            if not isinstance(batch_results, list):
                batch_results = [batch_results]

            now = datetime.now().isoformat()
            for br in batch_results:
                aid = br.get('id')
                if aid is None:
                    continue
                relevance = min(1.0, max(0.0, float(br.get('relevance_score', 0.5))))
                analysis_str = json.dumps(br, ensure_ascii=False)
                db.execute('UPDATE trading_intel_cache SET analysis=?, analyzed_at=?, relevance_score=? WHERE id=?',
                           (analysis_str, now, relevance, int(aid)))
                analyzed_count += 1
                results.append({'id': aid, 'sentiment': br.get('sentiment', ''), 'relevance_score': relevance})
            db.commit()
        except Exception as e:
            logger.error('[BatchAnalysis] Error: %s', e, exc_info=True)
            now = datetime.now().isoformat()
            for a in sub_batch:
                fallback = json.dumps({'sentiment': 'neutral', 'sentiment_label': '待分析', 'relevance_score': 0.3}, ensure_ascii=False)
                db.execute('UPDATE trading_intel_cache SET analysis=?, analyzed_at=?, relevance_score=? WHERE id=?',
                           (fallback, now, 0.3, a['id']))
                analyzed_count += 1
            db.commit()

    remaining = db.execute(
        "SELECT COUNT(*) FROM trading_intel_cache WHERE analysis IS NULL OR analysis = '' OR analysis = '{}' OR analysis = '{\"impact\": \"neutral\"}'"
    ).fetchone()[0]

    return jsonify({
        'ok': True, 'analyzed': analyzed_count, 'remaining': remaining,
        'results': results, 'message': f'已分析{analyzed_count}篇，剩余{remaining}篇待分析'
    })


# ── Shared helpers used by other trading routes ──

def _get_holdings_ctx(db):
    """Build holdings context string for LLM prompts."""
    rows = db.execute('SELECT * FROM trading_holdings ORDER BY buy_date DESC').fetchall()
    if not rows:
        return ''
    from lib.trading import calc_sell_fee, fetch_asset_info, fetch_trading_fees, get_latest_price
    ctx = "当前持仓:\n"
    for row in rows:
        h = dict(row)
        code = h['symbol']
        try:
            nav_val, nav_date = get_latest_price(code)
            info = fetch_asset_info(code)
            current_nav = nav_val or h['buy_price']
            profit = (current_nav - h['buy_price']) * h['shares']
            profit_pct = (current_nav / h['buy_price'] - 1) * 100 if h['buy_price'] > 0 else 0
            fetch_trading_fees(code)
            sell_info = calc_sell_fee(h)
            ctx += (f"- {code} {h.get('asset_name', info.get('name',''))}: "
                    f"{h['shares']}份, 成本¥{h['buy_price']}, 现价¥{current_nav}, "
                    f"{'盈利' if profit >= 0 else '亏损'}¥{abs(profit):.2f} ({profit_pct:+.2f}%), "
                    f"赎回费率{sell_info['fee_rate']*100:.2f}%\n")
        except Exception as e:
            logger.warning("[Holdings] ctx build error for %s: %s", code, e, exc_info=True)
            ctx += f"- {code} {h.get('asset_name','')}: {h['shares']}份, 成本¥{h['buy_price']}\n"
    return ctx


def _get_strategies_ctx(db):
    """Build strategies context string for LLM prompts."""
    rows = db.execute("SELECT * FROM trading_strategies WHERE status='active'").fetchall()
    if not rows:
        return ''
    ctx = "\n## 用户策略\n"
    for r in rows:
        r = dict(r)
        ctx += f"- {r['name']}: {r['logic']} (场景: {r.get('scenario', '通用')})\n"
    return ctx
