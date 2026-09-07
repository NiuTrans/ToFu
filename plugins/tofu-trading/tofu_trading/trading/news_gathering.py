"""lib/trading/news_gathering.py — Cached news gathering for trading decisions.

Provides ``gather_news_cached()`` — a 5-minute in-memory cache layer over
DB intel and (fallback) live web search.  Used by:
  - routes/trading_brain.py
  - routes/trading_autopilot.py
  - routes/trading_decision.py
  - routes/trading_tasks.py

Moved here from ``routes/trading_decision.py`` so that route modules
don't cross-import internal helpers from each other.
"""

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['gather_news_cached']


# ── 5-minute in-memory news cache ──
_news_cache: dict = {'items': [], 'ts': 0}


def _gather_news_from_db():
    """PRIMARY: gather recent news from DB intel cache."""
    try:
        from tofu_trading.identity import DEFAULT_OWNER_ID
        from tofu_trading.storage import DOMAIN_TRADING, get_thread_db

        db = get_thread_db(
            DOMAIN_TRADING, owner_user_id=DEFAULT_OWNER_ID)
        rows = db.execute(
            "SELECT title, summary, category, source_url, fetched_at "
            "FROM trading_intel_cache ORDER BY fetched_at DESC LIMIT 60"
        ).fetchall()
        items = []
        for r in rows:
            d = dict(r)
            items.append({
                'title': d.get('title', ''),
                'snippet': d.get('summary', '')[:200],
                'url': d.get('source_url', ''),
                'category': d.get('category', ''),
            })
        if items:
            logger.debug('[Trading] gathered %d news items from DB intel cache', len(items))
        return items
    except Exception as e:
        logger.warning('[Trading] DB intel gather error: %s', e, exc_info=True)
        return []


def gather_news_cached():
    """Gather market news: DB first (instant), live search fallback.

    Returns a list of news item dicts with keys: title, snippet, url, category/source.
    Results are cached for 5 minutes to avoid redundant DB/network calls when
    multiple endpoints trigger in quick succession.
    """
    import time as _t
    if _news_cache['items'] and (_t.time() - _news_cache['ts']) < 300:
        return _news_cache['items']
    items = _gather_news_from_db()
    if len(items) < 5:
        try:
            from tofu_trading.trading import _check_external_network
            if _check_external_network():
                from concurrent.futures import ThreadPoolExecutor, as_completed

                from tofu_search import perform_web_search
                queries = ['A股ETF和股票市场', '投资市场动态', '宏观经济']
                with ThreadPoolExecutor(max_workers=3) as executor:
                    futs = {executor.submit(perform_web_search, q, 3): q for q in queries}
                    for fut in as_completed(futs, timeout=5):
                        try:
                            for r in fut.result():
                                items.append({
                                    'title': r.get('title', ''),
                                    'snippet': r.get('snippet', ''),
                                    'url': r.get('url', ''),
                                    'source': futs[fut]
                                })
                        except Exception as e:
                            logger.warning('Web search future failed for news gathering: %s', e, exc_info=True)
        except Exception as e:
            logger.warning('Live news search fallback failed (non-critical): %s', e, exc_info=True)
    _news_cache['items'] = items
    _news_cache['ts'] = _t.time()
    return items
