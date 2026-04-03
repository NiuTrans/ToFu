#!/usr/bin/env python3
"""probe_450_filter.py — Systematically identify content that triggers HTTP 450.

Strategy:
  1. Pull actual articles from DB that have failed or are likely to fail
  2. Binary-search each article's text to find the minimal triggering substring
  3. Output a report of all discovered trigger phrases

Usage:
    python debug/probe_450_filter.py                    # auto-extract from DB
    python debug/probe_450_filter.py --text "some text"  # test a specific string
    python debug/probe_450_filter.py --file input.txt    # test content from file
"""

import sys, os, time, json, argparse, re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import LLM_API_KEY, LLM_BASE_URL

# ── Minimal probe — direct HTTP, no dispatch overhead ──
import requests

API_URL = f'{LLM_BASE_URL}/chat/completions'
HEADERS = {
    'Content-Type': 'application/json',
    'Authorization': f'Bearer {LLM_API_KEY}',
}
PROBE_MODEL = 'qwen3.6-plus'  # cheapest/fastest model
SLEEP = 0.3  # seconds between requests to avoid 429

_call_count = 0


def probe(text: str) -> bool:
    """Send text to the API. Returns True if it triggers 450, False if it passes."""
    global _call_count
    body = {
        'model': PROBE_MODEL,
        'messages': [{'role': 'user', 'content': text}],
        'max_tokens': 1,  # we don't care about the response
        'temperature': 0,
    }
    time.sleep(SLEEP)
    _call_count += 1
    try:
        resp = requests.post(API_URL, headers=HEADERS, json=body, timeout=30)
        if resp.status_code == 450:
            return True
        if resp.status_code == 429:
            print('  [429 rate limit — waiting 5s]')
            time.sleep(5)
            return probe(text)  # retry
        return False
    except Exception as e:
        print(f'  [Request error: {e}]')
        return False


def binary_search_trigger(text: str, min_len: int = 10) -> list[str]:
    """Find all minimal triggering substrings in text using binary search.
    
    Returns a list of minimal substrings that each independently trigger 450.
    """
    if not probe(text):
        return []  # whole text doesn't trigger — nothing to find
    
    print(f'  ✗ Full text ({len(text)} chars) triggers 450. Binary searching...')
    
    triggers = []
    _find_triggers(text, 0, len(text), min_len, triggers)
    return triggers


def _find_triggers(text: str, start: int, end: int, min_len: int, results: list):
    """Recursively binary-search for minimal triggering substrings."""
    chunk = text[start:end]
    if len(chunk) < min_len:
        # Too short to split further — this is a minimal trigger
        results.append(chunk)
        return
    
    mid = (start + end) // 2
    
    # Test left half
    left = text[start:mid]
    left_triggers = False
    if len(left) >= min_len:
        left_triggers = probe(left)
        if left_triggers:
            print(f'    ← Left half ({start}:{mid}, {len(left)} chars) triggers')
            _find_triggers(text, start, mid, min_len, results)
    
    # Test right half
    right = text[mid:end]
    right_triggers = False
    if len(right) >= min_len:
        right_triggers = probe(right)
        if right_triggers:
            print(f'    → Right half ({mid}:{end}, {len(right)} chars) triggers')
            _find_triggers(text, mid, end, min_len, results)
    
    # If neither half triggers alone, the trigger spans the boundary
    if not left_triggers and not right_triggers:
        # Expand window around boundary to find the cross-boundary trigger
        _search_boundary(text, start, end, mid, min_len, results)


def _search_boundary(text: str, start: int, end: int, mid: int, min_len: int, results: list):
    """Find trigger that spans the midpoint by expanding a window."""
    # Start with a small window around mid and expand
    for window in [50, 100, 200, 400, 800]:
        w_start = max(start, mid - window)
        w_end = min(end, mid + window)
        chunk = text[w_start:w_end]
        if len(chunk) < min_len:
            continue
        if probe(chunk):
            print(f'    ⊕ Boundary window ({w_start}:{w_end}, {len(chunk)} chars) triggers')
            if len(chunk) <= 100:
                results.append(chunk)
                return
            # Recurse to narrow down further
            _find_triggers(text, w_start, w_end, min_len, results)
            return
    
    # Couldn't isolate — record the whole span
    results.append(text[start:end])


def extract_articles_from_db() -> list[dict]:
    """Pull articles from the intel DB that are likely to trigger 450."""
    from lib.database import get_thread_db, DOMAIN_TRADING
    db = get_thread_db(DOMAIN_TRADING)
    
    # Get articles with neutral/failed analysis (likely 450 victims)
    rows = db.execute("""
        SELECT id, title, raw_content, summary, category, source_url, analysis
        FROM trading_intel_cache
        WHERE raw_content IS NOT NULL AND raw_content != ''
        ORDER BY fetched_at DESC
        LIMIT 100
    """).fetchall()
    
    return [dict(r) for r in rows]


def build_intel_prompt(title: str, content: str, category: str) -> str:
    """Replicate the exact prompt used in _auto_analyze_new_intel."""
    content_preview = content[:1500]
    return f"""你是一位资深金融分析师。请对以下新闻/情报进行快速分析。

标题: {title}
内容: {content_preview}
分类: {category}

请严格按JSON格式回复（不要markdown标记）：
{{"sentiment": "bullish/bearish/neutral", "sentiment_label": "一句话原因(≤20字)", "impact_summary": "对投资交易的影响(2句话)", "affected_sectors": ["板块1"], "relevance_score": 0.0到1.0, "risk_level": "low/medium/high", "action_suggestion": "操作建议"}}"""


def main():
    parser = argparse.ArgumentParser(description='Probe HTTP 450 content filter')
    parser.add_argument('--text', help='Test a specific string')
    parser.add_argument('--file', help='Test content from a file')
    parser.add_argument('--db', action='store_true', help='Scan articles from DB (default)')
    parser.add_argument('--limit', type=int, default=50, help='Max articles to scan')
    parser.add_argument('--min-len', type=int, default=20, help='Min trigger length for binary search')
    args = parser.parse_args()

    all_triggers = []

    if args.text:
        print(f'\n=== Testing provided text ({len(args.text)} chars) ===')
        triggers = binary_search_trigger(args.text, args.min_len)
        if triggers:
            all_triggers.extend(triggers)
        else:
            print('  ✓ Text does NOT trigger 450')

    elif args.file:
        with open(args.file, 'r') as f:
            text = f.read()
        print(f'\n=== Testing file content ({len(text)} chars) ===')
        triggers = binary_search_trigger(text, args.min_len)
        if triggers:
            all_triggers.extend(triggers)
        else:
            print('  ✓ File content does NOT trigger 450')

    else:
        # Default: scan DB articles
        print('Extracting articles from DB...')
        articles = extract_articles_from_db()
        print(f'Found {len(articles)} articles. Scanning up to {args.limit}...\n')

        blocked_articles = []
        passed_articles = []

        for i, art in enumerate(articles[:args.limit]):
            title = art['title'] or ''
            content = art.get('raw_content', '') or art.get('summary', '') or ''
            if not content:
                continue

            # Phase 1: Test the full prompt (as sent to LLM)
            prompt = build_intel_prompt(title, content, art.get('category', ''))
            print(f'[{i+1}/{min(len(articles), args.limit)}] "{title[:60]}..." ({len(content)} chars)')

            if probe(prompt):
                print(f'  ✗ BLOCKED (full prompt)')
                blocked_articles.append(art)

                # Phase 2: Is it the content or the title?
                if probe(content[:1500]):
                    print(f'  ✗ Content alone triggers 450')
                    triggers = binary_search_trigger(content[:1500], args.min_len)
                    for t in triggers:
                        all_triggers.append({'source': title[:80], 'trigger': t})
                elif probe(title):
                    print(f'  ✗ Title alone triggers 450')
                    all_triggers.append({'source': 'title', 'trigger': title})
                else:
                    print(f'  ⊕ Neither title nor content alone — combination trigger')
                    all_triggers.append({'source': title[:80], 'trigger': '[combination of title + content]', 'prompt_len': len(prompt)})
            else:
                passed_articles.append(art)
                print(f'  ✓ OK')

        print(f'\n{"="*60}')
        print(f'SCAN COMPLETE')
        print(f'  Total articles scanned: {min(len(articles), args.limit)}')
        print(f'  Blocked: {len(blocked_articles)}')
        print(f'  Passed:  {len(passed_articles)}')
        print(f'  API calls made: {_call_count}')

    # ── Report ──
    if all_triggers:
        print(f'\n{"="*60}')
        print(f'DISCOVERED TRIGGERS ({len(all_triggers)}):')
        print(f'{"="*60}')
        for i, t in enumerate(all_triggers, 1):
            if isinstance(t, dict):
                print(f'\n  [{i}] Source: {t["source"]}')
                print(f'      Trigger: {t["trigger"][:200]}')
            else:
                print(f'\n  [{i}] {t[:200]}')
        
        # Save to file
        out_path = 'debug/450_triggers.json'
        with open(out_path, 'w') as f:
            json.dump(all_triggers, f, ensure_ascii=False, indent=2)
        print(f'\nFull results saved to {out_path}')
    else:
        print('\nNo triggers found!')


if __name__ == '__main__':
    main()
