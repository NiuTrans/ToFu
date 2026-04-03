"""lib/trading_autopilot/backtest_learner.py — Backtest-Driven Failure Learning Engine.

The MEMORY that learns from every backtest to improve future decisions.

Unlike the live strategy_learner (which only learns from real deployments),
this module learns from backtests — enabling rapid iteration:

  1. Run intel-aware backtest → get decision records
  2. Analyze which strategies worked in which regimes
  3. Identify strategy combinations that consistently fail
  4. Auto-generate improvement proposals (restrict, retire, update logic)
  5. Feed learned restrictions back into the strategy registry
  6. Track learning progress over multiple backtest iterations

Key insight: backtesting lets us test thousands of decision points in
seconds, whereas live learning requires weeks/months of real outcomes.
This accelerates the learning loop by 100x.

Architecture:
  BacktestLearningSession  — one learning session from one backtest
  aggregate_backtest_learning — combine multiple backtests into unified learning
  auto_update_strategies   — apply learned restrictions to strategy DB
  build_backtest_learning_report — formatted report for LLM consumption
  get_strategy_regime_matrix — strategy × regime effectiveness heatmap
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'BacktestLearningSession',
    'analyze_backtest_decisions',
    'aggregate_backtest_learning',
    'auto_update_strategies',
    'evolve_strategy_logic',
    'build_backtest_learning_report',
    'get_strategy_regime_matrix',
    'run_full_learning_cycle',
]


# ═══════════════════════════════════════════════════════════
#  Learning Session
# ═══════════════════════════════════════════════════════════

class BacktestLearningSession:
    """Results of learning from a single backtest run.

    Captures:
      - Per-strategy effectiveness in each regime
      - Strategy combo performance
      - Identified failure patterns
      - Improvement proposals
    """

    def __init__(self, backtest_id: str = ''):
        self.backtest_id = backtest_id or datetime.now().strftime('bt_%Y%m%d_%H%M%S')
        self.created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # ── Strategy × Regime effectiveness ──
        # {strategy_type: {regime: {wins, losses, total_return}}}
        self.strategy_regime_matrix: dict[str, dict[str, dict[str, Any]]] = defaultdict(
            lambda: defaultdict(lambda: {'wins': 0, 'losses': 0, 'total_return': 0.0})
        )

        # ── Strategy combo performance ──
        # {combo_key: {wins, losses, avg_return}}
        self.combo_performance: dict[str, dict[str, Any]] = defaultdict(
            lambda: {'wins': 0, 'losses': 0, 'returns': []}
        )

        # ── Failure patterns ──
        self.failure_patterns: list[dict[str, Any]] = []

        # ── Improvement proposals ──
        self.proposals: list[dict[str, Any]] = []

        # ── Summary stats ──
        self.total_decisions: int = 0
        self.profitable_decisions: int = 0
        self.loss_decisions: int = 0

    def to_dict(self) -> dict[str, Any]:
        # Convert defaultdicts to regular dicts for JSON serialization
        srm = {}
        for stype, regimes in self.strategy_regime_matrix.items():
            srm[stype] = {}
            for regime, stats in regimes.items():
                srm[stype][regime] = dict(stats)

        combo_perf = {}
        for key, stats in self.combo_performance.items():
            combo_perf[key] = {
                'wins': stats['wins'],
                'losses': stats['losses'],
                'avg_return': (
                    round(sum(stats['returns']) / len(stats['returns']), 3)
                    if stats['returns'] else 0
                ),
                'sample_count': len(stats['returns']),
            }

        return {
            'backtest_id': self.backtest_id,
            'created_at': self.created_at,
            'total_decisions': self.total_decisions,
            'profitable_decisions': self.profitable_decisions,
            'loss_decisions': self.loss_decisions,
            'win_rate_pct': round(
                self.profitable_decisions / max(self.total_decisions, 1) * 100, 1
            ),
            'strategy_regime_matrix': srm,
            'combo_performance': combo_perf,
            'failure_patterns': self.failure_patterns,
            'proposals': self.proposals,
        }


# ═══════════════════════════════════════════════════════════
#  Backtest Decision Analysis
# ═══════════════════════════════════════════════════════════

def analyze_backtest_decisions(
    decision_records: list[dict[str, Any]],
    backtest_result: dict[str, Any] | None = None,
    backtest_id: str = '',
) -> BacktestLearningSession:
    """Analyze decision records from an intel-aware backtest to extract learning.

    Takes the decision_records list from IntelBacktestEngine and computes:
      1. Strategy × regime effectiveness matrix
      2. Strategy combo performance
      3. Failure pattern identification
      4. Auto-generated improvement proposals

    Args:
        decision_records: List of IntelDecisionRecord.to_dict() outputs.
        backtest_result:  Optional full backtest result dict (for overall metrics).
        backtest_id:      Optional identifier for this learning session.

    Returns:
        BacktestLearningSession with all learning data.
    """
    session = BacktestLearningSession(backtest_id)

    if not decision_records:
        return session

    # ── Step 1: Evaluate each decision ──
    for i, record in enumerate(decision_records):
        ret = record.get('return_pct', 0)
        regime = record.get('regime', 'unknown')
        strategies = record.get('selected_strategies', [])
        is_profitable = ret > 0

        session.total_decisions += 1
        if is_profitable:
            session.profitable_decisions += 1
        else:
            session.loss_decisions += 1

        # ── Update strategy × regime matrix ──
        for stype in strategies:
            cell = session.strategy_regime_matrix[stype][regime]
            if is_profitable:
                cell['wins'] += 1
            else:
                cell['losses'] += 1
            cell['total_return'] += ret

        # ── Update combo performance ──
        combo_key = '+'.join(sorted(strategies))
        if combo_key:
            combo = session.combo_performance[combo_key]
            if is_profitable:
                combo['wins'] += 1
            else:
                combo['losses'] += 1
            combo['returns'].append(ret)

    # ── Step 2: Identify failure patterns ──
    session.failure_patterns = _identify_failure_patterns(session)

    # ── Step 3: Generate improvement proposals ──
    session.proposals = _generate_backtest_proposals(session)

    logger.info(
        '[BacktestLearner] Analyzed %d decisions: %.1f%% win rate, '
        '%d failure patterns, %d proposals',
        session.total_decisions,
        session.profitable_decisions / max(session.total_decisions, 1) * 100,
        len(session.failure_patterns),
        len(session.proposals),
    )

    return session


def _identify_failure_patterns(session: BacktestLearningSession) -> list[dict[str, Any]]:
    """Identify recurring failure patterns from the strategy-regime matrix."""
    patterns = []

    for stype, regimes in session.strategy_regime_matrix.items():
        for regime, stats in regimes.items():
            total = stats['wins'] + stats['losses']
            if total < 3:
                continue

            win_rate = stats['wins'] / total
            avg_return = stats['total_return'] / total

            # Pattern: strategy consistently fails in a specific regime
            if win_rate < 0.35 and total >= 3:
                patterns.append({
                    'type': 'regime_mismatch',
                    'strategy_type': stype,
                    'regime': regime,
                    'win_rate': round(win_rate, 3),
                    'avg_return': round(avg_return, 3),
                    'sample_count': total,
                    'severity': 'high' if win_rate < 0.25 else 'medium',
                    'description': (
                        f"策略类型「{stype}」在{regime}体制下表现很差："
                        f"胜率{win_rate:.0%}，平均收益{avg_return:.2%}，"
                        f"基于{total}次决策。建议在此体制下禁用。"
                    ),
                })

    # Check combo failures
    for combo_key, stats in session.combo_performance.items():
        total = stats['wins'] + stats['losses']
        if total < 3:
            continue

        win_rate = stats['wins'] / total
        if win_rate < 0.30:
            avg_ret = sum(stats['returns']) / len(stats['returns']) if stats['returns'] else 0
            patterns.append({
                'type': 'bad_combo',
                'combo': combo_key,
                'win_rate': round(win_rate, 3),
                'avg_return': round(avg_ret, 3),
                'sample_count': total,
                'severity': 'high' if win_rate < 0.20 else 'medium',
                'description': (
                    f"策略组合「{combo_key}」胜率仅{win_rate:.0%}，"
                    f"平均收益{avg_ret:.2%}。建议避免此组合。"
                ),
            })

    # Sort by severity
    _SEV = {'high': 0, 'medium': 1, 'low': 2}
    patterns.sort(key=lambda p: _SEV.get(p.get('severity', 'low'), 3))

    return patterns


def _generate_backtest_proposals(
    session: BacktestLearningSession,
) -> list[dict[str, Any]]:
    """Generate improvement proposals from failure patterns."""
    proposals = []

    for pattern in session.failure_patterns:
        if pattern['type'] == 'regime_mismatch':
            if pattern['severity'] == 'high':
                proposals.append({
                    'action': 'restrict',
                    'target': pattern['strategy_type'],
                    'regime': pattern['regime'],
                    'reason': pattern['description'],
                    'evidence': {
                        'win_rate': pattern['win_rate'],
                        'avg_return': pattern['avg_return'],
                        'sample_count': pattern['sample_count'],
                    },
                    'confidence': 'high' if pattern['sample_count'] >= 5 else 'medium',
                })

        elif pattern['type'] == 'bad_combo':
            strategies = pattern['combo'].split('+')
            proposals.append({
                'action': 'incompatible_combo',
                'target': strategies,
                'reason': pattern['description'],
                'evidence': {
                    'win_rate': pattern['win_rate'],
                    'avg_return': pattern['avg_return'],
                    'sample_count': pattern['sample_count'],
                },
                'confidence': 'high' if pattern['sample_count'] >= 5 else 'medium',
            })

    # ── Check for strategies that fail globally (across all regimes) ──
    for stype, regimes in session.strategy_regime_matrix.items():
        total_wins = sum(r['wins'] for r in regimes.values())
        total_losses = sum(r['losses'] for r in regimes.values())
        total = total_wins + total_losses

        if total >= 5 and total_losses > 0:
            global_wr = total_wins / total
            if global_wr < 0.30:
                total_ret = sum(r['total_return'] for r in regimes.values())
                proposals.append({
                    'action': 'retire_type',
                    'target': stype,
                    'reason': (
                        f"策略类型「{stype}」全局胜率仅{global_wr:.0%}，"
                        f"总收益{total_ret:.2%}，基于{total}次决策。"
                        f"建议审查并可能替换此类策略。"
                    ),
                    'evidence': {
                        'global_win_rate': round(global_wr, 3),
                        'total_decisions': total,
                        'regime_count': len(regimes),
                    },
                    'confidence': 'high' if total >= 10 else 'medium',
                })

            # ── Check for strategies needing logic update ──
            elif global_wr < 0.45 and total >= 5:
                proposals.append({
                    'action': 'update_logic',
                    'target': stype,
                    'reason': (
                        f"策略类型「{stype}」胜率{global_wr:.0%}偏低，"
                        f"但未达退役标准。建议优化策略逻辑参数。"
                    ),
                    'evidence': {
                        'global_win_rate': round(global_wr, 3),
                        'total_decisions': total,
                    },
                    'confidence': 'medium',
                })

    return proposals


# ═══════════════════════════════════════════════════════════
#  Multi-Backtest Aggregation
# ═══════════════════════════════════════════════════════════

def aggregate_backtest_learning(
    sessions: list[BacktestLearningSession],
) -> dict[str, Any]:
    """Aggregate learning from multiple backtest sessions.

    Used when running backtests across different assets, time periods,
    or configuration variants. Combines all sessions into a unified
    learning report with high-confidence conclusions.

    Returns:
        {
            total_sessions:    number of backtests,
            total_decisions:   total decisions across all backtests,
            aggregated_matrix: strategy × regime with enough samples,
            confirmed_failures: patterns that appear in multiple sessions,
            high_confidence_proposals: proposals with cross-session evidence,
        }
    """
    if not sessions:
        return {'total_sessions': 0, 'error': 'No sessions to aggregate'}

    # ── Merge strategy-regime matrices ──
    merged_matrix: dict[str, dict[str, dict[str, Any]]] = defaultdict(
        lambda: defaultdict(lambda: {'wins': 0, 'losses': 0, 'total_return': 0.0})
    )

    total_decisions = 0
    total_profitable = 0

    for session in sessions:
        total_decisions += session.total_decisions
        total_profitable += session.profitable_decisions

        for stype, regimes in session.strategy_regime_matrix.items():
            for regime, stats in regimes.items():
                cell = merged_matrix[stype][regime]
                cell['wins'] += stats['wins']
                cell['losses'] += stats['losses']
                cell['total_return'] += stats['total_return']

    # ── Compute aggregated win rates ──
    agg_matrix = {}
    for stype, regimes in merged_matrix.items():
        agg_matrix[stype] = {}
        for regime, stats in regimes.items():
            total = stats['wins'] + stats['losses']
            agg_matrix[stype][regime] = {
                'wins': stats['wins'],
                'losses': stats['losses'],
                'total': total,
                'win_rate': round(stats['wins'] / max(total, 1), 3),
                'avg_return': round(stats['total_return'] / max(total, 1), 4),
            }

    # ── Find confirmed failure patterns (appear in 2+ sessions) ──
    pattern_counts: dict[str, int] = defaultdict(int)
    all_patterns: dict[str, dict[str, Any]] = {}

    for session in sessions:
        for pattern in session.failure_patterns:
            key = f"{pattern['type']}:{pattern.get('strategy_type', '')}:{pattern.get('regime', '')}:{pattern.get('combo', '')}"
            pattern_counts[key] += 1
            all_patterns[key] = pattern

    confirmed = [
        {**all_patterns[key], 'session_count': count}
        for key, count in pattern_counts.items()
        if count >= 2  # confirmed = appears in 2+ sessions
    ]

    # ── High-confidence proposals (cross-session evidence) ──
    proposal_counts: dict[str, int] = defaultdict(int)
    all_proposals: dict[str, dict[str, Any]] = {}

    for session in sessions:
        for proposal in session.proposals:
            key = f"{proposal['action']}:{proposal['target']}"
            proposal_counts[key] += 1
            all_proposals[key] = proposal

    high_conf_proposals = [
        {**all_proposals[key], 'session_count': count, 'confidence': 'high'}
        for key, count in proposal_counts.items()
        if count >= 2
    ]

    return {
        'total_sessions': len(sessions),
        'total_decisions': total_decisions,
        'overall_win_rate': round(total_profitable / max(total_decisions, 1) * 100, 1),
        'aggregated_matrix': agg_matrix,
        'confirmed_failures': confirmed,
        'high_confidence_proposals': high_conf_proposals,
    }


# ═══════════════════════════════════════════════════════════
#  Auto-Update Strategies from Learning
# ═══════════════════════════════════════════════════════════

def auto_update_strategies(
    db: Any,
    session: BacktestLearningSession,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Apply learned restrictions to the strategy database.

    Only applies changes with high confidence (5+ samples, win rate < 30%).
    Changes:
      - restrict: mark strategy as restricted for specific regimes
      - retire: set status='retired' for consistently failing strategies
      - incompatible: record pair incompatibility

    Args:
        db:       Database connection.
        session:  Learning session with proposals.
        dry_run:  If True, only return what WOULD change (default=True for safety).

    Returns:
        {
            applied:  list of changes made (or would be made if dry_run),
            skipped:  list of proposals skipped (insufficient confidence),
        }
    """
    applied = []
    skipped = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for proposal in session.proposals:
        evidence = proposal.get('evidence', {})
        sample_count = evidence.get('sample_count', evidence.get('total_decisions', 0))
        confidence = proposal.get('confidence', 'low')

        # Only apply high-confidence proposals
        if confidence != 'high' or sample_count < 5:
            skipped.append({
                'proposal': proposal,
                'reason': f"信心不足 (confidence={confidence}, samples={sample_count})",
            })
            continue

        action = proposal['action']
        target = proposal['target']

        if action == 'restrict' and isinstance(target, str):
            regime = proposal.get('regime', '')
            if not regime:
                skipped.append({'proposal': proposal, 'reason': 'No regime specified'})
                continue

            change = {
                'action': 'restrict',
                'target_type': target,
                'regime': regime,
                'reason': proposal.get('reason', ''),
            }

            if not dry_run:
                # Record restriction in strategy_failures table
                strategies = db.execute(
                    "SELECT id, name FROM trading_strategies WHERE type=? AND status='active'",
                    (target,)
                ).fetchall()
                for s in strategies:
                    s = dict(s)
                    db.execute(
                        '''INSERT INTO trading_strategy_failures
                           (strategy_id, strategy_name, cycle_id, market_regime,
                            actual_return_pct, excess_return_pct, failure_notes, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (s['id'], s['name'], session.backtest_id, regime,
                         evidence.get('avg_return', 0) * 100,
                         evidence.get('avg_return', 0) * 100,
                         f"[BacktestLearner] Auto-restricted: {proposal['reason']}",
                         now)
                    )
                db.commit()

            applied.append(change)

        elif action == 'retire_type' and isinstance(target, str):
            change = {
                'action': 'retire_type',
                'target_type': target,
                'reason': proposal.get('reason', ''),
            }

            if not dry_run:
                db.execute(
                    "UPDATE trading_strategies SET status='retired', "
                    "result=?, updated_at=? WHERE type=? AND status='active'",
                    (f"[BacktestLearner] Auto-retired: {proposal['reason']}", now, target)
                )
                db.commit()

            applied.append(change)

        elif action == 'incompatible_combo' and isinstance(target, list):
            change = {
                'action': 'incompatible_combo',
                'strategies': target,
                'reason': proposal.get('reason', ''),
            }

            if not dry_run:
                # Record negative compatibility for all pairs in the combo
                for i in range(len(target)):
                    for j in range(i + 1, len(target)):
                        pair_key = f"{target[i]}_{target[j]}"
                        db.execute(
                            '''INSERT OR REPLACE INTO trading_strategy_compatibility
                               (pair_key, strategy_id_a, strategy_id_b,
                                compatibility_score, sample_count, updated_at)
                               VALUES (?, 0, 0, ?, ?, ?)''',
                            (pair_key, -0.5, sample_count, now)
                        )
                db.commit()

            applied.append(change)

        else:
            skipped.append({
                'proposal': proposal,
                'reason': f"Unknown action: {action}",
            })

    logger.info(
        '[BacktestLearner] auto_update: %d applied, %d skipped (dry_run=%s)',
        len(applied), len(skipped), dry_run,
    )

    return {
        'applied': applied,
        'skipped': skipped,
        'dry_run': dry_run,
    }


# ═══════════════════════════════════════════════════════════
#  Reports & Visualization Data
# ═══════════════════════════════════════════════════════════

def build_backtest_learning_report(session: BacktestLearningSession) -> str:
    """Build a human-readable learning report from backtest analysis.

    Formatted for injection into the autopilot mega-prompt so the LLM
    knows what the system learned from backtesting.
    """
    s = session
    lines = [
        "",
        "═══════════════════════════════════════",
        "## 回测学习报告 (Backtest Learning Report)",
        "═══════════════════════════════════════",
        "",
        "### 📊 统计总览",
        f"  总决策次数: {s.total_decisions}",
        f"  盈利决策: {s.profitable_decisions} ({s.profitable_decisions / max(s.total_decisions, 1) * 100:.1f}%)",
        f"  亏损决策: {s.loss_decisions}",
        "",
    ]

    # ── Strategy × Regime heatmap ──
    if s.strategy_regime_matrix:
        lines.append("### 📈 策略×体制有效性矩阵")
        for stype, regimes in s.strategy_regime_matrix.items():
            lines.append(f"\n  **{stype}:**")
            for regime, stats in sorted(regimes.items()):
                total = stats['wins'] + stats['losses']
                wr = stats['wins'] / max(total, 1)
                icon = '✅' if wr >= 0.55 else '⚠️' if wr >= 0.40 else '❌'
                lines.append(
                    f"    {icon} {regime}: 胜率{wr:.0%} "
                    f"({stats['wins']}W/{stats['losses']}L, 收益{stats['total_return']:.2%})"
                )
        lines.append("")

    # ── Failure patterns ──
    if s.failure_patterns:
        lines.append(f"### ❌ 发现{len(s.failure_patterns)}个失败模式")
        for p in s.failure_patterns[:10]:
            sev_icon = '🔴' if p['severity'] == 'high' else '🟡'
            lines.append(f"  {sev_icon} {p['description']}")
        lines.append("")

    # ── Proposals ──
    if s.proposals:
        lines.append(f"### 💡 {len(s.proposals)}条改进建议")
        for p in s.proposals[:10]:
            action_icon = {
                'restrict': '🔒', 'retire_type': '🗑️',
                'incompatible_combo': '⛔', 'update_logic': '🔧',
            }.get(p['action'], '💡')
            lines.append(f"  {action_icon} [{p['action']}] {p['reason']}")
        lines.append("")

    return '\n'.join(lines)


def evolve_strategy_logic(
    db: Any,
    session: BacktestLearningSession,
    llm: Any = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Use LLM to rewrite underperforming strategy logic based on failure patterns.

    This goes beyond restrict/retire — it REWRITES the strategy.
    For each strategy with 'update_logic' proposals:
      1. Collects the strategy's current logic + failure evidence
      2. Asks LLM to propose an improved logic string
      3. Optionally writes the improved logic back to DB

    Args:
        db:       Database connection.
        session:  Learning session with proposals.
        llm:      Optional LLMService for LLM calls. Defaults to smart_chat.
        dry_run:  If True, only return proposed rewrites (default=True).

    Returns:
        {
            rewrites:  list of {strategy_id, name, old_logic, new_logic, reason},
            skipped:   list of proposals that didn't qualify,
        }
    """
    if llm is not None:
        _chat_fn = llm.chat
    else:
        from lib.llm_dispatch import smart_chat
        _chat_fn = smart_chat

    update_proposals = [
        p for p in session.proposals if p.get('action') == 'update_logic'
    ]

    if not update_proposals:
        return {'rewrites': [], 'skipped': [], 'reason': 'No update_logic proposals'}

    rewrites = []
    skipped = []
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    for proposal in update_proposals:
        target_type = proposal.get('target', '')
        evidence = proposal.get('evidence', {})
        global_wr = evidence.get('global_win_rate', 0)

        # Find matching strategies in DB
        strategies = db.execute(
            "SELECT id, name, type, logic FROM trading_strategies "
            "WHERE type = ? AND status = 'active'",
            (target_type,)
        ).fetchall()

        if not strategies:
            skipped.append({'proposal': proposal, 'reason': f'No active strategies of type {target_type}'})
            continue

        for s in strategies:
            s = dict(s)
            old_logic = s.get('logic', '')

            # Build failure context for the LLM
            regime_data = dict(session.strategy_regime_matrix.get(target_type, {}))
            regime_summary = []
            for regime, stats in regime_data.items():
                total = stats['wins'] + stats['losses']
                if total > 0:
                    wr = stats['wins'] / total
                    regime_summary.append(
                        f"  - {regime}: 胜率{wr:.0%} ({stats['wins']}胜/{stats['losses']}负, "
                        f"收益{stats['total_return']:.2%})"
                    )

            # Get bad combos involving this type
            bad_combos = [
                p for p in session.failure_patterns
                if p.get('type') == 'bad_combo'
                and target_type in p.get('combo', '')
            ]
            combo_warnings = []
            for bc in bad_combos[:3]:
                combo_warnings.append(f"  - 组合「{bc['combo']}」胜率仅{bc['win_rate']:.0%}")

            prompt = f"""你是一个量化策略优化专家。以下策略在回测中表现不佳，请根据失败模式重写策略逻辑。

## 当前策略
- 名称: {s['name']}
- 类型: {target_type}
- 当前逻辑: {old_logic}
- 全局胜率: {global_wr:.0%}

## 各市场体制下的表现
{chr(10).join(regime_summary) if regime_summary else '  暂无数据'}

## 失败的策略组合
{chr(10).join(combo_warnings) if combo_warnings else '  暂无'}

## 要求
1. 保持策略类型({target_type})不变
2. 针对表现最差的市场体制重点优化
3. 修正导致与其他策略不兼容的逻辑
4. 输出格式: 只返回新的策略逻辑描述（一段中文文本，200字以内）
5. 不要输出任何代码，只描述逻辑规则

请直接输出新的策略逻辑:"""

            try:
                new_logic, _ = _chat_fn(
                    messages=[{'role': 'user', 'content': prompt}],
                    max_tokens=512, temperature=0.3,
                    capability='cheap',
                    log_prefix='[StrategyEvolve]',
                )

                if not new_logic or len(new_logic.strip()) < 10:
                    skipped.append({
                        'strategy_id': s['id'], 'name': s['name'],
                        'reason': 'LLM returned empty/short response',
                    })
                    continue

                new_logic = new_logic.strip()

                rewrite_entry = {
                    'strategy_id': s['id'],
                    'name': s['name'],
                    'type': target_type,
                    'old_logic': old_logic,
                    'new_logic': new_logic,
                    'reason': proposal.get('reason', ''),
                    'evidence': evidence,
                }

                if not dry_run:
                    # Write updated logic to DB and record the evolution
                    db.execute(
                        "UPDATE trading_strategies SET logic = ?, "
                        "result = ?, updated_at = ? WHERE id = ?",
                        (new_logic,
                         f"[BacktestLearner] Logic rewritten: old_wr={global_wr:.0%}. "
                         f"Previous logic: {old_logic[:200]}",
                         now, s['id'])
                    )
                    # Record in strategy_failures as an evolution event
                    db.execute(
                        '''INSERT INTO trading_strategy_failures
                           (strategy_id, strategy_name, cycle_id, market_regime,
                            actual_return_pct, excess_return_pct, failure_notes, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                        (s['id'], s['name'], session.backtest_id, 'logic_evolution',
                         0, 0,
                         f"[LOGIC_REWRITE] Old: {old_logic[:300]} → New: {new_logic[:300]}",
                         now)
                    )
                    db.commit()
                    rewrite_entry['applied'] = True
                else:
                    rewrite_entry['applied'] = False

                rewrites.append(rewrite_entry)

            except Exception as e:
                logger.warning('[StrategyEvolve] LLM rewrite failed for %s: %s',
                               s['name'], e, exc_info=True)
                skipped.append({
                    'strategy_id': s['id'], 'name': s['name'],
                    'reason': f'LLM error: {e}',
                })

    logger.info(
        '[BacktestLearner] evolve_strategy_logic: %d rewrites, %d skipped (dry_run=%s)',
        len(rewrites), len(skipped), dry_run,
    )

    return {
        'rewrites': rewrites,
        'skipped': skipped,
        'dry_run': dry_run,
    }


def get_strategy_regime_matrix(
    session: BacktestLearningSession,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Get the strategy × regime effectiveness matrix as a nested dict.

    Returns:
        {
            strategy_type: {
                regime: {
                    wins, losses, total, win_rate, avg_return,
                    rating: 'good' | 'neutral' | 'bad'
                }
            }
        }
    """
    matrix = {}
    for stype, regimes in session.strategy_regime_matrix.items():
        matrix[stype] = {}
        for regime, stats in regimes.items():
            total = stats['wins'] + stats['losses']
            wr = stats['wins'] / max(total, 1)
            avg_ret = stats['total_return'] / max(total, 1)

            rating = 'good' if wr >= 0.55 else 'neutral' if wr >= 0.40 else 'bad'

            matrix[stype][regime] = {
                'wins': stats['wins'],
                'losses': stats['losses'],
                'total': total,
                'win_rate': round(wr, 3),
                'avg_return': round(avg_ret, 4),
                'rating': rating,
            }

    return matrix


# ═══════════════════════════════════════════════════════════
#  End-to-End Learning Pipeline
# ═══════════════════════════════════════════════════════════

def run_full_learning_cycle(
    db: Any,
    symbols: list[str],
    *,
    start_date: str = '',
    end_date: str = '',
    crawl_first: bool = True,
    auto_apply: bool = False,
    auto_evolve: bool = False,
    progress_callback: callable | None = None,
    llm: Any = None,
) -> dict[str, Any]:
    """End-to-end automated learning pipeline.

    Chains: mega-crawl → intel-backtest → analyze → learn → evolve → re-validate.

    This is the master orchestrator that the user's request describes:
      1. Crawl intelligence from the web (with time categorization)
      2. Run intel-aware backtest (with time-locked DB)
      3. Analyze which strategies worked/failed in which regimes
      4. Auto-apply restrictions for failing strategies
      5. LLM-rewrite logic for underperforming strategies
      6. Re-run backtest to validate improvements

    Args:
        db:                Database connection.
        symbols:           Asset codes to backtest.
        start_date:        Backtest start (default 1yr ago).
        end_date:          Backtest end (default today).
        crawl_first:       Whether to mega-crawl intel before backtest.
        auto_apply:        Apply restrictions/retirements automatically.
        auto_evolve:       Use LLM to rewrite strategy logic.
        progress_callback: Optional fn(phase, detail) for UI progress.
        llm:               Optional LLMService for strategy evolution.

    Returns:
        {
            phases:   {crawl, backtest_before, learning, evolution, backtest_after},
            summary:  high-level improvement metrics,
        }
    """
    from datetime import timedelta as _td

    if not start_date:
        start_date = (datetime.now() - _td(days=365)).strftime('%Y-%m-%d')
    if not end_date:
        end_date = datetime.now().strftime('%Y-%m-%d')

    phases = {}

    def _progress(phase, detail=''):
        if progress_callback:
            try:
                progress_callback(phase, detail)
            except Exception as _cb_err:
                logger.debug('[BacktestLearner] Progress callback failed: %s', _cb_err)

    # ══════════════════════════════════════════
    #  Phase 1: Mega Crawl (optional)
    # ══════════════════════════════════════════
    if crawl_first:
        _progress('crawl', 'Starting mega crawl...')
        try:
            from lib.search import perform_web_search
            from lib.trading.intel_mega_crawler import (
                MegaCrawlConfig,
                run_mega_crawl,
            )

            crawl_config = MegaCrawlConfig(
                start_date=start_date,
                end_date=end_date,
                max_workers=4,
            )
            crawl_result = run_mega_crawl(
                db, perform_web_search, crawl_config,
                progress_callback=lambda d, t, c, m: _progress('crawl', f'{d}/{t} {m}'),
            )
            phases['crawl'] = {
                'status': crawl_result.get('status', 'unknown'),
                'total_fetched': crawl_result.get('total_fetched', 0),
                'duration_seconds': crawl_result.get('duration_seconds', 0),
                'date_confidence': crawl_result.get('date_confidence', {}),
            }
        except Exception as e:
            logger.error('[LearningCycle] Crawl failed: %s', e, exc_info=True)
            phases['crawl'] = {'status': 'error', 'error': str(e)}
    else:
        phases['crawl'] = {'status': 'skipped'}

    # ══════════════════════════════════════════
    #  Phase 2: Initial Intel-Aware Backtest
    # ══════════════════════════════════════════
    _progress('backtest_before', 'Running initial intel-aware backtest...')
    try:
        from lib.trading import fetch_price_history
        from lib.trading_backtest_engine.intel_backtest import (
            IntelBacktestConfig,
            run_intel_backtest,
        )

        # Fetch price data
        asset_prices = {}
        for code in symbols:
            try:
                navs = fetch_price_history(code, start_date, end_date)
                if navs and len(navs) >= 60:
                    asset_prices[code] = navs
            except Exception as e:
                logger.warning('[LearningCycle] Price fetch failed for %s: %s', code, e)

        if not asset_prices:
            return {'error': 'No valid price data for any symbol', 'phases': phases}

        bt_config = IntelBacktestConfig(
            initial_capital=100000,
            intel_enabled=True,
            meta_strategy_enabled=True,
            decision_frequency=5,
        )
        bt_result_before = run_intel_backtest(db, asset_prices, config=bt_config)
        decision_records = bt_result_before.get('intel_analysis', {}).get('decision_records', [])

        phases['backtest_before'] = {
            'status': 'completed',
            'total_return_pct': bt_result_before.get('summary', {}).get('total_return_pct', 0),
            'max_drawdown_pct': bt_result_before.get('summary', {}).get('max_drawdown_pct', 0),
            'sharpe_ratio': bt_result_before.get('summary', {}).get('sharpe_ratio', 0),
            'total_decisions': len(decision_records),
        }

    except Exception as e:
        logger.error('[LearningCycle] Backtest failed: %s', e, exc_info=True)
        phases['backtest_before'] = {'status': 'error', 'error': str(e)}
        return {'error': f'Backtest failed: {e}', 'phases': phases}

    # ══════════════════════════════════════════
    #  Phase 3: Learning Analysis
    # ══════════════════════════════════════════
    _progress('learning', 'Analyzing backtest decisions...')
    try:
        session = analyze_backtest_decisions(
            decision_records, backtest_id=f"learning_cycle_{end_date}",
        )

        phases['learning'] = {
            'status': 'completed',
            'total_decisions': session.total_decisions,
            'win_rate_pct': round(
                session.profitable_decisions / max(session.total_decisions, 1) * 100, 1
            ),
            'failure_patterns': len(session.failure_patterns),
            'proposals': len(session.proposals),
            'failure_details': [
                {
                    'type': p['type'],
                    'description': p['description'],
                    'severity': p['severity'],
                }
                for p in session.failure_patterns[:10]
            ],
            'proposal_details': [
                {
                    'action': p['action'],
                    'target': str(p['target']),
                    'reason': p['reason'],
                    'confidence': p.get('confidence', 'unknown'),
                }
                for p in session.proposals[:10]
            ],
        }

        # Auto-apply restrictions if enabled
        if auto_apply and session.proposals:
            _progress('learning', 'Auto-applying learned restrictions...')
            update_result = auto_update_strategies(db, session, dry_run=False)
            phases['learning']['auto_update'] = update_result
        elif session.proposals:
            dry_result = auto_update_strategies(db, session, dry_run=True)
            phases['learning']['dry_run'] = dry_result

    except Exception as e:
        logger.error('[LearningCycle] Learning analysis failed: %s', e, exc_info=True)
        phases['learning'] = {'status': 'error', 'error': str(e)}
        session = None

    # ══════════════════════════════════════════
    #  Phase 4: Strategy Evolution (LLM rewrite)
    # ══════════════════════════════════════════
    if auto_evolve and session:
        _progress('evolution', 'LLM-driven strategy logic rewriting...')
        try:
            evolve_result = evolve_strategy_logic(
                db, session, llm=llm, dry_run=not auto_apply,
            )
            phases['evolution'] = {
                'status': 'completed',
                'rewrites': len(evolve_result.get('rewrites', [])),
                'skipped': len(evolve_result.get('skipped', [])),
                'dry_run': evolve_result.get('dry_run', True),
                'rewrite_details': evolve_result.get('rewrites', []),
            }
        except Exception as e:
            logger.error('[LearningCycle] Evolution failed: %s', e, exc_info=True)
            phases['evolution'] = {'status': 'error', 'error': str(e)}
    else:
        phases['evolution'] = {'status': 'skipped'}

    # ══════════════════════════════════════════
    #  Phase 5: Validation Re-Backtest
    # ══════════════════════════════════════════
    if auto_apply and session:
        _progress('backtest_after', 'Re-running backtest to validate improvements...')
        try:
            bt_result_after = run_intel_backtest(db, asset_prices, config=bt_config)
            after_records = bt_result_after.get('intel_analysis', {}).get('decision_records', [])

            phases['backtest_after'] = {
                'status': 'completed',
                'total_return_pct': bt_result_after.get('summary', {}).get('total_return_pct', 0),
                'max_drawdown_pct': bt_result_after.get('summary', {}).get('max_drawdown_pct', 0),
                'sharpe_ratio': bt_result_after.get('summary', {}).get('sharpe_ratio', 0),
                'total_decisions': len(after_records),
            }
        except Exception as e:
            logger.error('[LearningCycle] Validation backtest failed: %s', e, exc_info=True)
            phases['backtest_after'] = {'status': 'error', 'error': str(e)}
    else:
        phases['backtest_after'] = {'status': 'skipped'}

    # ══════════════════════════════════════════
    #  Summary
    # ══════════════════════════════════════════
    summary = {
        'symbols': symbols,
        'period': f"{start_date} → {end_date}",
    }

    before = phases.get('backtest_before', {})
    after = phases.get('backtest_after', {})
    if before.get('status') == 'completed':
        summary['before'] = {
            'return_pct': before.get('total_return_pct', 0),
            'sharpe': before.get('sharpe_ratio', 0),
        }
    if after.get('status') == 'completed':
        summary['after'] = {
            'return_pct': after.get('total_return_pct', 0),
            'sharpe': after.get('sharpe_ratio', 0),
        }
        summary['improvement'] = {
            'return_delta': round(
                after.get('total_return_pct', 0) - before.get('total_return_pct', 0), 2
            ),
            'sharpe_delta': round(
                after.get('sharpe_ratio', 0) - before.get('sharpe_ratio', 0), 3
            ),
        }

    learning = phases.get('learning', {})
    if learning.get('status') == 'completed':
        summary['learning'] = {
            'failure_patterns_found': learning.get('failure_patterns', 0),
            'proposals_generated': learning.get('proposals', 0),
        }

    evolution = phases.get('evolution', {})
    if evolution.get('status') == 'completed':
        summary['evolution'] = {
            'strategies_rewritten': evolution.get('rewrites', 0),
        }

    logger.info(
        '[LearningCycle] Complete: before=%.2f%% return, %d patterns, %d proposals',
        before.get('total_return_pct', 0),
        learning.get('failure_patterns', 0),
        learning.get('proposals', 0),
    )

    return {
        'phases': phases,
        'summary': summary,
    }
