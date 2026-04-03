"""lib/trading_autopilot/strategy_evolution.py — Strategy Evolution Engine.

Reviews active strategies, evaluates their historical performance,
learns from outcomes, and proposes improvements.
"""

from datetime import datetime, timedelta

from lib.log import get_logger
from lib.trading_autopilot._constants import STRATEGY_EVOLUTION_LOOKBACK

logger = get_logger(__name__)

__all__ = [
    'evaluate_strategy_history',
    'evolve_strategies',
    'record_decision_outcome',
]


def evaluate_strategy_history(db, strategy_id, lookback_days=STRATEGY_EVOLUTION_LOOKBACK):
    """Evaluate a strategy's historical performance.

    Looks at past decisions that used this strategy, cross-referencing
    the trading_strategy_performance table for per-decision outcome data.

    Returns:
      { win_rate, avg_return, total_decisions, lesson_summary }
    """
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime('%Y-%m-%d')

    # Step 1: Check which columns exist in trading_strategy_performance
    #         (handles DB migration gracefully)
    try:
        sp_cols = {row[1] for row in db.execute("PRAGMA table_info(trading_strategy_performance)").fetchall()}
    except Exception as e:
        logger.warning('Failed to read trading_strategy_performance table schema: %s', e, exc_info=True)
        sp_cols = set()

    has_decision_id = 'decision_id' in sp_cols
    _has_outcome = 'actual_outcome' in sp_cols  # noqa: F841 — reserved for future outcome tracking
    has_lesson = 'lesson' in sp_cols

    # Step 2: Query decisions that used this strategy (via strategy_group_id or strategy match)
    decisions = db.execute('''
        SELECT * FROM trading_decision_history
        WHERE created_at >= ? AND strategy_group_id IS NOT NULL
        ORDER BY created_at DESC LIMIT 100
    ''', (cutoff,)).fetchall()
    decisions = [dict(d) for d in decisions]

    if not decisions:
        return {
            'win_rate': None, 'avg_return': None,
            'total_decisions': 0, 'lesson_summary': 'No historical data yet.',
        }

    # Step 3: Get performance records for this strategy
    perf_by_decision = {}
    if has_decision_id:
        perfs = db.execute(
            'SELECT * FROM trading_strategy_performance WHERE strategy_id = ?',
            (strategy_id,)
        ).fetchall()
        for p in perfs:
            p = dict(p)
            if p.get('decision_id'):
                perf_by_decision[p['decision_id']] = p
    else:
        # Fallback: match by strategy_id alone
        perfs = db.execute(
            'SELECT * FROM trading_strategy_performance WHERE strategy_id = ? ORDER BY created_at DESC LIMIT 50',
            (strategy_id,)
        ).fetchall()
        for p in perfs:
            perf_by_decision[dict(p).get('id')] = dict(p)

    wins = 0
    returns = []
    lessons = []

    for d in decisions:
        perf = perf_by_decision.get(d['id'])
        if perf:
            rp = perf.get('return_pct')
            if rp is not None:
                returns.append(rp)
                if rp > 0:
                    wins += 1
            if has_lesson and perf.get('lesson'):
                lessons.append(perf['lesson'])

    total = len(returns) if returns else len(decisions)
    return {
        'win_rate': round(wins / total * 100, 1) if total > 0 else None,
        'avg_return': round(sum(returns) / len(returns), 2) if returns else None,
        'total_decisions': len(decisions),
        'lesson_summary': '; '.join(lessons[-5:]) if lessons else 'No lessons yet.',
    }


def evolve_strategies(db):
    """Review all strategies, learn from outcomes, propose improvements.

    This is the "self-improvement" loop:
      1. For each active strategy, check its win rate
      2. If win rate is low, mark it as 'needs_review'
      3. Collect lessons from failures
      4. Return evolution context for the LLM to propose better strategies

    Returns context string for the LLM.
    """
    strategies = db.execute(
        "SELECT * FROM trading_strategies WHERE status='active' ORDER BY updated_at DESC"
    ).fetchall()
    strategies = [dict(s) for s in strategies]

    if not strategies:
        return "", []

    evolution_items = []
    for s in strategies:
        perf = evaluate_strategy_history(db, s['id'])
        evolution_items.append({
            'id': s['id'],
            'name': s['name'],
            'type': s['type'],
            'logic': s['logic'],
            'performance': perf,
        })

    # Build context
    lines = ["## 策略进化分析 (Strategy Evolution Review)"]
    underperformers = []
    for item in evolution_items:
        p = item['performance']
        status = '✅' if (p['win_rate'] or 0) >= 60 else '⚠️' if (p['win_rate'] or 0) >= 40 else '❌'
        lines.append(f"\n### {status} {item['name']} ({item['type']})")
        lines.append(f"  逻辑: {item['logic']}")
        if p['total_decisions'] > 0:
            lines.append(f"  胜率: {p['win_rate']}% | 平均收益: {p['avg_return']}% | 决策次数: {p['total_decisions']}")
            lines.append(f"  经验教训: {p['lesson_summary']}")
        else:
            lines.append("  (尚无历史数据)")
        if (p['win_rate'] or 0) < 40 and p['total_decisions'] >= 3:
            underperformers.append(item)

    if underperformers:
        lines.append(f"\n### 🔴 需要改进的策略: {len(underperformers)}个")
        for u in underperformers:
            lines.append(f"  - {u['name']}: 胜率仅{u['performance']['win_rate']}%, 建议优化或替换")

    return "\n".join(lines), evolution_items


def record_decision_outcome(db, decision_id, strategy_id, return_pct, actual_outcome, lesson):
    """Record the outcome of a past decision for strategy learning.

    The autopilot columns (decision_id, actual_outcome, lesson, evaluated_at)
    are guaranteed to exist — they are added by init_db() migration which runs
    at startup before any autopilot code executes.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute('''
        INSERT INTO trading_strategy_performance
        (strategy_id, decision_id, return_pct, actual_outcome, lesson, evaluated_at,
         period_start, period_end, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'autopilot', ?)
    ''', (strategy_id, decision_id, return_pct, actual_outcome, lesson, now, now, now, now))
    db.commit()
