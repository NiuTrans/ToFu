#!/usr/bin/env python3
"""Standalone script to kick off historical data fetching for LLM simulation.

Usage:
    python debug/run_historical_fetch.py

This fetches all 4 data layers:
  1. Fund/ETF NAV history (eastmoney)
  2. Major index K-line history (eastmoney)
  3. Macro indicators (akshare if available)
  4. Historical news/intel (Google News RSS + web search)
"""
import os
import sys
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log import get_logger
logger = get_logger('run_historical_fetch')

# ─── Configuration ─────────────────────────────────────
# Broad market ETFs covering major Chinese indices
SYMBOLS = [
    '510300',   # 沪深300ETF
    '510500',   # 中证500ETF
    '159915',   # 创业板ETF
    '512880',   # 证券ETF
    '512010',   # 医药ETF
    '159941',   # 纳指ETF (QDII)
    '518880',   # 黄金ETF
    '511010',   # 国债ETF
    '512690',   # 酒ETF
    '515790',   # 光伏ETF
    '512660',   # 军工ETF
    '512200',   # 房地产ETF
    '159869',   # 游戏ETF
    '512480',   # 半导体ETF
    '515030',   # 新能源车ETF
]

# Time period: last 6 months for good news coverage
START_DATE = '2025-10-01'
END_DATE   = '2026-03-28'

# ─── Progress display ──────────────────────────────────
_last_phase = [None]

def on_progress(phase, done, total, msg=''):
    if phase != _last_phase[0]:
        _last_phase[0] = phase
        phase_labels = {
            'prices': '📈 基金净值',
            'indices': '📊 大盘指数',
            'macro': '🏛️  宏观数据',
            'intel': '📰 新闻情报',
        }
        print(f'\n{"="*60}')
        print(f'  {phase_labels.get(phase, phase)}')
        print(f'{"="*60}')

    pct = int(done / total * 100) if total else 0
    bar = '█' * (pct // 5) + '░' * (20 - pct // 5)
    print(f'  [{bar}] {pct:3d}% ({done}/{total}) {msg}')


def main():
    print(f'''
╔══════════════════════════════════════════════════════════╗
║     LLM 模拟引擎 — 历史数据抓取                          ║
╠══════════════════════════════════════════════════════════╣
║  标的: {len(SYMBOLS)} 个 ETF                                       ║
║  时段: {START_DATE} → {END_DATE}                       ║
║  数据: 净值 + 指数 + 宏观 + 新闻情报                      ║
╚══════════════════════════════════════════════════════════╝
''')

    from lib.database import get_thread_db
    db = get_thread_db()

    from lib.trading.historical_data import (
        run_full_historical_fetch,
        get_data_coverage_report,
    )

    start = time.time()
    result = run_full_historical_fetch(
        db=db,
        symbols=SYMBOLS,
        start_date=START_DATE,
        end_date=END_DATE,
        on_progress=on_progress,
        skip_intel=False,
    )

    elapsed = time.time() - start

    # Print summary
    print(f'\n{"="*60}')
    print(f'  ✅ 抓取完成!  用时 {elapsed:.1f}s')
    print(f'{"="*60}')

    phases = result.get('phases', {})

    # Prices
    prices = phases.get('prices', {})
    for sym, info in prices.items():
        status = info.get('status', '?')
        count = info.get('count', 0)
        print(f'  📈 {sym}: {count} 条净值 ({status})')

    # Indices
    indices = phases.get('indices', {})
    for secid, info in indices.items():
        name = info.get('name', secid)
        count = info.get('count', 0)
        print(f'  📊 {name}: {count} 条K线 ({info.get("status", "?")})')

    # Macro
    macro = phases.get('macro', {})
    for key, info in macro.items():
        label = info.get('label', key)
        count = info.get('count', 0)
        print(f'  🏛️  {label}: {count} 条 ({info.get("status", "?")})')

    # Intel
    intel = phases.get('intel', {})
    if isinstance(intel, dict) and 'total_fetched' in intel:
        print(f'  📰 新闻情报: 共 {intel["total_fetched"]} 条 ({intel["total_queries"]} 次查询)')
        for cat, ci in intel.get('categories', {}).items():
            print(f'     └─ {ci["label"]}: {ci["fetched"]} 条')

    # Coverage report
    print(f'\n{"="*60}')
    print(f'  📋 数据覆盖率报告')
    print(f'{"="*60}')

    coverage = get_data_coverage_report(db, SYMBOLS, START_DATE, END_DATE)
    for sym, info in coverage.get('prices', {}).items():
        pct = info.get('coverage_pct', 0)
        cnt = info.get('count', 0)
        bar = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))
        print(f'  {sym}: [{bar}] {pct}% ({cnt} days)')

    idx = coverage.get('indices', {})
    for secid, info in idx.items():
        name = info.get('name', secid)
        pct = info.get('coverage_pct', 0)
        print(f'  {name}: {pct}% coverage')

    intel_cov = coverage.get('intel', {})
    print(f'  Intel: {intel_cov.get("total_count", 0)} articles total')

    print(f'\n  Done. Ready to run simulation with:')
    print(f'  python debug/run_llm_simulation.py')


if __name__ == '__main__':
    main()
