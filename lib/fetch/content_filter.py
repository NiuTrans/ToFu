"""lib/fetch/content_filter.py — LLM-based web content relevance filtering and cleaning.

Uses a fast non-thinking LLM call to assess relevance and clean raw page text.
Step 1: Relevance verdict — pages irrelevant to the user's question are rejected
        instantly via stop token (minimal output tokens).
Step 2: Content cleaning — removes boilerplate (navigation, ads, sidebars, banners)
        while preserving ALL substantive content intact.

The filter does NOT summarize, interpret, or answer questions — that is the main
model's job.

Enable/disable via FETCH_LLM_FILTER env var (default: enabled).
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from lib.log import get_logger

logger = get_logger(__name__)

# ── Config ──
FILTER_ENABLED   = os.environ.get('FETCH_LLM_FILTER', '1') == '1'
FILTER_MIN_CHARS = int(os.environ.get('FETCH_FILTER_MIN_CHARS', '3000'))
FILTER_MODEL     = os.environ.get('FETCH_FILTER_MODEL', '')  # empty = dispatcher default
FILTER_TIMEOUT   = int(os.environ.get('FETCH_FILTER_TIMEOUT', '300'))

logger.info('[ContentFilter] module loaded — enabled=%s, min_chars=%d, model=%s, timeout=%ds',
            FILTER_ENABLED, FILTER_MIN_CHARS, FILTER_MODEL or '(auto)', FILTER_TIMEOUT)

# ── Stop token: generation halts instantly when the model outputs this ──
_IRRELEVANT_STOP = '§§IRRELEVANT§§'

_SYSTEM_PROMPT = """\
You are a web page content cleaner. Your ONLY job is to reformat raw extracted text \
into clean, readable content and remove obvious junk. You must NOT interpret, \
summarize, or answer any question — just clean the text.

You will be given context about the user's intent (when available). Use it ONLY to \
judge relevance in Step 1 — do NOT let it influence what content you keep or remove \
in Step 2.

## Step 1 — Relevance verdict (MANDATORY first line)

Output exactly ONE of these two tokens on the FIRST line:

- `[USEFUL]` — if the page contains substantive content (articles, docs, code, \
discussions, data, etc.).
- `§§IRRELEVANT§§` — if the page does NOT help answer the user's question. This includes: \
empty/broken pages (login wall, captcha, cookie wall, 404, access denied, blank page), \
AND pages whose content is entirely unrelated to or does not help answer the user's question. \
If the page contains ANY information that could help — even partially or indirectly — output [USEFUL]. \
Generation stops immediately after this token.

When in doubt, output [USEFUL]. Err on the side of keeping content.

## Step 2 — Content cleaning (only after [USEFUL])

**Your job: format optimization + junk removal. Keep everything else INTACT.**

**KEEP (preserve original wording, do not paraphrase or summarize):**
- ALL substantive text: articles, paragraphs, explanations, opinions, arguments
- ALL technical content: code, APIs, configs, commands, formulas, version strings
- ALL data: numbers, dates, names, URLs, tables, statistics, quotes
- ALL discussion content: questions, answers, comments with substance
- Document structure: headings, lists, sections — improve formatting if messy

**REMOVE (only these categories of junk):**
- Navigation menus, breadcrumbs, site headers/footers, sidebars
- Ads, promotions, "related articles", "you might also like", "trending now"
- Cookie/login/newsletter banners and popups
- Social sharing buttons, "follow us", "share this"
- Legal boilerplate (privacy policy links, copyright footers)
- Duplicate/repeated text blocks (e.g. same nav appearing multiple times)
- Pagination chrome ("page 1 of 5", "next →", "load more")

**NEVER do any of these:**
- Do NOT summarize or condense the content
- Do NOT answer questions based on the content
- Do NOT remove substantive content that relates to the user's question
- Do NOT add your own commentary or analysis
- Do NOT rewrite or paraphrase the author's words

Output the cleaned content directly after [USEFUL] — no preamble, no wrapper."""

# ── Sentinel returned by filter when LLM deems page irrelevant ──
IRRELEVANT_SENTINEL = '[IRRELEVANT]'


def filter_web_content(raw_text: str, *, url: str = '', query: str = '',
                       user_question: str = '',
                       timeout: int | None = None,
                       min_chars: int | None = None) -> str:
    """Filter noise from web page text using LLM (non-thinking mode).

    Args:
        raw_text: Raw extracted text from web page
        url: Source URL (for context)
        query: Search query used to find this page (model-generated keywords)
        user_question: The user's original question (true intent)
        timeout: Override timeout in seconds
        min_chars: Override minimum character threshold for filtering.
                   Set to 0 to force all documents through the LLM filter
                   (e.g. search pipeline sends short bot-protection pages).
                   None = use module-level FILTER_MIN_CHARS default.

    Returns:
        Filtered text, IRRELEVANT_SENTINEL if page is irrelevant,
        or original raw_text if filtering fails/is disabled/too short.
    """
    effective_min = FILTER_MIN_CHARS if min_chars is None else min_chars

    if not FILTER_ENABLED:
        logger.debug('[ContentFilter] SKIP (disabled) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    # PDF content is already structured text — skip LLM filtering
    if url and url.lower().rstrip('/').endswith('.pdf'):
        logger.debug('[ContentFilter] SKIP (PDF) url=%s len=%d', url[:80], len(raw_text))
        return raw_text

    if len(raw_text) < effective_min:
        logger.debug('[ContentFilter] SKIP (too short: %d < %d) url=%s',
                     len(raw_text), effective_min, url[:80])
        return raw_text

    from lib.llm_dispatch.api import dispatch_chat

    _timeout = timeout or FILTER_TIMEOUT
    logger.info('[ContentFilter] START url=%s raw_chars=%d query=%r user_question=%r timeout=%ds',
                url[:100], len(raw_text), query[:80] if query else '',
                user_question[:80] if user_question else '', _timeout)

    # Build the user message — include BOTH user question and search query
    user_parts = []
    if user_question:
        user_parts.append(f"User's original question: {user_question}")
    if query:
        user_parts.append(f"Search query: {query}")
    if url:
        user_parts.append(f"Source URL: {url}")
    user_parts.append(f"\n--- Raw page content ({len(raw_text):,} chars) ---\n{raw_text}")

    messages = [
        {'role': 'system', 'content': _SYSTEM_PROMPT},
        {'role': 'user', 'content': '\n'.join(user_parts)},
    ]

    t0 = time.time()
    try:
        prefer = FILTER_MODEL or None

        content, usage = dispatch_chat(
            messages,
            temperature=0,
            thinking_enabled=False,
            capability='cheap',
            prefer_model=prefer,
            max_retries=2,
            log_prefix='[ContentFilter]',
            timeout=_timeout,
            extra={'stop': [_IRRELEVANT_STOP]},
        )

        elapsed = time.time() - t0
        in_tok = usage.get('input_tokens', 0) if usage else 0
        out_tok = usage.get('output_tokens', 0) if usage else 0

        # ── Check if LLM flagged the page as irrelevant ──
        # The stop token halts generation immediately, so content will be
        # empty or just whitespace when the model outputs §§IRRELEVANT§§.
        # Also check for the old [IRRELEVANT] sentinel for compatibility.
        _stripped = (content or '').strip()
        if (not _stripped
                or _stripped == _IRRELEVANT_STOP
                or _stripped.startswith(IRRELEVANT_SENTINEL)
                or _stripped.startswith(_IRRELEVANT_STOP)):
            logger.info('[ContentFilter] IRRELEVANT url=%s  '
                        'query=%r user_question=%r  tokens in=%d out=%d  %.1fs',
                        url[:100], query[:60] if query else '',
                        user_question[:60] if user_question else '',
                        in_tok, out_tok, elapsed)
            return IRRELEVANT_SENTINEL

        # ── Strip the [USEFUL] verdict prefix if present ──
        if _stripped.startswith('[USEFUL]'):
            content = _stripped[len('[USEFUL]'):].lstrip('\n')

        if content and len(content) > 100:
            reduction = (1 - len(content) / len(raw_text)) * 100
            logger.info('[ContentFilter] DONE url=%s  %s → %s chars (%.0f%% reduction)  '
                        'tokens in=%d out=%d  %.1fs',
                        url[:100], f'{len(raw_text):,}', f'{len(content):,}',
                        reduction, in_tok, out_tok, elapsed)
            return content
        else:
            logger.warning('[ContentFilter] FAIL — LLM returned too-short content '
                           '(%d chars), falling back to raw  url=%s  tokens in=%d out=%d  %.1fs',
                           len(content) if content else 0, url[:100],
                           in_tok, out_tok, elapsed)
            return raw_text

    except Exception as e:
        elapsed = time.time() - t0

        # ── HTTP 450: content policy violation ──
        # The raw text itself contains content that the API gateway considers
        # "违规".  Returning raw_text here would just cause the same 450 when
        # the main LLM call sends it in the conversation messages.
        # Instead, return a short placeholder so the conversation isn't
        # interrupted.  Rule-based extraction already ran (raw_text IS the
        # extracted text); we just can't pass it through the LLM.
        from lib.llm_client import ContentFilterError
        if isinstance(e, ContentFilterError):
            logger.info('[ContentFilter] SKIP (content policy 450) after %.1fs — '
                        'url=%s raw_chars=%d. Using rule-based extraction only.',
                        elapsed, url[:100], len(raw_text))
            return (f'[Page content from {url} was filtered by content policy. '
                    f'The page contained {len(raw_text):,} characters of extracted text '
                    f'but could not be processed by the LLM content filter.]')

        logger.error('[ContentFilter] ERROR after %.1fs: %s  url=%s',
                     elapsed, str(e)[:300], url[:100], exc_info=True)
        return raw_text


def filter_web_contents_batch(items: list[tuple[str, str]], *,
                              query: str = '',
                              user_question: str = '',
                              timeout: int | None = None,
                              min_chars: int | None = None) -> dict[str, str]:
    """Filter multiple web pages in parallel — concurrency = len(items).

    Args:
        items: List of (url, raw_text) pairs
        query: Search query used to find these pages
        user_question: The user's original question (true intent)
        timeout: Per-call timeout
        min_chars: Override minimum character threshold for filtering.
                   Set to 0 to force all documents through the LLM filter.
                   None = use module-level FILTER_MIN_CHARS default.

    Returns:
        Dict mapping url → filtered_text (IRRELEVANT_SENTINEL for rejected pages)
    """
    effective_min = FILTER_MIN_CHARS if min_chars is None else min_chars

    batch_t0 = time.time()
    logger.info('[ContentFilter] BATCH start — %d items, min_chars=%d, query=%r',
                len(items), effective_min, query[:80] if query else '')

    if not FILTER_ENABLED:
        logger.info('[ContentFilter] BATCH skipped (filter disabled)')
        return {url: text for url, text in items}

    results = {}

    # Skip items that don't need filtering (too short / PDF)
    to_filter = []
    for url, text in items:
        if url and url.lower().rstrip('/').endswith('.pdf'):
            logger.debug('[ContentFilter] BATCH skip PDF url=%s len=%d', url[:80], len(text))
            results[url] = text
        elif len(text) < effective_min:
            logger.debug('[ContentFilter] BATCH skip short item url=%s len=%d (< %d)',
                         url[:80], len(text), effective_min)
            results[url] = text
        else:
            to_filter.append((url, text))

    if not to_filter:
        logger.info('[ContentFilter] BATCH done — nothing to filter (%d items all below %d chars)',
                    len(items), effective_min)
        return results

    total_raw_chars = sum(len(t) for _, t in to_filter)
    n_workers = len(to_filter)  # 并发打满，抓到几个就并发几个
    logger.info('[ContentFilter] BATCH filtering %d/%d items  total_raw=%s chars  workers=%d',
                len(to_filter), len(items), f'{total_raw_chars:,}', n_workers)

    # Filter in parallel — full concurrency (1 worker per document), no cap
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futures = {
            pool.submit(filter_web_content, text, url=url, query=query,
                        user_question=user_question, timeout=timeout,
                        min_chars=min_chars): url
            for url, text in to_filter
        }
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                results[url] = fut.result()
            except Exception as e:
                logger.error('[ContentFilter] BATCH item failed url=%s: %s',
                             url[:80], str(e)[:200], exc_info=True)
                # Fallback to raw text
                results[url] = dict(to_filter).get(url, '')

    batch_elapsed = time.time() - batch_t0
    total_filtered_chars = sum(len(results.get(u, '')) for u, _ in to_filter)
    overall_reduction = (1 - total_filtered_chars / total_raw_chars) * 100 if total_raw_chars else 0
    logger.info('[ContentFilter] BATCH done — %d items  %s → %s chars (%.0f%% reduction)  %.1fs total',
                len(to_filter), f'{total_raw_chars:,}', f'{total_filtered_chars:,}',
                overall_reduction, batch_elapsed)

    return results
