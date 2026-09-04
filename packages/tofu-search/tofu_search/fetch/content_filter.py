"""tofu_search.fetch.content_filter — LLM-based web content relevance filtering.

Standalone version — uses tofu_search.llm_adapter instead of chatui's dispatch_chat.
When no LLM is configured, filtering is skipped (raw text returned as-is; the
skip is logged at debug).

Two modes (config.filter_mode):
  * 'gate'    — verdict ONLY: the LLM reads the head of the page and answers
                [USEFUL] / §§IRRELEVANT§§; useful pages keep their ORIGINAL
                text. A handful of output tokens — fast.
  * 'rewrite' — the LLM regenerates the whole cleaned page (pre-0.6 behaviour):
                best cleaning quality, but generation time ≈ page length.

Results are cached (config.filter_cache_ttl/max_size) keyed on
(mode, url, query, user_question, raw_text).
"""

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from tofu_search.config import get_config
from tofu_search.fetch.utils import _FetchCache
from tofu_search.log import get_logger
from tofu_search.passages import select_relevant_passages

logger = get_logger(__name__)

_IRRELEVANT_STOP = '§§IRRELEVANT§§'
IRRELEVANT_SENTINEL = '[IRRELEVANT]'

# Cache value marker for a gate-mode USEFUL verdict — the cached entry carries
# no page text (the caller already HAS the raw text), just the verdict.
_CACHE_GATE_USEFUL = '[USEFUL]'

_GATE_SYSTEM_PROMPT = """\
You are a web page relevance judge. Your ONLY job is to decide whether a page \
is worth reading for the user's intent. You must NOT clean, summarize, or \
answer — only judge.

Output exactly ONE of these two tokens as your ENTIRE response:

- `[USEFUL]` -- if the page contains substantive content (articles, docs, code, \
discussions, data, etc.) that could help -- even partially or indirectly.
- `§§IRRELEVANT§§` -- if the page does NOT help answer the user's question. This includes: \
empty/broken pages (login wall, captcha, cookie wall, 404, access denied, blank page), \
AND pages whose content is entirely unrelated to the user's question. \
Generation stops immediately after this token.

The page opening and query-focused excerpts are shown. Judge from what you see: \
a page whose opening is navigation chrome, an error, or a login wall with no substance is \
§§IRRELEVANT§§; a page that opens like a real article is [USEFUL] even if the \
shown part is only the beginning.

When in doubt, output [USEFUL]. Err on the side of keeping content."""

_SYSTEM_PROMPT = """\
You are a web page content cleaner. Your ONLY job is to reformat raw extracted text \
into clean, readable content and remove obvious junk. You must NOT interpret, \
summarize, or answer any question -- just clean the text.

You will be given context about the user's intent (when available). Use it ONLY to \
judge relevance in Step 1 -- do NOT let it influence what content you keep or remove \
in Step 2.

## Step 1 -- Relevance verdict (MANDATORY first line)

Output exactly ONE of these two tokens on the FIRST line:

- `[USEFUL]` -- if the page contains substantive content (articles, docs, code, \
discussions, data, etc.).
- `§§IRRELEVANT§§` -- if the page does NOT help answer the user's question. This includes: \
empty/broken pages (login wall, captcha, cookie wall, 404, access denied, blank page), \
AND pages whose content is entirely unrelated to or does not help answer the user's question. \
If the page contains ANY information that could help -- even partially or indirectly -- output [USEFUL]. \
Generation stops immediately after this token.

When in doubt, output [USEFUL]. Err on the side of keeping content.

## Step 2 -- Content cleaning (only after [USEFUL])

**Your job: format optimization + junk removal. Keep everything else INTACT.**

**KEEP (preserve original wording, do not paraphrase or summarize):**
- ALL substantive text: articles, paragraphs, explanations, opinions, arguments
- ALL technical content: code, APIs, configs, commands, formulas, version strings
- ALL data: numbers, dates, names, URLs, tables, statistics, quotes
- ALL discussion content: questions, answers, comments with substance
- Document structure: headings, lists, sections -- improve formatting if messy

**REMOVE (only these categories of junk):**
- Navigation menus, breadcrumbs, site headers/footers, sidebars
- Ads, promotions, "related articles", "you might also like", "trending now"
- Cookie/login/newsletter banners and popups
- Social sharing buttons, "follow us", "share this"
- Legal boilerplate (privacy policy links, copyright footers)
- Duplicate/repeated text blocks
- Pagination chrome ("page 1 of 5", "next", "load more")

**NEVER do any of these:**
- Do NOT summarize or condense the content
- Do NOT answer questions based on the content
- Do NOT remove substantive content that relates to the user's question
- Do NOT add your own commentary or analysis
- Do NOT rewrite or paraphrase the author's words

Output the cleaned content directly after [USEFUL] -- no preamble, no wrapper."""


# ── Result cache (mirrors fetch/utils.py's _fetch_cache pattern) ──
# The instance is built lazily from the config in effect at first use, and
# rebuilt if a reconfigure changes ttl/max_size. filter_cache_ttl <= 0 (or
# max_size <= 0) disables the cache entirely.
_filter_cache = None
_filter_cache_params = (None, None)
_filter_cache_lock = threading.Lock()


def _get_filter_cache(config):
    """Return the shared result cache, or None when disabled by config."""
    global _filter_cache, _filter_cache_params
    ttl = getattr(config, 'filter_cache_ttl', 600)
    size = getattr(config, 'filter_cache_max_size', 500)
    if ttl <= 0 or size <= 0:
        return None
    if _filter_cache is None or _filter_cache_params != (ttl, size):
        with _filter_cache_lock:
            if _filter_cache is None or _filter_cache_params != (ttl, size):
                _filter_cache = _FetchCache(ttl=ttl, max_size=size, name='Filter')
                _filter_cache_params = (ttl, size)
    return _filter_cache


def _reset_filter_cache():
    """Drop the shared cache instance (tests, benchmarks)."""
    global _filter_cache, _filter_cache_params
    with _filter_cache_lock:
        _filter_cache = None
        _filter_cache_params = (None, None)


def _cache_key(mode: str, url: str, query: str, user_question: str, raw_text: str) -> str:
    h = hashlib.sha1()
    for part in (mode, url, query, user_question, raw_text):
        h.update(part.encode('utf-8', errors='replace'))
        h.update(b'\x00')
    return h.hexdigest()


def filter_web_content(raw_text: str, *, url: str = '', query: str = '',
                       user_question: str = '',
                       timeout: int | None = None,
                       min_chars: int | None = None,
                       config=None) -> str:
    """Filter noise from web page text using LLM.

    Args:
        raw_text: Raw extracted text from web page.
        url: Source URL (for context).
        query: Search query used to find this page.
        user_question: The user's original question (true intent).
        timeout: Override timeout in seconds.
        min_chars: Override minimum character threshold. Set to 0 to
                   force all documents through the LLM filter.
        config: Optional SearchConfig override.

    Returns:
        Filtered text (rewrite mode) or the ORIGINAL raw_text (gate mode,
        useful verdict), IRRELEVANT_SENTINEL if page is irrelevant, or
        original raw_text if filtering fails/is disabled/no LLM configured.
    """
    if config is None:
        config = get_config()

    effective_min = config.filter_min_chars if min_chars is None else min_chars

    if not config.filter_enabled:
        logger.debug('[ContentFilter] SKIP (disabled) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    # No LLM configured — gracefully skip
    if not config.has_llm():
        logger.debug('[ContentFilter] SKIP (no LLM configured) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    # PDF content is already structured — skip
    if url and url.lower().rstrip('/').endswith('.pdf'):
        logger.debug('[ContentFilter] SKIP (PDF) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    if len(raw_text) < effective_min:
        logger.debug('[ContentFilter] SKIP (too short: %d < %d) url=%s',
                     len(raw_text), effective_min, url[:80])
        return raw_text

    mode = (getattr(config, 'filter_mode', 'gate') or 'gate').strip().lower()
    if mode not in ('gate', 'rewrite'):
        logger.warning('[ContentFilter] unknown filter_mode=%r, falling back to gate', mode)
        mode = 'gate'

    cache = _get_filter_cache(config)
    cache_key = _cache_key(mode, url, query, user_question, raw_text)
    if cache is not None:
        hit = cache.get(cache_key)
        if hit is not None:
            if hit == IRRELEVANT_SENTINEL:
                logger.debug('[ContentFilter] CACHE-HIT (irrelevant) url=%s', url[:80])
                return IRRELEVANT_SENTINEL
            if hit == _CACHE_GATE_USEFUL:
                logger.debug('[ContentFilter] CACHE-HIT (gate-useful) url=%s', url[:80])
                return raw_text
            logger.debug('[ContentFilter] CACHE-HIT (cleaned, %s chars) url=%s',
                         f'{len(hit):,}', url[:80])
            return hit

    from tofu_search.llm_adapter import call_llm

    _timeout = timeout or config.filter_timeout

    if mode == 'gate':
        # Keep the opening (page identity / login wall) plus query-focused
        # excerpts. This uses the same input budget as the old head-only gate,
        # but no longer rejects a long page whose useful section appears later.
        page_input = select_relevant_passages(
            raw_text,
            ' '.join(part for part in (query, user_question) if part),
            config.gate_input_max_chars,
            include_lead=True,
        )
        system_prompt = _GATE_SYSTEM_PROMPT
        content_header = (f'--- Raw page content '
                          f'(selected {len(page_input):,} of {len(raw_text):,} chars) ---')
    else:
        page_input = raw_text
        system_prompt = _SYSTEM_PROMPT
        content_header = f'--- Raw page content ({len(raw_text):,} chars) ---'

    logger.info('[ContentFilter] START mode=%s url=%s raw_chars=%d sent_chars=%d query=%r timeout=%ds',
                mode, url[:100], len(raw_text), len(page_input),
                query[:80] if query else '', _timeout)

    user_parts = []
    if user_question:
        user_parts.append(f"User's original question: {user_question}")
    if query:
        user_parts.append(f"Search query: {query}")
    if url:
        user_parts.append(f"Source URL: {url}")
    user_parts.append(f"{content_header}\n{page_input}")

    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': '\n'.join(user_parts)},
    ]

    t0 = time.time()
    try:
        content = call_llm(
            messages,
            config=config,
            temperature=0,
            stop=[_IRRELEVANT_STOP],
            timeout=_timeout,
        )

        elapsed = time.time() - t0

        _stripped = (content or '').strip()
        if not _stripped:
            # An empty completion is an ANOMALY (rate-limited empty, content-
            # policy refusal, gateway truncation), NOT a relevance verdict —
            # fail open like the exception path: serve the raw text and do NOT
            # cache, so a transient hiccup neither drops a good page from the
            # results nor poisons the cache for the TTL.
            logger.warning('[ContentFilter] EMPTY response (anomaly, serving raw) mode=%s url=%s %.1fs',
                           mode, url[:100], elapsed)
            return raw_text
        if (_stripped == _IRRELEVANT_STOP
                or _stripped.startswith(IRRELEVANT_SENTINEL)
                or _stripped.startswith(_IRRELEVANT_STOP)):
            logger.info('[ContentFilter] IRRELEVANT mode=%s url=%s query=%r %.1fs',
                        mode, url[:100], query[:60] if query else '', elapsed)
            if cache is not None:
                cache.put(cache_key, IRRELEVANT_SENTINEL)
            return IRRELEVANT_SENTINEL

        if mode == 'gate':
            # Verdict-only mode: anything that isn't an irrelevance verdict
            # keeps the page. The LLM output (a handful of tokens) is
            # discarded — the caller gets the ORIGINAL extracted text.
            logger.info('[ContentFilter] GATE-USEFUL url=%s raw=%s chars %.1fs',
                        url[:100], f'{len(raw_text):,}', elapsed)
            if cache is not None:
                cache.put(cache_key, _CACHE_GATE_USEFUL)
            return raw_text

        if _stripped.startswith('[USEFUL]'):
            content = _stripped[len('[USEFUL]'):].lstrip('\n')

        if content and len(content) > 100:
            reduction = (1 - len(content) / len(raw_text)) * 100
            logger.info('[ContentFilter] DONE mode=rewrite url=%s %s -> %s chars (%.0f%% reduction) %.1fs',
                        url[:100], f'{len(raw_text):,}', f'{len(content):,}',
                        reduction, elapsed)
            if cache is not None:
                cache.put(cache_key, content)
            return content
        else:
            logger.warning('[ContentFilter] FAIL — LLM returned too-short (%d chars), using raw  url=%s',
                           len(content) if content else 0, url[:100])
            return raw_text

    except Exception as e:
        elapsed = time.time() - t0
        logger.error('[ContentFilter] ERROR after %.1fs: %s  url=%s',
                     elapsed, str(e)[:300], url[:100], exc_info=True)
        return raw_text


def filter_web_contents_batch(items: list[tuple[str, str]], *,
                              query: str = '',
                              user_question: str = '',
                              timeout: int | None = None,
                              min_chars: int | None = None,
                              config=None) -> dict[str, str]:
    """Filter multiple web pages in parallel."""
    if config is None:
        config = get_config()

    effective_min = config.filter_min_chars if min_chars is None else min_chars

    if not config.filter_enabled or not config.has_llm():
        return {url: text for url, text in items}

    results = {}
    to_filter = []
    for url, text in items:
        if url and url.lower().rstrip('/').endswith('.pdf'):
            results[url] = text
        elif len(text) < effective_min:
            results[url] = text
        else:
            to_filter.append((url, text))

    if not to_filter:
        return results

    # Cap concurrency: an unbounded pool fans out one LLM call per page,
    # which can mean dozens of simultaneous requests against the LLM endpoint
    # (rate-limit / cost / connection-pool exhaustion). 8 is plenty to hide
    # per-call latency without hammering the backend.
    n_workers = min(len(to_filter), 8)
    logger.info('[ContentFilter] BATCH filtering %d/%d items  workers=%d  mode=%s',
                len(to_filter), len(items), n_workers,
                getattr(config, 'filter_mode', 'gate'))

    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(filter_web_content, text, url=url, query=query,
                        user_question=user_question, timeout=timeout,
                        min_chars=min_chars, config=config): url
            for url, text in to_filter
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as e:
                logger.error('[ContentFilter] BATCH item failed url=%s: %s',
                             url[:80], str(e)[:200], exc_info=True)
                results[url] = dict(to_filter).get(url, '')

    return results
