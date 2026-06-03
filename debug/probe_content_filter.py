#!/usr/bin/env python3
"""Probe the corporate gateway content filter via binary search.

Reconstructs a recent simulator prompt and systematically narrows down
which section(s) (or even which specific sentences) trigger HTTP 450.

Usage:
    python debug/probe_content_filter.py [--model MODEL]

Strategy:
    1. Rebuild a recent sim prompt from DB data
    2. Test the full prompt → expect 450
    3. Binary-search: split prompt into halves, test each → find which half triggers
    4. Recurse until we isolate the specific paragraph/sentence
    5. Also test each section independently (intel, signals, holdings, etc.)
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
logger = get_logger('debug.probe_filter')


def _test_content(content: str, model: str, label: str = '') -> tuple[bool, str]:
    """Send content to the gateway and check if it's filtered.

    Returns:
        (is_blocked: bool, error_detail: str)
    """
    from lib.llm import chat, ContentFilterError, RateLimitError

    messages = [
        {'role': 'system', 'content': '你是一位助手。'},
        {'role': 'user', 'content': content},
    ]

    try:
        result, usage = chat(
            messages, model=model, max_tokens=50, temperature=0,
            timeout=30, log_prefix=f'[Probe:{label}]',
        )
        return False, f'OK (got {len(result)} chars)'
    except ContentFilterError as e:
        return True, str(e)[:200]
    except RateLimitError:
        # Rate limited — wait and retry once
        time.sleep(2)
        try:
            result, usage = chat(
                messages, model=model, max_tokens=50, temperature=0,
                timeout=30, log_prefix=f'[Probe:{label}:retry]',
            )
            return False, f'OK (got {len(result)} chars)'
        except ContentFilterError as e:
            return True, str(e)[:200]
        except Exception as e:
            return False, f'NON-FILTER-ERROR: {e}'
    except Exception as e:
        return False, f'NON-FILTER-ERROR: {e}'


def _build_recent_sim_prompt(sim_date: str = '2026-01-26') -> dict:
    """Reconstruct a sim prompt from DB data to match what was recently blocked.

    Returns dict with keys: full_prompt, sections (dict of section_name → text)
    """
    from lib.database._core import _pool_get, DOMAIN_TRADING
    from lib.trading.historical_data import build_market_snapshot, _ensure_sim_tables
    from lib.trading.intel_timeline import build_intel_context_at
    from lib.trading.llm_simulator import _load_strategy_toolbox, _format_tradeable_symbols

    db = _pool_get()
    _ensure_sim_tables(db)

    # Use same symbols as the recent sim run
    symbols = ['510300', '510500', '159915', '518880', '511010']

    # Build sections independently
    sections = {}

    # 1. Strategy toolbox
    try:
        strategy_prompt, _ = _load_strategy_toolbox(db)
    except Exception as e:
        strategy_prompt = f'(策略加载失败: {e})'
    sections['strategy_toolbox'] = strategy_prompt

    # 2. Market snapshot
    try:
        market_ctx = build_market_snapshot(db, sim_date)
    except Exception as e:
        market_ctx = f'(市场快照失败: {e})'
    sections['market_snapshot'] = market_ctx or ''

    # 3. Intel context (most likely trigger!)
    try:
        intel_ctx, intel_count = build_intel_context_at(db, sim_date, only_confident_dates=True)
    except Exception as e:
        intel_ctx, intel_count = f'(情报加载失败: {e})', 0
    sections['intel_context'] = intel_ctx[:4000] if intel_ctx else ''
    sections['intel_count'] = str(intel_count)

    # 4. Static parts (account state, rules, format)
    sections['account_state'] = f"""## 账户状态
- 初始资金: ¥50,000.00
- 当前现金: ¥25,000.00
- 持仓市值: ¥25,000.00
- 组合总值: ¥50,000.00
- 累计收益: +0.00%
- 历史胜率: 50.0% (3/6)
- 已完成步数: 12/24"""

    sections['holdings'] = """## 当前持仓
- 510300 华泰柏瑞沪深300ETF: 5000份 @3.500 市值¥17,500 盈亏+2.00%
- 511010 国泰上证5年期国债ETF: 200份 @128.000 市值¥25,600 盈亏+0.50%"""

    sections['tradeable'] = f"""## 可交易标的
**用户关注的标的（已有完整数据和信号）：**
{_format_tradeable_symbols(symbols)}

**你可以自由交易 A 股市场的任意股票、ETF、基金。** 使用 6 位证券代码下单，系统会自动获取价格数据。
- 用户关注的标的已有完整量化信号，可优先参考
- 可以基于市场分析自主发现和交易任何标的
- 确保使用真实存在的 6 位证券代码（如 600519 贵州茅台、510300 沪深300ETF）
- 不要编造不存在的代码"""

    sections['rules'] = """## 交易规则
- 单笔不超过总资金的30%
- 最多持有5个标的
- 买入费率0.03%，卖出费率0.08%
- T+1交易（买入次日才能卖出）
- 信心度 < 30 的决策不执行"""

    sections['output_format'] = """## 你的任务
1. 分析当前市场环境和持仓状态
2. 从策略工具箱中选择你认为当前最适合的策略组合
3. 基于所选策略做出买卖决策
4. 输出决策和你使用的策略

## 输出格式（必须严格遵守）

**重要：你必须首先输出 <decisions> 和 <strategies_used> 结构化块，然后再输出分析文本。**

<decisions>
[{"action": "buy|sell|hold", "symbol": "标的代码", "amount": 10000, "confidence": 80, "reason": "理由"}]
</decisions>
<strategies_used>["策略名称"]</strategies_used>"""

    # Assemble full prompt
    full = f"""你正在进行历史模拟交易（股票、ETF、基金均可）。
⚠️ 当前模拟日期: {sim_date}（你只能看到此日期及之前的数据）

{sections['strategy_toolbox']}

{sections['account_state']}

{sections['holdings']}

{sections['tradeable']}

## 量化信号（截至{sim_date}）
（数据不足）

{sections['market_snapshot']}

## 市场情报（时间锁定至{sim_date}）
{sections['intel_context']}
（共{sections['intel_count']}条情报）

{sections['rules']}

{sections['output_format']}"""

    sections['full_prompt'] = full
    return sections


def _binary_search_trigger(text: str, model: str, depth: int = 0, max_depth: int = 6) -> list[str]:
    """Binary search within a text block to find the minimal triggering substring.

    Returns list of trigger snippets found.
    """
    indent = '  ' * depth

    if len(text) < 20:
        return [text]

    if depth >= max_depth:
        print(f'{indent}⚠️  Max depth reached. Trigger within: {text[:100]}...')
        return [text[:200]]

    mid = len(text) // 2
    first_half = text[:mid]
    second_half = text[mid:]

    triggers = []

    # Test first half
    blocked_1, detail_1 = _test_content(first_half, model, f'half1-d{depth}')
    time.sleep(0.5)  # be gentle on rate limits

    # Test second half
    blocked_2, detail_2 = _test_content(second_half, model, f'half2-d{depth}')
    time.sleep(0.5)

    if blocked_1:
        print(f'{indent}🔴 First half BLOCKED ({len(first_half)} chars)')
        triggers.extend(_binary_search_trigger(first_half, model, depth + 1, max_depth))
    else:
        print(f'{indent}🟢 First half OK ({len(first_half)} chars)')

    if blocked_2:
        print(f'{indent}🔴 Second half BLOCKED ({len(second_half)} chars)')
        triggers.extend(_binary_search_trigger(second_half, model, depth + 1, max_depth))
    else:
        print(f'{indent}🟢 Second half OK ({len(second_half)} chars)')

    if not blocked_1 and not blocked_2:
        # Neither half triggers alone — the trigger might span the boundary
        print(f'{indent}⚠️  Neither half triggers alone — testing boundary region...')
        boundary = text[max(0, mid - 200):min(len(text), mid + 200)]
        blocked_b, _ = _test_content(boundary, model, f'boundary-d{depth}')
        if blocked_b:
            print(f'{indent}🔴 Boundary region BLOCKED')
            triggers.extend(_binary_search_trigger(boundary, model, depth + 1, max_depth))
        else:
            print(f'{indent}⚠️  Combination-only trigger (needs both halves together)')
            triggers.append(f'[COMBO-TRIGGER: full block of {len(text)} chars]')

    return triggers


def main():
    parser = argparse.ArgumentParser(description='Probe content filter via binary search')
    parser.add_argument('--model', default='gemini-2.5-pro',
                        help='Model to use for probing (default: gemini-2.5-pro)')
    parser.add_argument('--sim-date', default='2026-01-26',
                        help='Simulation date to reconstruct prompt for')
    parser.add_argument('--section-only', default=None,
                        help='Test only a specific section (e.g. intel_context)')
    parser.add_argument('--deep', action='store_true',
                        help='Run binary search on blocked sections')
    args = parser.parse_args()

    print(f'=== Content Filter Probe ===')
    print(f'Model: {args.model}')
    print(f'Sim date: {args.sim_date}')
    print()

    # Build prompt sections
    print('📦 Reconstructing simulator prompt from DB...')
    sections = _build_recent_sim_prompt(args.sim_date)
    print(f'   Full prompt: {len(sections["full_prompt"])} chars')
    print(f'   Sections: {[k for k in sections if k != "full_prompt"]}')
    print()

    if args.section_only:
        # Test just one section
        text = sections.get(args.section_only, '')
        if not text:
            print(f'❌ Section "{args.section_only}" is empty or not found')
            return
        print(f'Testing section "{args.section_only}" ({len(text)} chars)...')
        blocked, detail = _test_content(text, args.model, args.section_only)
        status = '🔴 BLOCKED' if blocked else '🟢 OK'
        print(f'  {status}: {detail}')

        if blocked and args.deep:
            print(f'\n🔍 Binary searching within "{args.section_only}"...')
            triggers = _binary_search_trigger(text, args.model)
            print(f'\n📋 Found {len(triggers)} trigger(s):')
            for i, t in enumerate(triggers, 1):
                print(f'  {i}. {t[:300]}')
        return

    # ── Phase 1: Test full prompt ──
    print('── Phase 1: Full prompt test ──')
    blocked, detail = _test_content(sections['full_prompt'], args.model, 'full')
    status = '🔴 BLOCKED' if blocked else '🟢 OK'
    print(f'  Full prompt: {status}')
    print(f'  Detail: {detail}')
    print()

    if not blocked:
        print('✅ Full prompt passed! The filter might be intermittent or model-specific.')
        print('   Try a different --model or --sim-date.')
        return

    # ── Phase 2: Test each section independently ──
    print('── Phase 2: Section-by-section test ──')
    blocked_sections = []
    test_sections = [
        'strategy_toolbox', 'account_state', 'holdings', 'tradeable',
        'market_snapshot', 'intel_context', 'rules', 'output_format',
    ]

    for name in test_sections:
        text = sections.get(name, '')
        if not text or len(text) < 10:
            print(f'  {name}: (empty/trivial, skipped)')
            continue
        blocked, detail = _test_content(text, args.model, name)
        status = '🔴 BLOCKED' if blocked else '🟢 OK'
        print(f'  {name} ({len(text)} chars): {status}')
        if blocked:
            blocked_sections.append(name)
        time.sleep(0.5)

    print()

    if not blocked_sections:
        print('⚠️  No individual section triggers the filter alone.')
        print('   The trigger might be from section combinations.')
        print('   Try: python debug/probe_content_filter.py --deep')
        return

    print(f'🔴 Blocked sections: {blocked_sections}')

    # ── Phase 3: Deep binary search on blocked sections ──
    if args.deep:
        print()
        print('── Phase 3: Binary search within blocked sections ──')
        all_triggers = []
        for name in blocked_sections:
            text = sections[name]
            print(f'\n🔍 Searching in "{name}" ({len(text)} chars)...')
            triggers = _binary_search_trigger(text, args.model)
            all_triggers.extend(triggers)

        print(f'\n{"="*60}')
        print(f'📋 RESULTS: Found {len(all_triggers)} trigger snippet(s):')
        print(f'{"="*60}')
        for i, t in enumerate(all_triggers, 1):
            print(f'\n--- Trigger {i} ---')
            print(t[:500])
    else:
        print('💡 Run with --deep to binary-search within blocked sections.')


if __name__ == '__main__':
    main()
