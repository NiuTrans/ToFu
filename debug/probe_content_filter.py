#!/usr/bin/env python3
"""probe_content_filter.py — Reverse-engineer the gateway's HTTP 450 content filter.

Strategy:
  1. SENTENCE SCAN: split text → sentences, test each → find flagged sentences
  2. BINARY SEARCH: within each flagged sentence, binary-split to isolate minimal trigger
  3. SINGLE-WORD SCAN: test individual words to find atomic triggers
  4. PHRASE SCAN: test adjacent word-pairs for combination triggers

Uses the cheapest model (gemini-3.1-flash-lite-preview, $0.001/req) to keep costs near zero.

Usage:
    # Test a specific text string
    python debug/probe_content_filter.py --text "要测试的中文内容"

    # Test content from a file
    python debug/probe_content_filter.py --file /path/to/article.txt

    # Scan a list of known suspicious words/phrases (one per line)
    python debug/probe_content_filter.py --wordlist /path/to/wordlist.txt

    # Use a built-in set of common Chinese sensitive terms to scan
    python debug/probe_content_filter.py --builtin

    # Custom model (default: gemini-3.1-flash-lite-preview)
    python debug/probe_content_filter.py --text "..." --model qwen3.5-plus
"""

import sys, os, re, time, json, argparse, textwrap
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from lib import LLM_API_KEY, LLM_BASE_URL

# ── Config ──
DEFAULT_MODEL = 'gemini-3.1-flash-lite-preview'  # cheapest, ~$0.001/call
REQUEST_TIMEOUT = (10, 30)
SLEEP_BETWEEN = [0.3]     # mutable container so we can update from main() without `global`
RESULTS_FILE = 'debug/filter_results.json'

# ── Stats ──
_stats = {'calls': 0, 'blocked': 0, 'passed': 0, 'errors': 0}


def _no_proxy():
    return {'no_proxy': '*'}


def test_content(text: str, model: str = DEFAULT_MODEL) -> bool:
    """Send text to the gateway. Returns True if BLOCKED (HTTP 450), False if passed.
    
    Raises RuntimeError on unexpected errors (not 200, not 450).
    """
    url = f'{LLM_BASE_URL.rstrip("/")}/chat/completions'
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {LLM_API_KEY}',
    }
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': text}],
        'max_tokens': 5,       # minimal — we only care about the status code
        'temperature': 0,
        'stream': False,
    }
    _stats['calls'] += 1
    try:
        resp = requests.post(url, headers=headers, json=body,
                             timeout=REQUEST_TIMEOUT, proxies=_no_proxy())
        if resp.status_code == 450:
            _stats['blocked'] += 1
            return True
        if resp.status_code == 200:
            _stats['passed'] += 1
            return False
        if resp.status_code == 429:
            # Rate limited — wait and retry
            print(f'    ⏳ Rate limited (429), sleeping 5s...')
            time.sleep(5)
            return test_content(text, model)  # retry
        # Unexpected error
        _stats['errors'] += 1
        print(f'    ⚠️  Unexpected HTTP {resp.status_code}: {resp.text[:200]}')
        raise RuntimeError(f'HTTP {resp.status_code}')
    except requests.RequestException as e:
        _stats['errors'] += 1
        print(f'    ⚠️  Request error: {e}')
        raise


def split_sentences(text: str) -> list[str]:
    """Split Chinese/English text into sentences."""
    # Split on Chinese sentence-ending punctuation, newlines, or English periods
    parts = re.split(r'(?<=[。！？；\n.!?;])\s*', text)
    return [s.strip() for s in parts if s.strip()]


def split_words(text: str) -> list[str]:
    """Split text into words/characters. For Chinese, each char is a 'word'."""
    # Mix of Chinese chars and English words
    tokens = re.findall(r'[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]|[a-zA-Z0-9]+|[^\s]', text)
    return tokens


# ══════════════════════════════════════════════════════════
#  Phase 1: Sentence-level scan
# ══════════════════════════════════════════════════════════

def scan_sentences(text: str, model: str) -> list[str]:
    """Test each sentence individually. Return list of blocked sentences."""
    sentences = split_sentences(text)
    if not sentences:
        return []
    
    print(f'\n{"="*60}')
    print(f'Phase 1: Sentence Scan ({len(sentences)} sentences)')
    print(f'{"="*60}')
    
    blocked = []
    for i, sent in enumerate(sentences):
        if len(sent) < 2:
            continue
        time.sleep(SLEEP_BETWEEN[0])
        try:
            is_blocked = test_content(sent, model)
            status = '🚫 BLOCKED' if is_blocked else '✅ OK'
            print(f'  [{i+1:3d}/{len(sentences)}] {status}  {sent[:80]}')
            if is_blocked:
                blocked.append(sent)
        except Exception:
            print(f'  [{i+1:3d}/{len(sentences)}] ❓ ERROR  {sent[:80]}')
    
    print(f'\n  → {len(blocked)}/{len(sentences)} sentences blocked')
    return blocked


# ══════════════════════════════════════════════════════════
#  Phase 2: Binary search within a sentence
# ══════════════════════════════════════════════════════════

def binary_search_trigger(text: str, model: str, depth: int = 0) -> list[str]:
    """Binary-search to find the minimal substring that triggers the filter.
    
    Returns a list of minimal trigger fragments.
    """
    indent = '    ' + '  ' * depth
    words = split_words(text)
    
    if len(words) <= 1:
        # Single token — this IS the trigger
        return [text]
    
    # Test if the full text still triggers
    time.sleep(SLEEP_BETWEEN[0])
    if not test_content(text, model):
        return []  # no longer triggers (context-dependent)
    
    # Split in half
    mid = len(words) // 2
    left_text = ''.join(words[:mid])
    right_text = ''.join(words[mid:])
    
    triggers = []
    
    # Test left half
    time.sleep(SLEEP_BETWEEN[0])
    left_blocked = test_content(left_text, model) if len(left_text) >= 2 else False
    if left_blocked:
        print(f'{indent}🔍 LEFT blocked ({len(words[:mid])} tokens): {left_text[:60]}')
        triggers.extend(binary_search_trigger(left_text, model, depth + 1))
    
    # Test right half
    time.sleep(SLEEP_BETWEEN[0])
    right_blocked = test_content(right_text, model) if len(right_text) >= 2 else False
    if right_blocked:
        print(f'{indent}🔍 RIGHT blocked ({len(words[mid:])} tokens): {right_text[:60]}')
        triggers.extend(binary_search_trigger(right_text, model, depth + 1))
    
    # If neither half triggers alone, it's a combination trigger (both halves needed)
    if not left_blocked and not right_blocked:
        print(f'{indent}⚡ COMBINATION trigger (neither half blocked alone): {text[:80]}')
        # Try sliding window to find the minimal combination
        triggers.extend(_sliding_window_search(words, model, depth))
    
    return triggers


def _sliding_window_search(words: list[str], model: str, depth: int) -> list[str]:
    """When binary search fails (combination trigger), use sliding window."""
    indent = '    ' + '  ' * (depth + 1)
    best_trigger = ''.join(words)  # fallback: full text
    
    # Try progressively smaller windows
    for window_size in range(2, min(len(words), 10)):
        found = False
        for start in range(len(words) - window_size + 1):
            fragment = ''.join(words[start:start + window_size])
            time.sleep(SLEEP_BETWEEN[0])
            if test_content(fragment, model):
                print(f'{indent}🎯 Window({window_size}) hit: {fragment}')
                best_trigger = fragment
                found = True
                break
        if found:
            # Found a smaller trigger — try to shrink further
            if window_size <= 3:
                return [best_trigger]
            # Recursively binary search within this window
            return binary_search_trigger(best_trigger, model, depth + 1)
    
    return [best_trigger]


# ══════════════════════════════════════════════════════════
#  Phase 3: Single-word scan
# ══════════════════════════════════════════════════════════

def scan_single_words(text: str, model: str) -> list[str]:
    """Test every unique word/token individually. Find atomic triggers."""
    words = list(set(split_words(text)))
    # Filter out very short tokens (single punctuation, etc.)
    words = [w for w in words if len(w) >= 2]
    words.sort()
    
    print(f'\n{"="*60}')
    print(f'Phase 3: Single-Word Scan ({len(words)} unique tokens)')
    print(f'{"="*60}')
    
    blocked = []
    for i, word in enumerate(words):
        time.sleep(SLEEP_BETWEEN[0])
        try:
            is_blocked = test_content(word, model)
            if is_blocked:
                print(f'  [{i+1:3d}/{len(words)}] 🚫 BLOCKED: "{word}"')
                blocked.append(word)
            elif (i + 1) % 50 == 0:
                print(f'  [{i+1:3d}/{len(words)}] ... scanning ...')
        except Exception:
            pass
    
    print(f'\n  → {len(blocked)} single-word triggers found')
    return blocked


# ══════════════════════════════════════════════════════════
#  Phase 4: Word-pair (bigram) scan
# ══════════════════════════════════════════════════════════

def scan_word_pairs(text: str, model: str, max_pairs: int = 500) -> list[str]:
    """Test adjacent word pairs to find combination triggers."""
    words = split_words(text)
    pairs = []
    for i in range(len(words) - 1):
        pair = words[i] + words[i+1]
        if len(pair) >= 3:
            pairs.append(pair)
    
    # Deduplicate
    pairs = list(dict.fromkeys(pairs))
    if len(pairs) > max_pairs:
        print(f'  (Limiting to {max_pairs} pairs out of {len(pairs)})')
        pairs = pairs[:max_pairs]
    
    print(f'\n{"="*60}')
    print(f'Phase 4: Word-Pair Scan ({len(pairs)} pairs)')
    print(f'{"="*60}')
    
    blocked = []
    for i, pair in enumerate(pairs):
        time.sleep(SLEEP_BETWEEN[0])
        try:
            is_blocked = test_content(pair, model)
            if is_blocked:
                print(f'  [{i+1:3d}/{len(pairs)}] 🚫 BLOCKED: "{pair}"')
                blocked.append(pair)
            elif (i + 1) % 50 == 0:
                print(f'  [{i+1:3d}/{len(pairs)}] ... scanning ...')
        except Exception:
            pass
    
    print(f'\n  → {len(blocked)} word-pair triggers found')
    return blocked


# ══════════════════════════════════════════════════════════
#  Wordlist mode: test a list of words/phrases
# ══════════════════════════════════════════════════════════

def scan_wordlist(words: list[str], model: str) -> list[str]:
    """Test a provided list of words/phrases."""
    print(f'\n{"="*60}')
    print(f'Wordlist Scan ({len(words)} items)')
    print(f'{"="*60}')
    
    blocked = []
    for i, word in enumerate(words):
        word = word.strip()
        if not word or word.startswith('#'):
            continue
        time.sleep(SLEEP_BETWEEN[0])
        try:
            is_blocked = test_content(word, model)
            status = '🚫 BLOCKED' if is_blocked else '✅ OK'
            print(f'  [{i+1:3d}/{len(words)}] {status}  "{word}"')
            if is_blocked:
                blocked.append(word)
        except Exception:
            print(f'  [{i+1:3d}/{len(words)}] ❓ ERROR  "{word}"')
    
    print(f'\n  → {len(blocked)}/{len(words)} items blocked')
    return blocked


# ══════════════════════════════════════════════════════════
#  Built-in comprehensive word list
# ══════════════════════════════════════════════════════════

# Common categories of content that Chinese gateways typically filter:
# Political leaders, historical events, territorial issues, foreign media,
# social movements, sensitive organizations, economic criticism, etc.
BUILTIN_WORDLIST = [
    # ── Political terms ──
    '习近平', '习主席', '总书记', '李克强', '温家宝', '胡锦涛',
    '江泽民', '毛泽东', '邓小平', '周恩来', '刘少奇',
    '中共', '共产党', '中国共产党', '政治局', '常委',
    '反腐', '反腐败', '贪污', '腐败', '官员落马',
    '维权', '上访', '信访', '请愿', '抗议',
    '民主', '自由', '人权', '言论自由', '新闻自由',
    '独裁', '专制', '极权', '威权',
    
    # ── Historical events ──
    '六四', '天安门', '六四事件', '天安门事件', '天安门广场',
    '八九学运', '八九民运', '六四屠杀',
    '文化大革命', '文革', '大跃进', '大饥荒',
    '反右', '反右运动', '镇压',
    
    # ── Territorial/ethnic ──
    '台独', '台湾独立', '西藏独立', '藏独',
    '新疆', '维吾尔', '集中营', '再教育营',
    '达赖喇嘛', '达赖',
    '香港', '香港独立', '港独', '反送中',
    '占中', '雨伞运动', '国安法',
    
    # ── Organizations ──
    '法轮功', '法轮大法', '李洪志',
    '大纪元', '明慧网', '神韵',
    
    # ── Foreign media (Chinese names) ──
    '纽约时报', '华尔街日报', '华盛顿邮报',
    'BBC中文', 'CNN', 'VOA', '美国之音',
    'RFI', '法广', '德国之声', 'NHK',
    '路透社', '法新社', '美联社',
    
    # ── Economic criticism ──
    '经济崩溃', '经济衰退', '经济危机', '金融危机',
    '楼市崩盘', '房价暴跌', '股市崩盘', '股灾',
    '失业率', '失业潮', '裁员潮',
    '通货膨胀', '通缩', '滞胀',
    '人民币贬值', '资本外逃', '外资撤离',
    '债务危机', '地方债', '烂尾楼',
    '中国经济放缓', '中国经济衰退', '中国经济下滑',
    
    # ── Military/security ──
    '军事', '解放军', '军队', '军费',
    '间谍', '情报', '国安', '国家安全',
    '网络审查', '防火长城', 'GFW', '翻墙', 'VPN',
    '审查', '封锁', '屏蔽',
    
    # ── Social issues ──
    '计划生育', '强制堕胎', '一胎政策',
    '强制拆迁', '拆迁', '城管暴力',
    '躺平', '润', '移民潮',
    '内卷', '996', '过劳死',
    
    # ── COVID related ──
    '清零', '动态清零', '封城', '核酸',
    '新冠', '武汉病毒', '实验室泄漏',
    '李文亮', '吹哨人',
    
    # ── Religion/cult ──
    '邪教', '宗教迫害', '基督教', '天主教',
    '穆斯林', '伊斯兰', '清真寺',
    
    # ── Phrases from actual 450 triggers (from logs) ──
    '投资低迷加剧', '中国经济进入更保守时代',
    '中国2025经济增长预计将为近三十年来最低',
    '美股股灾', '科技大佬全军覆没',
    '强生爽身粉致癌案',
    '北京调整楼市限购政策',
    '十五五开局之年',
    '券商火速解读政府工作报告',
    
    # ── Compound political phrases ──
    '中国政府隐瞒', '中共暴政', '中共独裁',
    '一党专政', '党国体制', '中共政权',
    
    # ── People (dissidents/activists) ──
    '刘晓波', '艾未未', '陈光诚',
    '王丹', '吾尔开希', '柴玲',
    
    # ── Sensitive tech ──
    'ChatGPT', 'OpenAI',
    
    # ── Controls (should NOT be blocked) ──
    '你好', '天气预报', '今天吃什么', '基金净值',
    '上证指数', '沪深300', '股票代码', 'A股市场',
]


# ══════════════════════════════════════════════════════════
#  Full analysis pipeline
# ══════════════════════════════════════════════════════════

def full_analysis(text: str, model: str) -> dict:
    """Run all phases on a text and return comprehensive results."""
    results = {
        'input_length': len(text),
        'model': model,
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'phases': {},
        'all_triggers': [],
    }
    
    # Phase 0: Test full text
    print(f'\n{"="*60}')
    print(f'Phase 0: Full Text Test ({len(text)} chars)')
    print(f'{"="*60}')
    time.sleep(SLEEP_BETWEEN[0])
    full_blocked = test_content(text, model)
    print(f'  Full text: {"🚫 BLOCKED" if full_blocked else "✅ OK"}')
    results['full_text_blocked'] = full_blocked
    
    if not full_blocked:
        print('\n✅ Full text is NOT blocked. Nothing to analyze.')
        return results
    
    # Phase 1: Sentence scan
    blocked_sentences = scan_sentences(text, model)
    results['phases']['sentences'] = blocked_sentences
    
    # Phase 2: Binary search within each blocked sentence
    print(f'\n{"="*60}')
    print(f'Phase 2: Binary Search ({len(blocked_sentences)} blocked sentences)')
    print(f'{"="*60}')
    
    binary_triggers = []
    for i, sent in enumerate(blocked_sentences):
        print(f'\n  ── Sentence {i+1}: {sent[:60]}... ──')
        triggers = binary_search_trigger(sent, model)
        binary_triggers.extend(triggers)
        for t in triggers:
            print(f'    🎯 Minimal trigger: "{t}"')
    
    results['phases']['binary_search'] = binary_triggers
    
    # Phase 3: Single-word scan (only on blocked sentences to save API calls)
    combined_text = ' '.join(blocked_sentences)
    single_triggers = scan_single_words(combined_text, model)
    results['phases']['single_words'] = single_triggers
    
    # Phase 4: Word-pair scan
    pair_triggers = scan_word_pairs(combined_text, model)
    results['phases']['word_pairs'] = pair_triggers
    
    # Consolidate all triggers
    all_triggers = list(set(binary_triggers + single_triggers + pair_triggers))
    all_triggers.sort(key=len)
    results['all_triggers'] = all_triggers
    
    return results


def print_summary(results: dict):
    """Print a clean summary of findings."""
    print(f'\n{"="*60}')
    print(f'  SUMMARY')
    print(f'{"="*60}')
    print(f'  API calls made:    {_stats["calls"]}')
    print(f'  Blocked responses: {_stats["blocked"]}')
    print(f'  Passed responses:  {_stats["passed"]}')
    print(f'  Errors:            {_stats["errors"]}')
    
    triggers = results.get('all_triggers', [])
    if triggers:
        print(f'\n  🚫 Discovered {len(triggers)} trigger(s):')
        print(f'  {"─"*40}')
        for t in triggers:
            print(f'    • "{t}"')
    else:
        print('\n  ✅ No individual triggers found (may be combination-based)')
    
    # Save results
    os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
    # Load existing results
    existing = []
    if os.path.isfile(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, 'r') as f:
                existing = json.load(f)
        except Exception:
            existing = []
    
    existing.append(results)
    with open(RESULTS_FILE, 'w') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)
    print(f'\n  Results saved to {RESULTS_FILE}')


# ══════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description='Probe the gateway content filter to discover blocked words/phrases.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent('''\
            Examples:
              python debug/probe_content_filter.py --builtin
              python debug/probe_content_filter.py --text "中国经济进入更保守时代"
              python debug/probe_content_filter.py --file article.txt
              python debug/probe_content_filter.py --wordlist my_words.txt
        '''))
    
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--text', type=str, help='Text string to analyze')
    group.add_argument('--file', type=str, help='Path to text file to analyze')
    group.add_argument('--wordlist', type=str, help='Path to wordlist file (one word/phrase per line)')
    group.add_argument('--builtin', action='store_true', 
                       help='Scan built-in list of ~180 common sensitive terms')
    
    parser.add_argument('--model', type=str, default=DEFAULT_MODEL,
                        help=f'Model to use for probing (default: {DEFAULT_MODEL})')
    parser.add_argument('--sentences-only', action='store_true',
                        help='Only run Phase 1 (sentence scan), skip binary search')
    parser.add_argument('--sleep', type=float, default=SLEEP_BETWEEN[0],
                        help=f'Seconds between API calls (default: {SLEEP_BETWEEN[0]})')
    
    args = parser.parse_args()
    SLEEP_BETWEEN[0] = args.sleep
    
    print(f'🔍 Content Filter Probe')
    print(f'   Model: {args.model}')
    print(f'   API:   {LLM_BASE_URL}')
    print(f'   Sleep: {SLEEP_BETWEEN[0]}s between calls')
    
    if args.builtin:
        # Wordlist mode with built-in list
        blocked = scan_wordlist(BUILTIN_WORDLIST, args.model)
        results = {
            'mode': 'builtin_wordlist',
            'model': args.model,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_tested': len(BUILTIN_WORDLIST),
            'all_triggers': blocked,
        }
        print_summary(results)
        return
    
    if args.wordlist:
        with open(args.wordlist, 'r', encoding='utf-8') as f:
            words = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        blocked = scan_wordlist(words, args.model)
        results = {
            'mode': 'custom_wordlist',
            'model': args.model,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'total_tested': len(words),
            'all_triggers': blocked,
        }
        print_summary(results)
        return
    
    if args.file:
        with open(args.file, 'r', encoding='utf-8') as f:
            text = f.read()
    else:
        text = args.text
    
    if not text or len(text) < 2:
        print('❌ Text too short to analyze')
        sys.exit(1)
    
    if args.sentences_only:
        blocked = scan_sentences(text, args.model)
        results = {
            'mode': 'sentences_only',
            'model': args.model,
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'input_length': len(text),
            'all_triggers': blocked,
        }
    else:
        results = full_analysis(text, args.model)
    
    print_summary(results)


if __name__ == '__main__':
    main()
