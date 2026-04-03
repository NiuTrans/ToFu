"""lib/trading_autopilot/strategy_learner.py — Strategy Failure Learning Engine.

Learns from every decision outcome to build:
  1. Strategy compatibility matrix (which combos work / don't work together)
  2. Strategy failure profiles (conditions where each strategy fails)
  3. Auto-generated improvement proposals (update logic, retire, replace)

This is the "memory" of the system — it prevents repeating the same mistakes.

Core data model:
  trading_strategy_deployments  — what combos were deployed when
  trading_strategy_combo_outcomes — outcomes for each combo deployment
  trading_strategy_failures — detailed failure records with conditions

Learning loop:
  1. After each autopilot cycle, record_combo_deployment (in meta_strategy.py)
  2. After outcome tracking (7-14 days later), evaluate_deployment_outcome
  3. Update compatibility scores + failure records
  4. Next meta-strategy selection uses updated data
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'evaluate_deployment_outcome',
    'get_strategy_compatibility_scores',
    'get_strategy_failure_summary',
    'generate_improvement_proposals',
    'get_learning_report',
]


# ═══════════════════════════════════════════════════════════
#  Deployment Outcome Evaluation
# ═══════════════════════════════════════════════════════════

def evaluate_deployment_outcome(
    db: Any,
    cycle_id: str,
    actual_return_pct: float,
    benchmark_return_pct: float = 0.0,
    outcome_notes: str = '',
) -> dict[str, Any]:
    """Evaluate the outcome of a past strategy combo deployment.

    Called after the outcome tracking period (typically 7-14 days).
    Computes:
      - Whether the combo beat the benchmark
      - Per-strategy attribution (as far as possible)
      - Compatibility updates for each strategy pair

    Args:
        db:                   Database connection.
        cycle_id:             The autopilot cycle ID this deployment belongs to.
        actual_return_pct:    Actual portfolio return since deployment.
        benchmark_return_pct: Benchmark return over same period.
        outcome_notes:        Free-text notes about what happened.

    Returns:
        Dict with evaluation results.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Load the deployment record
    dep = db.execute(
        'SELECT * FROM trading_strategy_deployments WHERE cycle_id = ?',
        (cycle_id,)
    ).fetchone()

    if not dep:
        logger.warning('[StrategyLearner] No deployment found for cycle %s', cycle_id)
        return {'error': f'No deployment found for cycle {cycle_id}'}

    dep = dict(dep)
    try:
        strategy_ids = json.loads(dep.get('strategy_ids_json', '[]'))
        strategy_names = json.loads(dep.get('strategy_names_json', '[]'))
        condition = json.loads(dep.get('market_condition_json', '{}'))
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[StrategyLearner] JSON parse error for deployment %s: %s', cycle_id, e)
        return {'error': f'Corrupt deployment data: {e}'}

    excess_return = actual_return_pct - benchmark_return_pct
    is_success = excess_return > -1.0  # success = beat benchmark by more than -1%
    outcome = 'success' if is_success else 'failure'

    # ── Record combo outcome ──
    try:
        db.execute(
            '''INSERT INTO trading_strategy_combo_outcomes
               (cycle_id, strategy_ids_json, market_regime, actual_return_pct,
                benchmark_return_pct, excess_return_pct, outcome,
                outcome_notes, evaluated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                cycle_id,
                json.dumps(strategy_ids),
                condition.get('regime', 'unknown'),
                actual_return_pct,
                benchmark_return_pct,
                round(excess_return, 3),
                outcome,
                outcome_notes,
                now,
            )
        )
    except Exception as e:
        logger.error('[StrategyLearner] Failed to record combo outcome: %s', e, exc_info=True)
        return {'error': str(e)}

    # ── Update compatibility scores for each strategy pair ──
    compatibility_updates = []
    for i in range(len(strategy_ids)):
        for j in range(i + 1, len(strategy_ids)):
            pair_key = _combo_key(strategy_ids[i], strategy_ids[j])
            delta = 0.1 if is_success else -0.15  # failures weigh more (learn faster from mistakes)
            _update_compatibility(db, pair_key, strategy_ids[i], strategy_ids[j], delta)
            compatibility_updates.append({
                'pair': pair_key,
                'strategy_a': strategy_names[i] if i < len(strategy_names) else str(strategy_ids[i]),
                'strategy_b': strategy_names[j] if j < len(strategy_names) else str(strategy_ids[j]),
                'delta': delta,
            })

    # ── Record failures with conditions ──
    failure_records = []
    if not is_success:
        regime = condition.get('regime', 'unknown')
        for sid, sname in zip(strategy_ids, strategy_names):
            _record_failure(
                db, sid, sname, cycle_id, regime,
                actual_return_pct, excess_return, outcome_notes,
            )
            failure_records.append({
                'strategy_id': sid,
                'strategy_name': sname,
                'regime': regime,
                'excess_return': excess_return,
            })

    db.commit()

    logger.info(
        '[StrategyLearner] Evaluated cycle=%s: %s (return=%.2f%%, excess=%.2f%%), '
        '%d compat updates, %d failures recorded',
        cycle_id, outcome, actual_return_pct, excess_return,
        len(compatibility_updates), len(failure_records),
    )

    return {
        'cycle_id': cycle_id,
        'outcome': outcome,
        'actual_return_pct': actual_return_pct,
        'excess_return_pct': round(excess_return, 3),
        'strategy_count': len(strategy_ids),
        'compatibility_updates': compatibility_updates,
        'failure_records': failure_records,
    }


def _combo_key(id_a: int, id_b: int) -> str:
    """Canonical key for a strategy pair (order-independent)."""
    return f"{min(id_a, id_b)}_{max(id_a, id_b)}"


def _update_compatibility(db, pair_key, id_a, id_b, delta):
    """Update compatibility score for a strategy pair.

    Score range: [-1, +1]
      -1 = always fail together
      +1 = always succeed together
    """
    existing = db.execute(
        'SELECT * FROM trading_strategy_compatibility WHERE pair_key = ?',
        (pair_key,)
    ).fetchone()

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    if existing:
        existing = dict(existing)
        old_score = existing.get('compatibility_score', 0.0)
        new_count = existing.get('sample_count', 0) + 1
        # Exponential moving average: more recent outcomes weigh more
        alpha = 2.0 / (new_count + 1)
        new_score = old_score * (1 - alpha) + delta * alpha
        new_score = max(-1.0, min(1.0, new_score))
        db.execute(
            '''UPDATE trading_strategy_compatibility
               SET compatibility_score = ?, sample_count = ?, updated_at = ?
               WHERE pair_key = ?''',
            (round(new_score, 4), new_count, now, pair_key)
        )
    else:
        db.execute(
            '''INSERT INTO trading_strategy_compatibility
               (pair_key, strategy_id_a, strategy_id_b,
                compatibility_score, sample_count, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (pair_key, id_a, id_b, round(delta, 4), 1, now)
        )


def _record_failure(db, strategy_id, strategy_name, cycle_id, regime,
                    actual_return, excess_return, notes):
    """Record a strategy failure with conditions for pattern analysis."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        db.execute(
            '''INSERT INTO trading_strategy_failures
               (strategy_id, strategy_name, cycle_id, market_regime,
                actual_return_pct, excess_return_pct, failure_notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            (strategy_id, strategy_name, cycle_id, regime,
             actual_return, excess_return, notes, now)
        )
    except Exception as e:
        logger.error('[StrategyLearner] Failed to record failure: %s', e, exc_info=True)


# ═══════════════════════════════════════════════════════════
#  Compatibility & Failure Queries
# ═══════════════════════════════════════════════════════════

def get_strategy_compatibility_scores(db: Any) -> dict[str, float]:
    """Load all strategy compatibility scores.

    Returns dict: {pair_key: score} where score in [-1, +1].
    """
    try:
        rows = db.execute(
            'SELECT pair_key, compatibility_score FROM trading_strategy_compatibility'
        ).fetchall()
        return {r['pair_key']: r['compatibility_score'] for r in rows}
    except Exception as e:
        logger.warning('[StrategyLearner] Failed to load compatibility scores: %s', e)
        return {}


def get_strategy_failure_summary(db: Any) -> dict[str, dict[str, Any]]:
    """Get failure summary per strategy.

    Returns dict: {strategy_id: {total_failures, regime_failures, avg_excess_return, ...}}
    """
    try:
        rows = db.execute(
            '''SELECT strategy_id, strategy_name, market_regime,
                      actual_return_pct, excess_return_pct
               FROM trading_strategy_failures
               ORDER BY created_at DESC'''
        ).fetchall()
    except Exception as e:
        logger.warning('[StrategyLearner] Failed to load failure summary: %s', e)
        return {}

    by_strategy: dict[str, dict[str, Any]] = {}
    for r in rows:
        r = dict(r)
        sid = str(r['strategy_id'])
        if sid not in by_strategy:
            by_strategy[sid] = {
                'strategy_name': r['strategy_name'],
                'total_failures': 0,
                'regime_failures': defaultdict(int),
                'excess_returns': [],
            }
        entry = by_strategy[sid]
        entry['total_failures'] += 1
        entry['regime_failures'][r['market_regime']] += 1
        entry['excess_returns'].append(r['excess_return_pct'])

    # Compute aggregates
    for sid, entry in by_strategy.items():
        returns = entry['excess_returns']
        entry['avg_excess_return'] = round(sum(returns) / len(returns), 2) if returns else 0
        entry['worst_excess_return'] = round(min(returns), 2) if returns else 0
        # Convert defaultdict to regular dict for JSON serialization
        entry['regime_failures'] = dict(entry['regime_failures'])
        # Find the regime where this strategy fails most
        if entry['regime_failures']:
            entry['worst_regime'] = max(entry['regime_failures'],
                                        key=entry['regime_failures'].get)
        else:
            entry['worst_regime'] = 'unknown'
        del entry['excess_returns']  # Don't expose raw data

    return by_strategy


# ═══════════════════════════════════════════════════════════
#  Improvement Proposals
# ═══════════════════════════════════════════════════════════

def generate_improvement_proposals(
    db: Any,
    min_failures: int = 3,
) -> list[dict[str, Any]]:
    """Generate actionable improvement proposals for underperforming strategies.

    Analyzes failure patterns to propose:
      - 'retire': strategy consistently fails across regimes
      - 'restrict': strategy fails in specific regimes → restrict its use
      - 'update': strategy logic needs modification
      - 'incompatible': specific combo always fails → flag as incompatible

    Args:
        db:            Database connection.
        min_failures:  Minimum failures before generating a proposal.

    Returns:
        List of proposal dicts with action, target, reason, evidence.
    """
    failures = get_strategy_failure_summary(db)
    compatibility = get_strategy_compatibility_scores(db)
    proposals: list[dict[str, Any]] = []

    # ── Proposal 1: Retire consistently failing strategies ──
    for sid, summary in failures.items():
        if summary['total_failures'] < min_failures:
            continue

        # If strategy fails across ALL regimes (not just one), retire it
        regime_count = len(summary['regime_failures'])
        if regime_count >= 3 and summary['avg_excess_return'] < -2.0:
            proposals.append({
                'action': 'retire',
                'strategy_id': int(sid),
                'strategy_name': summary['strategy_name'],
                'reason': (
                    f"策略「{summary['strategy_name']}」在{regime_count}种市场体制下"
                    f"共失败{summary['total_failures']}次，平均超额收益{summary['avg_excess_return']}%。"
                    f"建议退役并替换。"
                ),
                'evidence': {
                    'total_failures': summary['total_failures'],
                    'regime_failures': summary['regime_failures'],
                    'avg_excess_return': summary['avg_excess_return'],
                },
                'severity': 'high',
            })
            continue

        # ── Proposal 2: Restrict to specific regimes ──
        worst_regime = summary.get('worst_regime', 'unknown')
        worst_count = summary['regime_failures'].get(worst_regime, 0)
        if worst_count >= min_failures:
            proposals.append({
                'action': 'restrict',
                'strategy_id': int(sid),
                'strategy_name': summary['strategy_name'],
                'reason': (
                    f"策略「{summary['strategy_name']}」在{worst_regime}体制下"
                    f"失败{worst_count}次。建议在该体制下禁用此策略。"
                ),
                'evidence': {
                    'worst_regime': worst_regime,
                    'regime_failure_count': worst_count,
                    'total_failures': summary['total_failures'],
                },
                'restricted_regimes': [worst_regime],
                'severity': 'medium',
            })

    # ── Proposal 3: Flag incompatible strategy combos ──
    for pair_key, score in compatibility.items():
        if score < -0.4:  # significantly negative compatibility
            parts = pair_key.split('_')
            if len(parts) == 2:
                id_a, id_b = parts
                # Look up names
                name_a = _get_strategy_name(db, int(id_a))
                name_b = _get_strategy_name(db, int(id_b))
                proposals.append({
                    'action': 'incompatible',
                    'strategy_id_a': int(id_a),
                    'strategy_id_b': int(id_b),
                    'strategy_name_a': name_a,
                    'strategy_name_b': name_b,
                    'reason': (
                        f"策略组合「{name_a}」+「{name_b}」兼容性得分={score:.2f}，"
                        f"多次同时部署均表现不佳。建议避免同时使用。"
                    ),
                    'evidence': {
                        'compatibility_score': score,
                    },
                    'severity': 'medium',
                })

    # ── Proposal 4: Strategies needing logic update ──
    for sid, summary in failures.items():
        if summary['total_failures'] < min_failures:
            continue
        # If the strategy fails moderately (not retirement-level), suggest update
        if 2 <= summary['total_failures'] < 6 and summary['avg_excess_return'] > -5.0:
            proposals.append({
                'action': 'update',
                'strategy_id': int(sid),
                'strategy_name': summary['strategy_name'],
                'reason': (
                    f"策略「{summary['strategy_name']}」失败{summary['total_failures']}次，"
                    f"平均超额收益{summary['avg_excess_return']}%。"
                    f"主要在{summary.get('worst_regime', '?')}体制下失败。"
                    f"建议审视并优化策略逻辑。"
                ),
                'evidence': {
                    'total_failures': summary['total_failures'],
                    'worst_regime': summary.get('worst_regime'),
                    'avg_excess_return': summary['avg_excess_return'],
                },
                'severity': 'low',
            })

    # Sort by severity
    _SEV_ORDER = {'high': 0, 'medium': 1, 'low': 2}
    proposals.sort(key=lambda p: _SEV_ORDER.get(p.get('severity', 'low'), 3))

    logger.info('[StrategyLearner] Generated %d improvement proposals', len(proposals))
    return proposals


def _get_strategy_name(db, strategy_id):
    """Look up strategy name by ID."""
    row = db.execute('SELECT name FROM trading_strategies WHERE id=?', (strategy_id,)).fetchone()
    return row['name'] if row else f'策略#{strategy_id}'


# ═══════════════════════════════════════════════════════════
#  Learning Report
# ═══════════════════════════════════════════════════════════

def get_learning_report(db: Any) -> dict[str, Any]:
    """Generate a comprehensive learning report for the autopilot.

    This is injected into the mega-prompt so the LLM knows:
      - What the system has learned from past failures
      - Which combos to avoid
      - Which strategies need attention

    Returns:
        Dict with sections: compatibility_issues, failure_patterns,
        improvement_proposals, learning_stats.
    """
    compatibility = get_strategy_compatibility_scores(db)
    failures = get_strategy_failure_summary(db)
    proposals = generate_improvement_proposals(db)

    # Stats
    try:
        total_deployments = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_strategy_deployments'
        ).fetchone()
        total_outcomes = db.execute(
            'SELECT COUNT(*) as cnt FROM trading_strategy_combo_outcomes'
        ).fetchone()
        success_count = db.execute(
            "SELECT COUNT(*) as cnt FROM trading_strategy_combo_outcomes WHERE outcome='success'"
        ).fetchone()
    except Exception as e:
        logger.warning('[StrategyLearner] Stats query failed: %s', e)
        total_deployments = total_outcomes = success_count = None

    stats = {
        'total_deployments': total_deployments['cnt'] if total_deployments else 0,
        'total_evaluated': total_outcomes['cnt'] if total_outcomes else 0,
        'success_count': success_count['cnt'] if success_count else 0,
    }
    if stats['total_evaluated'] > 0:
        stats['win_rate_pct'] = round(
            stats['success_count'] / stats['total_evaluated'] * 100, 1
        )

    # Top incompatible pairs
    bad_pairs = sorted(
        [(k, v) for k, v in compatibility.items() if v < -0.2],
        key=lambda x: x[1]
    )[:5]

    # Most-failing strategies
    worst_strategies = sorted(
        failures.items(),
        key=lambda x: x[1]['total_failures'],
        reverse=True
    )[:5]

    return {
        'stats': stats,
        'worst_combos': [
            {'pair': k, 'score': v} for k, v in bad_pairs
        ],
        'worst_strategies': [
            {'id': sid, **summary} for sid, summary in worst_strategies
        ],
        'proposals': proposals,
    }


def build_learning_prompt_section(db: Any) -> str:
    """Build prompt section from learning data for injection into mega-prompt."""
    report = get_learning_report(db)

    if report['stats']['total_evaluated'] == 0:
        return (
            "\n## 策略学习系统\n"
            "_策略学习系统刚启动，尚无历史学习数据。"
            "系统将从每次决策结果中学习，自动优化策略选择。_\n"
        )

    lines = [
        "",
        "═══════════════════════════════════════",
        "## 策略学习报告 (Strategy Learning Report)",
        "═══════════════════════════════════════",
        "",
        "### 📈 学习统计",
        f"  总部署次数: {report['stats']['total_deployments']}",
        f"  已评估结果: {report['stats']['total_evaluated']}",
        f"  成功率: {report['stats'].get('win_rate_pct', 'N/A')}%",
        "",
    ]

    # Incompatible combos
    if report['worst_combos']:
        lines.append("### ⛔ 不兼容策略组合 (避免同时使用)")
        for item in report['worst_combos']:
            lines.append(f"  - {item['pair']}: 兼容性={item['score']:.2f}")
        lines.append("")

    # Worst strategies
    if report['worst_strategies']:
        lines.append("### ❌ 需要关注的策略")
        for item in report['worst_strategies']:
            lines.append(
                f"  - {item.get('strategy_name', '?')}: "
                f"失败{item['total_failures']}次, "
                f"最差体制={item.get('worst_regime', '?')}, "
                f"平均超额={item.get('avg_excess_return', 0)}%"
            )
        lines.append("")

    # Proposals
    if report['proposals']:
        lines.append("### 💡 改进建议 (由学习系统自动生成)")
        for p in report['proposals'][:5]:
            icon = {'retire': '🗑️', 'restrict': '🔒', 'update': '🔧', 'incompatible': '⛔'}.get(p['action'], '💡')
            lines.append(f"  {icon} [{p['action']}] {p['reason']}")
        lines.append("")

    lines.extend([
        "### ⚠️ 学习指引",
        "1. 请认真参考上述学习报告，不要重复已知的失败模式",
        "2. 如果你要推荐一个被标记为「不兼容」的策略组合，必须解释为什么这次不同",
        "3. 在strategy_updates中，你可以根据学习报告提出retire/update建议",
    ])

    return '\n'.join(lines)
