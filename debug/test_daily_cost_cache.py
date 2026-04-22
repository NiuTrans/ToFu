"""debug/test_daily_cost_cache.py — smoke test for daily_cost_cache persistence.

Verifies:
  1. Schema has daily_cost_cache table with the expected columns.
  2. _persist_day_cost → _load_cached_day_costs round-trip works.
  3. invalidate_day_cost_cache(None) clears all; (date_str) clears one.
  4. INSERT OR REPLACE updates an existing row rather than erroring.
  5. _get_monthly_costs uses the cache (doesn't scan on second call).

Run: CHATUI_DB_BACKEND=sqlite python debug/test_daily_cost_cache.py
"""

import os
import sys
import tempfile
import time as _time

# Use an isolated SQLite DB for this test so we don't touch real data
_tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
_tmp.close()
os.environ['CHATUI_DB_PATH'] = _tmp.name
os.environ['CHATUI_DB_BACKEND'] = 'sqlite'

# Ensure project root is on sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from lib.database import DOMAIN_CHAT, get_thread_db, init_db


def run():
    print(f'[test] DB: {_tmp.name}')
    init_db()

    # Need Flask app context? get_thread_db doesn't use g, so it's fine.
    from routes.daily_report import (
        _get_monthly_costs, _load_cached_day_costs, _persist_day_cost,
        invalidate_day_cost_cache,
    )

    db = get_thread_db(DOMAIN_CHAT)

    # ── 1) Schema check ──
    rows = db.execute("PRAGMA table_info(daily_cost_cache)").fetchall()
    cols = {r[1] for r in rows} if rows else set()
    expected = {'user_id', 'date', 'cost', 'conversations_json', 'computed_at'}
    assert expected.issubset(cols), f'Schema missing cols. Got: {cols}'
    print(f'[test] ✓ Schema OK — cols={sorted(cols)}')

    # ── 2) Persist & load round-trip ──
    day_data = {
        'cost': 3.1416,
        'conversations': {
            'conv-A': {'name': 'Hello ☃', 'cost': 2.0, 'tokens': 12345},
            'conv-B': {'name': 'World', 'cost': 1.1416, 'tokens': 6789},
        },
    }
    _persist_day_cost('2025-06-15', day_data)
    loaded = _load_cached_day_costs(2025, 6)
    assert 15 in loaded, f'Day 15 missing: {loaded}'
    assert abs(loaded[15]['cost'] - 3.1416) < 1e-6, f'Cost mismatch: {loaded[15]}'
    assert loaded[15]['conversations']['conv-A']['name'] == 'Hello ☃', \
        f'Unicode roundtrip failed: {loaded[15]}'
    print(f'[test] ✓ Round-trip OK — {loaded[15]}')

    # ── 3) INSERT OR REPLACE behavior ──
    day_data2 = {'cost': 99.99, 'conversations': {}}
    _persist_day_cost('2025-06-15', day_data2)
    loaded = _load_cached_day_costs(2025, 6)
    assert abs(loaded[15]['cost'] - 99.99) < 1e-6, \
        f'UPSERT didn\'t update: {loaded[15]}'
    print(f'[test] ✓ UPSERT OK — cost={loaded[15]["cost"]}')

    # ── 4) Per-date invalidation ──
    _persist_day_cost('2025-06-10', {'cost': 5.0, 'conversations': {}})
    invalidate_day_cost_cache('2025-06-15')
    loaded = _load_cached_day_costs(2025, 6)
    assert 15 not in loaded, f'Date 15 not invalidated: {loaded}'
    assert 10 in loaded, f'Date 10 also removed by mistake: {loaded}'
    print(f'[test] ✓ Per-date invalidation OK — remaining={list(loaded.keys())}')

    # ── 5) Bulk invalidation ──
    invalidate_day_cost_cache()
    loaded = _load_cached_day_costs(2025, 6)
    assert not loaded, f'Bulk invalidation left rows: {loaded}'
    print('[test] ✓ Bulk invalidation OK')

    # ── 6) _get_monthly_costs — past month, no conversations ──
    # (no rows in conversations table — should return {} and populate cache
    #  with zero rows... actually, our code persists 0-cost but doesn't
    #  return them.  Second call should see cache hits.)
    res1 = _get_monthly_costs(2025, 6)
    assert res1 == {}, f'Empty DB should produce empty result: {res1}'
    # After the first call, we expect 30 cached rows (one per day in June)
    cache_after = _load_cached_day_costs(2025, 6)
    assert len(cache_after) == 30, \
        f'Expected 30 zero-rows cached, got {len(cache_after)}'
    print(f'[test] ✓ First call populated {len(cache_after)} zero-rows')

    # Second call — should be a full cache hit, no scan
    t0 = _time.monotonic()
    res2 = _get_monthly_costs(2025, 6)
    elapsed = (_time.monotonic() - t0) * 1000
    assert res2 == {}, f'Empty result expected: {res2}'
    print(f'[test] ✓ Second call was {elapsed:.1f}ms (expected <50ms for cache hit)')
    assert elapsed < 200, f'Second call too slow ({elapsed:.1f}ms) — cache not hit?'

    # ── 7) Insert a real message with usage into conversations, verify scan ──
    now_ms = int(_time.time() * 1000)
    # A conversation in June 2025
    jun15_ms = 1750032000000  # 2025-06-15 ~12:00 UTC (approx)
    conv_msgs = [{
        'role': 'user',
        'content': 'hi',
        'timestamp': jun15_ms,
    }, {
        'role': 'assistant',
        'content': 'hello',
        'timestamp': jun15_ms + 1000,
        'model': 'gpt-4o',
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50},
    }]
    db.execute(
        'INSERT OR REPLACE INTO conversations '
        '(id, user_id, title, messages, created_at, updated_at, settings, msg_count, search_text) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        ('test-conv', 1, 'Test', json.dumps(conv_msgs),
         jun15_ms, jun15_ms + 1000, '{}', 2, '')
    )
    db.commit()

    # Invalidate cache and re-query
    invalidate_day_cost_cache()
    res3 = _get_monthly_costs(2025, 6)
    # Day 15 in UTC+8 would be day 16, but fromtimestamp uses local time.
    # Just check SOME day has cost > 0 in this month.
    days_with_cost = {d: v['cost'] for d, v in res3.items()}
    assert days_with_cost, f'No days with cost: {res3}'
    total = sum(days_with_cost.values())
    assert total > 0, f'Total cost should be > 0: {total}'
    print(f'[test] ✓ Full flow OK — days with cost: {days_with_cost}, total=¥{total:.4f}')

    print('\n[test] ALL GREEN ✓')


if __name__ == '__main__':
    try:
        run()
    finally:
        try:
            os.unlink(_tmp.name)
        except OSError:
            pass
