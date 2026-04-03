"""lib/trading/news_apis.py — Structured Financial News API Sources.

Provides high-quality, structured news data from professional financial
news APIs and RSS feeds — far more reliable than web search scraping.

Sources (in priority order):
  1. Eastmoney News API (np-listapi) — paginated JSON, precise timestamps,
     broad coverage across categories (macro, market, sector, global)
  2. Sina Finance RSS — stable, well-structured, multiple category feeds
  3. Investing.com RSS (Chinese) — macro, bonds, forex, commodities analysis

Each source returns items in the unified intel format:
  {title, snippet, url, source, published_date, published_at, category}

All items are stored in trading_intel_cache with proper date attribution,
enabling cache-aware backfill (items are never re-fetched).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from xml.etree import ElementTree

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'fetch_eastmoney_news',
    'fetch_sina_finance_rss',
    'fetch_investing_rss',
    'fetch_structured_news_sources',
]

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36'
    ),
}

# ═══════════════════════════════════════════════════════════
#  Source 1: Eastmoney News API (np-listapi)
# ═══════════════════════════════════════════════════════════

# Column IDs mapping to our intel categories
_EM_COLUMNS = {
    # column_id → (label, intel_category)
    '350': ('财经要闻', 'hot_news'),
    '345': ('重大新闻', 'hot_news'),
    '353': ('产经新闻', 'sector_rotation'),
    '355': ('国际财经', 'global_market'),
    '352': ('股市要闻', 'market_trend'),
    '357': ('债券要闻', 'bond_rate'),
    '359': ('基金要闻', 'fund_flow'),
    '1207': ('财经早餐', 'macro_policy'),
}


def fetch_eastmoney_news(
    start_date: str = '',
    end_date: str = '',
    max_pages: int = 5,
    page_size: int = 50,
) -> list[dict]:
    """Fetch news from Eastmoney np-listapi across multiple columns.

    This API provides paginated JSON with precise showTime timestamps,
    article codes for dedup, and media source attribution.

    Args:
        start_date: 'YYYY-MM-DD' — filter items after this date.
        end_date: 'YYYY-MM-DD' — filter items before this date.
        max_pages: Max pages to fetch per column.
        page_size: Items per page (max ~50).

    Returns:
        List of unified intel dicts with published_date set.
    """
    results = []
    base_url = 'https://np-listapi.eastmoney.com/comm/web/getNewsByColumns'

    for col_id, (label, category) in _EM_COLUMNS.items():
        col_results = 0
        for page in range(1, max_pages + 1):
            try:
                params = {
                    'client': 'web',
                    'biz': 'web_news_col',
                    'column': col_id,
                    'order': '1',
                    'needInteractData': '0',
                    'page_index': str(page),
                    'page_size': str(page_size),
                    'fields': 'code,showTime,title,mediaName,summary,url,uniqueUrl,Np_dst',
                    'types': '1,20',
                    'req_trace': str(int(time.time() * 1000)),
                }
                resp = requests.get(
                    base_url, params=params, headers=_HEADERS, timeout=10,
                )
                if not resp.ok:
                    logger.warning('[EastmoneyNews] HTTP %d for column %s page %d',
                                   resp.status_code, col_id, page)
                    break

                # Response is plain JSON (no JSONP wrapper when no callback param)
                data = resp.json()
                items = (data.get('data') or {}).get('list', [])
                if not items:
                    break

                stop_iteration = False
                for item in items:
                    title = item.get('title', '').strip()
                    if not title or len(title) < 4:
                        continue

                    show_time = item.get('showTime', '')
                    published_date = show_time[:10] if show_time else ''
                    published_at = show_time if show_time else ''

                    # Date range filtering
                    if start_date and published_date and published_date < start_date:
                        stop_iteration = True
                        break
                    if end_date and published_date and published_date > end_date:
                        continue

                    url = item.get('uniqueUrl', '') or item.get('url', '')
                    if not url:
                        code = item.get('code', '')
                        if code:
                            url = f'http://finance.eastmoney.com/a/{code}.html'

                    snippet = item.get('summary', '')
                    if not snippet:
                        snippet = title

                    results.append({
                        'title': title[:200],
                        'snippet': snippet[:500],
                        'url': url,
                        'source': item.get('mediaName', '东方财富'),
                        'published_date': published_date,
                        'published_at': published_at,
                        'category': category,
                    })
                    col_results += 1

                if stop_iteration:
                    break

                time.sleep(0.3)  # Rate limit between pages

            except requests.Timeout:
                logger.warning('[EastmoneyNews] Timeout for column %s page %d', col_id, page)
                break
            except Exception as e:
                logger.warning('[EastmoneyNews] Error for column %s page %d: %s',
                               col_id, page, e, exc_info=True)
                break

        if col_results > 0:
            logger.debug('[EastmoneyNews] Column %s (%s): %d items', col_id, label, col_results)

    logger.info('[EastmoneyNews] Total: %d items across %d columns',
                len(results), len(_EM_COLUMNS))
    return results


# ═══════════════════════════════════════════════════════════
#  Source 2: Sina Finance RSS
# ═══════════════════════════════════════════════════════════

# Sina Finance RSS feed URLs → (label, intel_category)
_SINA_FEEDS = {
    'http://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=50&page=1': ('新浪财经要闻', 'hot_news'),
    'http://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2517&k=&num=50&page=1': ('新浪股票要闻', 'market_trend'),
    'http://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2518&k=&num=50&page=1': ('新浪基金要闻', 'fund_flow'),
    'http://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2519&k=&num=30&page=1': ('新浪美股快报', 'global_market'),
}


def fetch_sina_finance_rss(
    start_date: str = '',
    end_date: str = '',
) -> list[dict]:
    """Fetch news from Sina Finance API feeds.

    Sina provides a JSON API for its news roll, with precise timestamps
    (Unix epoch) and structured article data.

    Args:
        start_date: 'YYYY-MM-DD' filter.
        end_date: 'YYYY-MM-DD' filter.

    Returns:
        List of unified intel dicts.
    """
    results = []

    for feed_url, (label, category) in _SINA_FEEDS.items():
        try:
            resp = requests.get(
                feed_url,
                headers={
                    **_HEADERS,
                    'Referer': 'https://finance.sina.com.cn/',
                },
                timeout=10,
            )
            if not resp.ok:
                logger.warning('[SinaRSS] HTTP %d for %s', resp.status_code, label)
                continue

            data = resp.json()
            items = data.get('result', {}).get('data', [])
            if not items:
                continue

            for item in items:
                title = (item.get('title') or '').strip()
                if not title or len(title) < 4:
                    continue

                # Parse Unix timestamp
                ctime = item.get('ctime', 0) or item.get('intime', 0)
                published_date = ''
                published_at = ''
                if ctime:
                    try:
                        dt = datetime.fromtimestamp(int(ctime))
                        published_date = dt.strftime('%Y-%m-%d')
                        published_at = dt.strftime('%Y-%m-%d %H:%M:%S')
                    except (ValueError, OSError) as exc:
                        logger.debug('[SinaRSS] Timestamp parse failed: %s: %s', ctime, exc)

                # Date range filtering
                if start_date and published_date and published_date < start_date:
                    continue
                if end_date and published_date and published_date > end_date:
                    continue

                url = item.get('url', '') or item.get('link', '')
                snippet = (item.get('intro', '') or item.get('summary', '') or title)[:500]
                media = item.get('media_name', '') or item.get('author', '') or '新浪财经'

                results.append({
                    'title': title[:200],
                    'snippet': snippet,
                    'url': url,
                    'source': media,
                    'published_date': published_date,
                    'published_at': published_at,
                    'category': category,
                })

            logger.debug('[SinaRSS] %s: %d items', label, len(items))

        except requests.Timeout:
            logger.warning('[SinaRSS] Timeout for %s', label)
        except Exception as e:
            logger.warning('[SinaRSS] Error for %s: %s', label, e, exc_info=True)

        time.sleep(0.2)

    logger.info('[SinaRSS] Total: %d items from %d feeds', len(results), len(_SINA_FEEDS))
    return results


# ═══════════════════════════════════════════════════════════
#  Source 3: Investing.com RSS (Chinese)
# ═══════════════════════════════════════════════════════════

# Investing.com Chinese RSS feeds → (label, intel_category)
_INVESTING_FEEDS = {
    'https://cn.investing.com/rss/news_14.rss': ('宏观与市场', 'macro_policy'),
    'https://cn.investing.com/rss/news_25.rss': ('股票资讯', 'market_trend'),
    'https://cn.investing.com/rss/news_285.rss': ('财经头条', 'hot_news'),
    'https://cn.investing.com/rss/bonds.rss': ('债券分析', 'bond_rate'),
    'https://cn.investing.com/rss/news_11.rss': ('期货/大宗商品', 'global_market'),
    'https://cn.investing.com/rss/central_banks.rss': ('央行动态', 'macro_policy'),
    'https://cn.investing.com/rss/news_95.rss': ('经济指标', 'macro_policy'),
}


def fetch_investing_rss(
    start_date: str = '',
    end_date: str = '',
    max_items_per_feed: int = 30,
) -> list[dict]:
    """Fetch news from Investing.com Chinese RSS feeds.

    Standard RSS/XML format parsed with xml.etree, no API key required.

    Args:
        start_date: 'YYYY-MM-DD' filter.
        end_date: 'YYYY-MM-DD' filter.
        max_items_per_feed: Max items per RSS feed.

    Returns:
        List of unified intel dicts.
    """
    results = []

    for feed_url, (label, category) in _INVESTING_FEEDS.items():
        try:
            resp = requests.get(
                feed_url,
                headers={
                    **_HEADERS,
                    'Accept': 'application/rss+xml, application/xml;q=0.9, */*;q=0.1',
                },
                timeout=15,
            )
            if not resp.ok:
                logger.warning('[InvestingRSS] HTTP %d for %s', resp.status_code, label)
                continue

            # Parse RSS XML
            root = ElementTree.fromstring(resp.content)
            channel = root.find('channel')
            if channel is None:
                continue

            item_count = 0
            for item_el in channel.findall('item'):
                if item_count >= max_items_per_feed:
                    break

                title_el = item_el.find('title')
                title = (title_el.text or '').strip() if title_el is not None else ''
                if not title or len(title) < 4:
                    continue

                link_el = item_el.find('link')
                url = (link_el.text or '').strip() if link_el is not None else ''

                desc_el = item_el.find('description')
                snippet = ''
                if desc_el is not None and desc_el.text:
                    # Strip HTML from description
                    snippet = re.sub(r'<[^>]+>', ' ', desc_el.text)
                    snippet = re.sub(r'\s+', ' ', snippet).strip()[:500]

                # Parse pubDate (RFC 2822)
                published_date = ''
                published_at = ''
                pub_el = item_el.find('pubDate')
                if pub_el is not None and pub_el.text:
                    try:
                        dt = parsedate_to_datetime(pub_el.text)
                        published_date = dt.strftime('%Y-%m-%d')
                        published_at = dt.strftime('%Y-%m-%d %H:%M:%S')
                    except Exception as e:
                        logger.debug('[InvestingRSS] Date parse failed: %s: %s',
                                     pub_el.text[:40] if pub_el.text else '?', e)

                # Date range filtering
                if start_date and published_date and published_date < start_date:
                    continue
                if end_date and published_date and published_date > end_date:
                    continue

                results.append({
                    'title': title[:200],
                    'snippet': snippet if snippet else title,
                    'url': url,
                    'source': f'Investing.com ({label})',
                    'published_date': published_date,
                    'published_at': published_at,
                    'category': category,
                })
                item_count += 1

            if item_count > 0:
                logger.debug('[InvestingRSS] %s: %d items', label, item_count)

        except requests.Timeout:
            logger.warning('[InvestingRSS] Timeout for %s', label)
        except requests.ConnectionError as e:
            logger.debug('[InvestingRSS] Connection error for %s (may be blocked): %s', label, e)
        except ElementTree.ParseError as e:
            logger.warning('[InvestingRSS] XML parse error for %s: %s', label, e)
        except Exception as e:
            logger.warning('[InvestingRSS] Error for %s: %s', label, e, exc_info=True)

        time.sleep(0.3)

    logger.info('[InvestingRSS] Total: %d items from %d feeds', len(results), len(_INVESTING_FEEDS))
    return results


# ═══════════════════════════════════════════════════════════
#  Unified Structured News Fetch + Store
# ═══════════════════════════════════════════════════════════

def fetch_structured_news_sources(
    db: Any,
    start_date: str,
    end_date: str,
    progress_cb: Callable | None = None,
) -> int:
    """Fetch from all structured news APIs and store in trading_intel_cache.

    This is called by backfill_historical_intel() BEFORE the search-based
    crawling, providing a high-quality base layer of intel with precise dates.

    Args:
        db: Database connection.
        start_date: 'YYYY-MM-DD'.
        end_date: 'YYYY-MM-DD'.
        progress_cb: Optional fn(done, total, message).

    Returns:
        Total number of new items stored.
    """
    from lib.trading.intel import compute_content_fingerprint, deduplicate_intel
    from lib.trading.simhash import to_signed64

    total_stored = 0
    sources = [
        ('东方财富新闻API', lambda: fetch_eastmoney_news(start_date, end_date)),
        ('新浪财经', lambda: fetch_sina_finance_rss(start_date, end_date)),
        ('Investing.com', lambda: fetch_investing_rss(start_date, end_date)),
    ]

    if progress_cb:
        progress_cb(
            0, len(sources),
            f'📡 正在从 {len(sources)} 个结构化新闻源获取情报...'
        )

    for src_idx, (src_name, fetch_fn) in enumerate(sources):
        try:
            if progress_cb:
                progress_cb(
                    src_idx, len(sources),
                    f'📡 [{src_idx+1}/{len(sources)}] 正在抓取 {src_name}...'
                )

            items = fetch_fn()
            stored = 0

            for item in items:
                title = item.get('title', '')
                url = item.get('url', '')
                snippet = item.get('snippet', '')
                category = item.get('category', 'hot_news')

                # Skip if duplicate
                if deduplicate_intel(db, title, url, category, snippet):
                    continue

                published_date = item.get('published_date', '')
                published_at = item.get('published_at', '')
                date_source = 'source_api' if published_date else 'fetched_at_fallback'
                if not published_date:
                    published_date = datetime.now().strftime('%Y-%m-%d')
                    published_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

                # Compute SimHash fingerprint for future dedup
                content_hash = compute_content_fingerprint(title, snippet)
                content_hash = to_signed64(content_hash)

                now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                expires_at = (datetime.now() + timedelta(hours=48)).strftime('%Y-%m-%d %H:%M:%S')

                try:
                    db.execute(
                        '''INSERT INTO trading_intel_cache
                           (category, title, summary, raw_content, source_url,
                            source_name, analysis, relevance_score,
                            published_at, published_date, date_source,
                            fetched_at, analyzed_at, expires_at, content_simhash)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (category, title, snippet[:500], snippet, url,
                         item.get('source', src_name),
                         '', 0.6,  # default relevance
                         published_at, published_date, date_source,
                         now_str, '', expires_at, content_hash)
                    )
                    stored += 1
                except Exception as e:
                    logger.debug('[NewsAPIs] Insert error for %s: %s', title[:40], e)

            if stored > 0:
                db.commit()
            total_stored += stored

            if progress_cb:
                progress_cb(
                    src_idx + 1, len(sources),
                    f'✅ {src_name}: +{stored}条新情报 (去重后)'
                )

            logger.info('[NewsAPIs] %s: stored %d/%d items (after dedup)',
                        src_name, stored, len(items))

        except Exception as e:
            logger.error('[NewsAPIs] %s fetch failed: %s', src_name, e, exc_info=True)
            if progress_cb:
                progress_cb(
                    src_idx + 1, len(sources),
                    f'⚠️ {src_name}: 抓取失败 - {e}'
                )

    if progress_cb:
        progress_cb(
            len(sources), len(sources),
            f'📡 结构化新闻源完成: 共新增 {total_stored} 条情报'
        )

    return total_stored
