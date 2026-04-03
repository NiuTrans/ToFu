"""lib/trading/sources.py — Multi-source news fetchers for industrial-grade intel crawling.

Sources (in priority order):
  1. Google News RSS  — 100 items/query, precise pubDate, broad coverage
  2. 财联社 (CLS) Telegraph — real-time flash news with unix timestamps
  3. DuckDuckGo HTML (time-filtered) — df=d (past day) / df=w (past week)
  4. DuckDuckGo HTML (unfiltered) — fallback for long-tail queries

Each fetcher returns a unified list of dicts:
  {title, snippet, url, source, published_date?, published_at?}

The `published_date` field (YYYY-MM-DD) is set at the source level when
available (Google News RSS, CLS timestamps), eliminating the need for
expensive HTML meta / LLM date extraction for those items.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from email.utils import parsedate_to_datetime
from html import unescape

import requests

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'fetch_google_news_rss',
    'fetch_cls_telegraph',
    'fetch_ddg_time_filtered',
    'multi_source_search',
    'PORTAL_BLOCKLIST',
]

_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 Chrome/121.0.0.0 Safari/537.36'
    ),
}

# ═══════════════════════════════════════════════════════════
#  URL Blocklist — data portals that look like news but aren't
# ═══════════════════════════════════════════════════════════

PORTAL_BLOCKLIST = {
    # These are data dashboard / index / quote pages that never change content
    # but DDG returns them for every financial query.
    'data.eastmoney.com/zjlx',         # 资金流向数据中心
    'data.eastmoney.com/hsgt/index',   # 沪深港通首页
    'data.eastmoney.com/shibor',       # Shibor数据页
    'data.eastmoney.com/bkzj',         # 板块资金首页
    'www.sse.org.cn/www/market',       # 上交所市场概览
    'www.szse.cn/market/trend',        # 深交所市场趋势
    'quote.eastmoney.com',             # 行情首页
    'www.morningstar.cn',              # 晨星首页
    'fund.eastmoney.com/#',            # 天天基金网首页
    'www.chinamoney.com.cn',           # 中国货币网首页
    'yield.chinabond.com.cn',          # 中债收益率曲线首页
    # ── Data portals that look like articles but aren't ──
    'www.shibor.org',                  # Shibor官网
    'www.macroview.club',              # MacroView数据图表
    'sc.macromicro.me',                # 财经M平方数据
    'tradingeconomics.com',            # Trading Economics数据
    'cn.investing.com/rates-bonds',    # 英为财情债券
    'q.10jqka.com.cn/gn/',             # 同花顺概念板块首页
    'q.stock.sohu.com/cn/zs.shtml',    # 搜狐指数首页
    'wallstreetcn.com/markets',        # 华尔街见闻行情页
    # ── Educational / Q&A / static pages ──
    'zhihu.com/question/',             # 知乎问答 (not news)
    'forextime.com/zh/market-analysis', # FXTM教程
    'tushare.pro/document',            # API文档
    'fund.sohu.com/',                  # 搜狐基金频道首页
    'cn-morningstar.cn',               # 晨星中国旧站
    'goodsfu.10jqka.com.cn',           # 同花顺期货首页
    'fund.10jqka.com.cn/datacenter',   # 同花顺数据中心
    'morningstar.cn/help',             # 晨星工具/帮助页
    'fund.eastmoney.com/data/',        # 天天基金网数据首页
    'bond.eastmoney.com/',             # 债券频道首页
    'cn.investing.com/analysis/',      # 英为财情分析首页
    'imf.org/en/research/commodity',   # IMF商品数据
    'nfra.gov.cn/cn/view/pages/index', # 金融监管数据页
    'hsi.com.hk/schi/indexes',         # 恒生指数官网
    'gushitong.baidu.com/index',       # 百度股市通行情
    'fundf10.eastmoney.com/jjpj_',     # 单只资产评级页
    'lhratings.com/file/',             # 联合资信PDF报告
    'icbc.com.cn/webpage/fund',        # 工行基金页
    'boc.cn/fimarkets/lilv',           # 中国银行利率页
    # ── DDG ad redirects (catch-all) ──
    'duckduckgo.com/y.js',             # DDG sponsored ads
}


def _is_blocked_url(url: str) -> bool:
    """Check if URL matches a known data portal or ad redirect (not a news article)."""
    if not url:
        return True   # empty URLs are always blocked
    url_clean = url.lower().replace('https://', '').replace('http://', '').rstrip('/')
    # DDG ad redirects — catch all variants
    if 'duckduckgo.com/y.js' in url_clean:
        return True
    for pattern in PORTAL_BLOCKLIST:
        if pattern in url_clean:
            return True
    return False


def _clean(s: str) -> str:
    """Clean text: strip HTML tags, decode entities, collapse whitespace."""
    if not s:
        return ''
    s = re.sub(r'<[^>]+>', ' ', s)
    s = unescape(s)
    s = re.sub(r'[\u200b-\u200f\u2028-\u202f\u2060\ufeff]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


# ═══════════════════════════════════════════════════════════
#  Google News URL resolution
# ═══════════════════════════════════════════════════════════

def _resolve_google_news_url(gnews_url: str, timeout: float = 5) -> str:
    """Try to resolve a Google News redirect URL to the real article URL.

    Google News RSS items use `news.google.com/rss/articles/...` URLs that
    are protobuf-encoded article IDs.  Resolution requires JavaScript
    rendering and cannot be done with simple HTTP requests (the protobuf
    payload contains Google-internal article IDs, not actual URLs).

    The redirect URLs still work as clickable links in browsers — Google
    handles the redirect via client-side JavaScript.

    Returns the original URL unchanged (resolution disabled by default).
    """
    if 'news.google.com' not in gnews_url:
        return gnews_url
    # NOTE: Simple HTTP HEAD/GET does NOT resolve Google News URLs.
    # They use protobuf-encoded internal IDs resolved via JavaScript.
    # Keeping original URL — it works as clickable link in browsers.
    return gnews_url


# ═══════════════════════════════════════════════════════════
#  Source 1: Google News RSS
# ═══════════════════════════════════════════════════════════

def fetch_google_news_rss(query: str, max_results: int = 20,
                         resolve_urls: bool = False) -> list[dict]:
    """Fetch news from Google News RSS feed.

    Returns items with precise pubDate from RSS <pubDate> element.
    Google News returns up to 100 items per query — very high yield.

    When ``resolve_urls=True`` (default), Google News redirect URLs
    (``news.google.com/rss/articles/...``) are resolved to actual article
    URLs in a concurrent batch.  This adds ~2-3s but produces clickable
    links and enables URL-path date extraction.
    """
    results = []
    try:
        resp = requests.get(
            'https://news.google.com/rss/search',
            params={'q': query, 'hl': 'zh-CN', 'gl': 'CN', 'ceid': 'CN:zh-Hans'},
            headers=_HEADERS,
            timeout=15,
        )
        if not resp.ok:
            logger.warning('[GoogleNews] HTTP %d for query: %s', resp.status_code, query[:50])
            return results

        items_xml = re.findall(r'<item>(.*?)</item>', resp.text, re.DOTALL)
        for item_xml in items_xml[:max_results]:
            title_m = re.search(r'<title>(.*?)</title>', item_xml)
            # Google News uses <link/> followed by the URL as text node
            link_m = re.search(r'<link\s*/>\s*(https?://[^\s<]+)', item_xml)
            if not link_m:
                link_m = re.search(r'<link>(https?://[^\s<]+)</link>', item_xml)
            date_m = re.search(r'<pubDate>(.*?)</pubDate>', item_xml)
            source_m = re.search(r'<source[^>]*url="([^"]*)">(.*?)</source>', item_xml)
            desc_m = re.search(r'<description>(.*?)</description>', item_xml, re.DOTALL)

            title = _clean(title_m.group(1)) if title_m else ''
            if not title or len(title) < 4:
                continue

            url = link_m.group(1).strip() if link_m else ''
            if not url:
                continue
            if _is_blocked_url(url):
                continue

            snippet = ''
            if desc_m:
                snippet = _clean(desc_m.group(1))[:500]
            source_name = source_m.group(2) if source_m else 'Google News'

            # Parse pubDate (RFC 2822 format)
            published_date = ''
            published_at = ''
            if date_m:
                try:
                    dt = parsedate_to_datetime(date_m.group(1))
                    published_date = dt.strftime('%Y-%m-%d')
                    published_at = dt.strftime('%Y-%m-%d %H:%M:%S')
                except Exception as e:
                    logger.debug('[GoogleNews] Date parse failed: %s: %s', date_m.group(1), e, exc_info=True)

            results.append({
                'title': title,
                'snippet': snippet if snippet else title,
                'url': url,
                'source': source_name,
                'published_date': published_date,
                'published_at': published_at,
            })

        # ── Batch-resolve Google News redirect URLs to real article URLs ──
        if resolve_urls and results:
            gnews_indices = [i for i, r in enumerate(results)
                            if 'news.google.com' in r['url']]
            if gnews_indices:
                resolved = 0
                with ThreadPoolExecutor(max_workers=6) as pool:
                    future_map = {
                        pool.submit(_resolve_google_news_url, results[i]['url'], 4): i
                        for i in gnews_indices
                    }
                    for fut in as_completed(future_map, timeout=8):
                        idx = future_map[fut]
                        try:
                            real_url = fut.result()
                            if real_url != results[idx]['url']:
                                results[idx]['url'] = real_url
                                resolved += 1
                        except Exception as e:
                            logger.debug('[GoogleNews] URL resolve failed for index %d: %s', idx, e)  # keep original URL on failure
                if resolved:
                    logger.debug('[GoogleNews] Resolved %d/%d redirect URLs',
                                 resolved, len(gnews_indices))

            # Re-filter after resolution (resolved URL might be blocked)
            results = [r for r in results if not _is_blocked_url(r['url'])]

    except Exception as e:
        logger.error('[GoogleNews] Error for query %s: %s', query[:50], e, exc_info=True)

    return results


# ═══════════════════════════════════════════════════════════
#  Source 2: 财联社 (CLS) Telegraph
# ═══════════════════════════════════════════════════════════

# Mapping from INTEL_SOURCES categories to CLS relevance keywords
_CLS_CATEGORY_KEYWORDS = {
    'hot_news': ['ETF', '股市', 'A股', '涨停', '跌停', '行情', '大盘', '重磅', '突发', '快讯', '央行',
                 '证监会', '利好', '利空', '热点', '龙头', '暴涨', '暴跌', '成交', '外资', '北向'],
    'macro_policy': ['央行', '货币政策', '财政', 'GDP', 'CPI', 'PPI', '经济数据', '降准', '降息', 'MLF', 'LPR'],
    'market_trend': ['A股', '大盘', '沪指', '深成指', '涨跌', '行情', '收盘', '开盘', '成交额'],
    'sector_rotation': ['板块', '涨停', '概念股', '龙头', '资金流向', '主力', '热门'],
    'fund_flow': ['ETF', '净申购', '赎回', 'ETF', '北向资金', '南向资金', '外资'],
    'policy_regulation': ['证监会', '监管', 'ETF', '公募', '费率', '注册制'],
    'global_market': ['美股', '港股', '纳斯达克', '标普', '恒生', '原油', '黄金', '汇率', '美元'],
    'bond_rate': ['国债', '利率', '债券', '收益率', 'Shibor'],
    'fund_rating': ['投资经理', '调仓', '重仓', '持仓', '业绩', '排名'],
}


def fetch_cls_telegraph(category: str = '', max_results: int = 20) -> list[dict]:
    """Fetch real-time flash news from 财联社 (CLS) telegraph API.

    Returns items with precise timestamps from CLS's ctime field.
    Optionally filters by category keywords.
    """
    results = []
    try:
        resp = requests.get(
            'https://www.cls.cn/nodeapi/updateTelegraphList',
            params={'app': 'CailianpressWeb', 'os': 'web', 'sv': '7.7.5', 'rn': 50},
            headers={**_HEADERS, 'Referer': 'https://www.cls.cn/'},
            timeout=10,
        )
        if not resp.ok:
            logger.warning('[CLS] HTTP %d', resp.status_code)
            return results

        data = resp.json()
        roll_data = data.get('data', {}).get('roll_data', [])
        if not roll_data:
            return results

        # Get category filter keywords
        filter_kws = _CLS_CATEGORY_KEYWORDS.get(category, []) if category else []

        for item in roll_data:
            if not isinstance(item, dict):
                continue

            title = _clean(item.get('title', ''))
            content = _clean(item.get('content', '') or item.get('brief', ''))

            # CLS telegraphs often have no title — use content as both
            if not title and content:
                title = content[:80]
            if not title:
                continue

            # Category filtering — skip items that don't match any keyword
            if filter_kws:
                combined = f"{title} {content}".lower()
                if not any(kw.lower() in combined for kw in filter_kws):
                    continue

            # Parse unix timestamp
            published_date = ''
            published_at = ''
            ctime = item.get('ctime', 0)
            if ctime:
                try:
                    dt = datetime.fromtimestamp(int(ctime))
                    published_date = dt.strftime('%Y-%m-%d')
                    published_at = dt.strftime('%Y-%m-%d %H:%M:%S')
                except (ValueError, OSError) as exc:
                    logger.debug('[Sources] Failed to parse timestamp ctime=%s: %s', ctime, exc)

            # Build URL from ID
            item_id = item.get('id', '')
            url = f"https://www.cls.cn/detail/{item_id}" if item_id else ''

            if _is_blocked_url(url):
                continue

            results.append({
                'title': title[:200],
                'snippet': content[:500] if content else title,
                'url': url,
                'source': '财联社',
                'published_date': published_date,
                'published_at': published_at,
            })

            if len(results) >= max_results:
                break

    except Exception as e:
        logger.error('[CLS] Error: %s', e, exc_info=True)

    return results


# ═══════════════════════════════════════════════════════════
#  Source 3: DuckDuckGo with time filter
# ═══════════════════════════════════════════════════════════

def fetch_ddg_time_filtered(query: str, max_results: int = 8,
                            time_filter: str = 'w') -> list[dict]:
    """Search DuckDuckGo HTML with a time filter to get fresh results.

    Args:
        time_filter: 'd' (past day), 'w' (past week), 'm' (past month)
    """
    results = []
    try:
        resp = requests.get(
            'https://html.duckduckgo.com/html/',
            params={'q': query, 'df': time_filter},
            headers=_HEADERS,
            timeout=15,
        )
        if not resp.ok:
            return results

        blocks = resp.text.split('class="result results_links')
        link_re = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            re.DOTALL,
        )
        snip_re = re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        from urllib.parse import parse_qs, unquote, urlparse

        for block in blocks[1:]:
            if len(results) >= max_results:
                break

            lm = link_re.search(block)
            if not lm:
                continue

            raw_url = lm.group(1)
            title = _clean(lm.group(2))

            snippet = ''
            sm = snip_re.search(block)
            if sm:
                snippet = _clean(sm.group(1))

            # Skip DDG ad redirects — catch ALL /y.js variants (not just ad_ ones)
            if '/y.js' in raw_url or 'ad_provider' in raw_url or 'ad_domain' in raw_url:
                continue

            # Resolve DDG redirect URLs
            url = raw_url
            if 'uddg=' in raw_url:
                try:
                    url = unquote(parse_qs(urlparse(raw_url).query).get('uddg', [raw_url])[0])
                except Exception as e:
                    logger.debug('[DDG] URL unquote failed for %s: %s', raw_url[:80], e, exc_info=True)

            if not url.startswith('http'):
                continue
            if _is_blocked_url(url):
                continue

            results.append({
                'title': title[:200],
                'snippet': snippet[:500],
                'url': url,
                'source': 'DuckDuckGo',
            })

    except Exception as e:
        logger.error('[DDG-TF] Error for query %s: %s', query[:50], e, exc_info=True)

    return results


# ═══════════════════════════════════════════════════════════
#  Unified Multi-Source Search
# ═══════════════════════════════════════════════════════════

def multi_source_search(query: str, category: str = '',
                        max_results: int = 20,
                        include_cls: bool = True) -> list[dict]:
    """Fan out to all sources concurrently, merge & deduplicate.

    Priority:
      1. Google News RSS (highest — precise dates, many results)
      2. CLS Telegraph (real-time flash news)
      3. DDG time-filtered (past week — fresh results)

    Returns unified list sorted by publication date (newest first),
    with at most `max_results` items.
    """
    all_results = []

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}

        # Google News — main source
        futures[pool.submit(fetch_google_news_rss, query, max_results)] = 'GoogleNews'

        # DDG time-filtered (past week for freshness)
        futures[pool.submit(fetch_ddg_time_filtered, query, 8, 'w')] = 'DDG-Week'

        # CLS telegraph (category-filtered real-time news)
        if include_cls:
            futures[pool.submit(fetch_cls_telegraph, category, 15)] = 'CLS'

        for fut in as_completed(futures, timeout=25):
            source = futures[fut]
            try:
                items = fut.result()
                if items:
                    all_results.extend(items)
                    logger.debug('[MultiSource] %s returned %d items for "%s"',
                                 source, len(items), query[:40])
            except Exception as e:
                logger.warning('[MultiSource] %s failed for "%s": %s',
                               source, query[:40], e, exc_info=True)

    # ── Dedup by URL + SimHash content fingerprint ──
    from lib.trading.simhash import compute_simhash, hamming_distance

    seen_urls = set()
    seen_hashes = []   # list of (simhash, index) for fingerprint comparison
    unique = []
    for r in all_results:
        url_key = r['url'].lower().rstrip('/').replace('https://', '').replace('http://', '')[:150]
        if url_key in seen_urls:
            continue

        # SimHash dedup: catch same article syndicated across sources
        text = f"{r['title']} {r.get('snippet', '')}".strip()
        h = compute_simhash(text)
        is_dup = False
        if h != 0:
            for existing_h, _ in seen_hashes:
                if hamming_distance(h, existing_h) <= 6:
                    is_dup = True
                    break
        if is_dup:
            continue

        seen_urls.add(url_key)
        if h != 0:
            seen_hashes.append((h, len(unique)))
        unique.append(r)

    # ── Sort by publication date (newest first), items without dates go last ──
    def _sort_key(item):
        pd = item.get('published_date', '')
        if pd:
            return pd  # YYYY-MM-DD sorts lexicographically
        return '0000-00-00'  # no date → sort to end
    unique.sort(key=_sort_key, reverse=True)

    return unique[:max_results]
