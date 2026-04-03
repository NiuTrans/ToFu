"""lib/trading_autopilot/adaptive_decision_engine.py — Unified Adaptive Decision Engine.

The BRAIN that unifies strategies with decisions.

Before this module, the decision system and strategies were separate:
  - meta_strategy.py selected strategies from a DB table
  - The backtest engine had its own hardcoded strategies
  - Neither learned from the other

This module creates a SINGLE decision engine that:
  1. Maintains a strategy registry with real-time effectiveness scores
  2. Detects market regime from quant signals + intel features
  3. Auto-selects the BEST strategy combination for current conditions
  4. Executes combined strategy logic as a unified decision
  5. Records every decision outcome for the learning engine
  6. Auto-disables failing strategies and proposes replacements

Architecture:
  StrategyProfile      — runtime profile for each strategy (scores, restrictions)
  AdaptiveDecisionEngine — the unified brain
  StrategyRegistry     — maintains live strategy effectiveness data

Key design:
  The engine doesn't just pick strategies — it FUSES their signals.
  If trend_following says "buy" but risk_control says "reduce exposure",
  the engine resolves the conflict using priority rules + confidence weighting.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from lib.log import get_logger
from lib.trading_autopilot.meta_strategy import (
    _SUITABILITY_MATRIX,
    MarketCondition,
    detect_market_condition,
)
from lib.trading_autopilot.strategy_learner import (
    generate_improvement_proposals,
    get_strategy_compatibility_scores,
    get_strategy_failure_summary,
)

logger = get_logger(__name__)

__all__ = [
    'StrategyProfile',
    'StrategyRegistry',
    'AdaptiveDecisionEngine',
]


# ═══════════════════════════════════════════════════════════
#  Strategy Profile
# ═══════════════════════════════════════════════════════════

class StrategyProfile:
    """Runtime profile for a strategy with live effectiveness tracking."""

    def __init__(
        self,
        strategy_id: int,
        name: str,
        strategy_type: str,
        logic: str,
        source: str = 'manual',
    ):
        self.strategy_id = strategy_id
        self.name = name
        self.strategy_type = strategy_type
        self.logic = logic
        self.source = source

        # ── Live effectiveness tracking ──
        self.total_deployments: int = 0
        self.successful_deployments: int = 0
        self.failed_deployments: int = 0
        self.avg_excess_return: float = 0.0
        self.win_rate: float = 0.5  # prior = 50%

        # ── Regime-specific performance ──
        self.regime_win_rates: dict[str, float] = {}
        self.regime_deployment_counts: dict[str, int] = {}

        # ── Restrictions ──
        self.restricted_regimes: list[str] = []
        self.is_retired: bool = False
        self.is_disabled: bool = False
        self.disable_reason: str = ''

        # ── Compatibility (updated by learner) ──
        self.incompatible_with: list[int] = []  # strategy IDs

    @property
    def effectiveness_score(self) -> float:
        """Overall effectiveness score [0, 1] combining win rate + returns."""
        if self.total_deployments < 3:
            return 0.5  # insufficient data → neutral prior
        # Weighted: 60% win rate + 40% return quality
        wr = self.win_rate
        ret_quality = max(0, min(1, (self.avg_excess_return + 5) / 10))  # normalize [-5, +5] → [0, 1]
        return wr * 0.6 + ret_quality * 0.4

    def regime_effectiveness(self, regime: str) -> float:
        """Effectiveness in a specific regime."""
        if regime in self.restricted_regimes:
            return 0.0  # hard block
        if regime in self.regime_win_rates:
            count = self.regime_deployment_counts.get(regime, 0)
            if count >= 3:
                return self.regime_win_rates[regime]
        return self.effectiveness_score  # fallback to overall

    def to_dict(self) -> dict[str, Any]:
        return {
            'strategy_id': self.strategy_id,
            'name': self.name,
            'type': self.strategy_type,
            'logic': self.logic,
            'effectiveness': round(self.effectiveness_score, 3),
            'win_rate': round(self.win_rate, 3),
            'avg_excess_return': round(self.avg_excess_return, 2),
            'total_deployments': self.total_deployments,
            'restricted_regimes': self.restricted_regimes,
            'is_retired': self.is_retired,
            'is_disabled': self.is_disabled,
        }


# ═══════════════════════════════════════════════════════════
#  Strategy Registry
# ═══════════════════════════════════════════════════════════

class StrategyRegistry:
    """Maintains live strategy effectiveness data.

    Loads strategies from DB, enriches them with failure/compatibility
    data from the learning engine, and provides selection methods.
    """

    def __init__(self, db: Any):
        self.db = db
        self.profiles: dict[int, StrategyProfile] = {}
        self.compatibility_scores: dict[str, float] = {}
        self._loaded = False

    def load(self) -> None:
        """Load strategy profiles from DB + learning data."""
        db = self.db

        # Load active strategies
        strategies = db.execute(
            "SELECT * FROM trading_strategies WHERE status='active' "
            "ORDER BY updated_at DESC"
        ).fetchall()

        self.profiles.clear()
        for s in strategies:
            s = dict(s)
            profile = StrategyProfile(
                strategy_id=s['id'],
                name=s['name'],
                strategy_type=s.get('type', 'observation'),
                logic=s.get('logic', ''),
                source=s.get('source', 'manual'),
            )
            self.profiles[s['id']] = profile

        # Enrich with failure data
        try:
            failures = get_strategy_failure_summary(db)
            for sid_str, summary in failures.items():
                sid = int(sid_str)
                if sid in self.profiles:
                    p = self.profiles[sid]
                    p.failed_deployments = summary.get('total_failures', 0)
                    p.avg_excess_return = summary.get('avg_excess_return', 0.0)

                    # Update regime-specific data
                    regime_f = summary.get('regime_failures', {})
                    for regime, count in regime_f.items():
                        p.regime_deployment_counts[regime] = count
                        # Approximate regime win rate: if this is failure data,
                        # we assume failures / total in that regime
                        p.regime_win_rates[regime] = max(
                            0, 1.0 - count / max(p.total_deployments, count + 1)
                        )
        except Exception as e:
            logger.warning('[Registry] Failure data load failed: %s', e)

        # Load compatibility
        try:
            self.compatibility_scores = get_strategy_compatibility_scores(db)
            for pair_key, score in self.compatibility_scores.items():
                if score < -0.4:
                    parts = pair_key.split('_')
                    if len(parts) == 2:
                        id_a, id_b = int(parts[0]), int(parts[1])
                        if id_a in self.profiles:
                            self.profiles[id_a].incompatible_with.append(id_b)
                        if id_b in self.profiles:
                            self.profiles[id_b].incompatible_with.append(id_a)
        except Exception as e:
            logger.warning('[Registry] Compatibility data load failed: %s', e)

        # Apply improvement proposals (auto-restrict/retire)
        try:
            proposals = generate_improvement_proposals(db, min_failures=3)
            for p in proposals:
                sid = p.get('strategy_id')
                if sid and sid in self.profiles:
                    profile = self.profiles[sid]
                    if p['action'] == 'retire' and p.get('severity') == 'high':
                        profile.is_disabled = True
                        profile.disable_reason = p.get('reason', 'Auto-retired by learner')
                    elif p['action'] == 'restrict':
                        for regime in p.get('restricted_regimes', []):
                            if regime not in profile.restricted_regimes:
                                profile.restricted_regimes.append(regime)
        except Exception as e:
            logger.warning('[Registry] Proposals load failed: %s', e)

        self._loaded = True
        logger.info(
            '[Registry] Loaded %d strategy profiles (%d disabled)',
            len(self.profiles),
            sum(1 for p in self.profiles.values() if p.is_disabled),
        )

    def get_active_profiles(self, regime: str = '') -> list[StrategyProfile]:
        """Get active (non-disabled, non-restricted) profiles for a regime."""
        if not self._loaded:
            self.load()

        active = []
        for p in self.profiles.values():
            if p.is_disabled or p.is_retired:
                continue
            if regime and regime in p.restricted_regimes:
                continue
            active.append(p)
        return active

    def get_compatible_subset(
        self,
        selected_ids: list[int],
        candidate_id: int,
    ) -> bool:
        """Check if a candidate is compatible with already-selected strategies."""
        for sel_id in selected_ids:
            pair_key = f"{min(sel_id, candidate_id)}_{max(sel_id, candidate_id)}"
            score = self.compatibility_scores.get(pair_key, 0.0)
            if score < -0.3:
                return False
        return True


# ═══════════════════════════════════════════════════════════
#  Adaptive Decision Engine
# ═══════════════════════════════════════════════════════════

class AdaptiveDecisionEngine:
    """The unified brain that selects and fuses strategies based on conditions.

    Usage:
        engine = AdaptiveDecisionEngine(db)
        decision = engine.make_decision(
            quant_signals=signals,
            as_of='2025-01-15',
        )
        # decision contains: selected strategies, fused signal, confidence,
        # and explanations of why each strategy was chosen/rejected
    """

    def __init__(self, db: Any):
        self.db = db
        self.registry = StrategyRegistry(db)
        self._decision_history: list[dict[str, Any]] = []

    def make_decision(
        self,
        quant_signals: dict[str, Any] | None = None,
        as_of: str = '',
        max_strategies: int = 8,
    ) -> dict[str, Any]:
        """Make a unified adaptive decision.

        Steps:
          1. Detect market conditions (quant + intel)
          2. Load strategy registry with latest learning data
          3. Score each strategy for current conditions
          4. Select optimal combo (with compatibility checks)
          5. Fuse strategy signals into a unified direction
          6. Generate decision with full explainability

        Returns:
            {
                market_condition: MarketCondition dict,
                selected_strategies: [{profile, score, reason}],
                rejected_strategies: [{profile, reason}],
                fused_signal: {direction, confidence, components},
                decision_explanation: str,
            }
        """
        if not as_of:
            as_of = datetime.now().strftime('%Y-%m-%d')

        # Step 1: Detect market conditions
        condition = detect_market_condition(
            self.db, quant_signals=quant_signals, as_of=as_of,
        )

        # Step 2: Refresh registry
        self.registry.load()

        # Step 3: Score and rank strategies
        scored_profiles = self._score_strategies(condition)

        # Step 4: Select optimal combo
        selected, rejected = self._select_combo(
            scored_profiles, condition, max_strategies,
        )

        # Step 5: Fuse signals
        fused = self._fuse_signals(selected, condition)

        # Step 6: Build explanation
        explanation = self._build_explanation(
            condition, selected, rejected, fused,
        )

        decision = {
            'market_condition': condition.to_dict(),
            'selected_strategies': [
                {
                    'profile': s['profile'].to_dict(),
                    'selection_score': s['score'],
                    'reason': s['reason'],
                }
                for s in selected
            ],
            'rejected_strategies': [
                {
                    'profile': r['profile'].to_dict(),
                    'reason': r['reason'],
                }
                for r in rejected
            ],
            'fused_signal': fused,
            'decision_explanation': explanation,
            'as_of': as_of,
        }

        self._decision_history.append(decision)
        return decision

    def _score_strategies(
        self,
        condition: MarketCondition,
    ) -> list[dict[str, Any]]:
        """Score all active strategies for current conditions."""
        profiles = self.registry.get_active_profiles(regime=condition.regime)
        scored = []

        for profile in profiles:
            # Base suitability from matrix
            base = _SUITABILITY_MATRIX.get(
                (profile.strategy_type, condition.regime), 0.5
            )

            # Effectiveness modifier from learning history
            effectiveness = profile.regime_effectiveness(condition.regime)

            # Intel-based modifier
            intel_mod = 1.0
            if condition.risk_level in ('critical', 'high'):
                if profile.strategy_type == 'risk_control':
                    intel_mod *= 1.3
                elif profile.strategy_type == 'buy_signal':
                    intel_mod *= 0.7
            if condition.sentiment_score > 0.3 and profile.strategy_type == 'buy_signal':
                intel_mod *= 1.15
            elif condition.sentiment_score < -0.3 and profile.strategy_type == 'sell_signal':
                intel_mod *= 1.15

            # Combined score
            final_score = base * effectiveness * intel_mod

            # Penalty for many failures
            if profile.failed_deployments >= 5:
                final_score *= 0.5
            elif profile.failed_deployments >= 3:
                final_score *= 0.7

            # Build reason
            reasons = [
                f"适配度={base:.0%}({profile.strategy_type}@{condition.regime_label})",
                f"有效性={effectiveness:.0%}",
            ]
            if intel_mod != 1.0:
                reasons.append(f"情报修正×{intel_mod:.2f}")
            if profile.failed_deployments > 0:
                reasons.append(f"历史失败{profile.failed_deployments}次")

            scored.append({
                'profile': profile,
                'score': round(final_score, 3),
                'reason': '; '.join(reasons),
            })

        scored.sort(key=lambda x: x['score'], reverse=True)
        return scored

    def _select_combo(
        self,
        scored_profiles: list[dict[str, Any]],
        condition: MarketCondition,
        max_strategies: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Select the best compatible strategy combination."""
        selected: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        selected_ids: list[int] = []
        type_counts: dict[str, int] = defaultdict(int)
        MAX_PER_TYPE = 3

        for item in scored_profiles:
            profile = item['profile']

            if len(selected) >= max_strategies:
                rejected.append({
                    'profile': profile,
                    'reason': f"已选满{max_strategies}个策略",
                })
                continue

            # Type diversity check
            if type_counts[profile.strategy_type] >= MAX_PER_TYPE:
                rejected.append({
                    'profile': profile,
                    'reason': f"类型{profile.strategy_type}已选满{MAX_PER_TYPE}个",
                })
                continue

            # Compatibility check
            if not self.registry.get_compatible_subset(selected_ids, profile.strategy_id):
                rejected.append({
                    'profile': profile,
                    'reason': "与已选策略不兼容",
                })
                continue

            # Score threshold
            if item['score'] < 0.15:
                rejected.append({
                    'profile': profile,
                    'reason': f"评分{item['score']:.2f}低于阈值0.15",
                })
                continue

            selected.append(item)
            selected_ids.append(profile.strategy_id)
            type_counts[profile.strategy_type] += 1

        return selected, rejected

    def _fuse_signals(
        self,
        selected: list[dict[str, Any]],
        condition: MarketCondition,
    ) -> dict[str, Any]:
        """Fuse signals from multiple strategies into a unified direction.

        Resolution rules:
          1. risk_control has VETO power (if it says "reduce", reduce)
          2. Conflicting buy/sell resolved by confidence-weighted voting
          3. Observation strategies contribute 0 weight to direction
          4. allocation strategies modify sizing but not direction
        """
        if not selected:
            return {
                'direction': 'hold',
                'confidence': 0,
                'components': [],
            }

        buy_weight = 0.0
        sell_weight = 0.0
        hold_weight = 0.0
        risk_veto = False
        components = []

        for item in selected:
            profile = item['profile']
            score = item['score']

            if profile.strategy_type == 'risk_control':
                if condition.risk_level in ('critical', 'high'):
                    risk_veto = True
                    sell_weight += score * 2.0  # double weight for risk control
                    components.append({
                        'strategy': profile.name,
                        'type': profile.strategy_type,
                        'signal': 'reduce_exposure',
                        'weight': score * 2.0,
                    })
                else:
                    hold_weight += score * 0.5
                    components.append({
                        'strategy': profile.name,
                        'type': profile.strategy_type,
                        'signal': 'monitor',
                        'weight': score * 0.5,
                    })

            elif profile.strategy_type == 'buy_signal':
                buy_weight += score
                components.append({
                    'strategy': profile.name,
                    'type': profile.strategy_type,
                    'signal': 'buy',
                    'weight': score,
                })

            elif profile.strategy_type == 'sell_signal':
                sell_weight += score
                components.append({
                    'strategy': profile.name,
                    'type': profile.strategy_type,
                    'signal': 'sell',
                    'weight': score,
                })

            elif profile.strategy_type == 'timing':
                # Timing: direction depends on regime
                if condition.regime in ('strong_uptrend', 'uptrend', 'recovery'):
                    buy_weight += score * 0.8
                    signal = 'buy'
                elif condition.regime in ('downtrend', 'capitulation', 'distribution'):
                    sell_weight += score * 0.8
                    signal = 'sell'
                else:
                    hold_weight += score * 0.5
                    signal = 'hold'
                components.append({
                    'strategy': profile.name,
                    'type': profile.strategy_type,
                    'signal': signal,
                    'weight': score * 0.8,
                })

            elif profile.strategy_type in ('observation', 'allocation'):
                hold_weight += score * 0.3
                components.append({
                    'strategy': profile.name,
                    'type': profile.strategy_type,
                    'signal': 'observe',
                    'weight': score * 0.3,
                })

        # ── Resolve direction ──
        total = buy_weight + sell_weight + hold_weight
        if total == 0:
            return {'direction': 'hold', 'confidence': 0, 'components': components}

        # Risk veto overrides everything
        if risk_veto:
            direction = 'reduce'
            confidence = min(90, int(sell_weight / total * 100))
        elif buy_weight > sell_weight * 1.3 and buy_weight > hold_weight:
            direction = 'buy'
            confidence = min(90, int(buy_weight / total * 100))
        elif sell_weight > buy_weight * 1.3 and sell_weight > hold_weight:
            direction = 'sell'
            confidence = min(90, int(sell_weight / total * 100))
        else:
            direction = 'hold'
            confidence = min(80, int(hold_weight / total * 100))

        return {
            'direction': direction,
            'confidence': confidence,
            'buy_weight': round(buy_weight, 3),
            'sell_weight': round(sell_weight, 3),
            'hold_weight': round(hold_weight, 3),
            'risk_veto': risk_veto,
            'components': components,
        }

    def _build_explanation(
        self,
        condition: MarketCondition,
        selected: list[dict[str, Any]],
        rejected: list[dict[str, Any]],
        fused: dict[str, Any],
    ) -> str:
        """Build human-readable decision explanation."""
        lines = [
            f"## 自适应决策引擎报告 ({condition.as_of})",
            "",
            "### 市场状态",
            f"  体制: {condition.regime_label} | 波动: {condition.volatility} | "
            f"风险: {condition.risk_level}",
            f"  情绪: {condition.sentiment_score:+.2f} | "
            f"政策活跃: {condition.policy_signal:.2f} | "
            f"情报流速: {condition.intel_velocity:.1f}条/天",
            "",
            f"### 选中策略 ({len(selected)}个)",
        ]

        for item in selected:
            p = item['profile']
            lines.append(
                f"  ✅ [{p.strategy_type}] {p.name} "
                f"(分数={item['score']:.2f}, 有效性={p.effectiveness_score:.0%})"
            )
            lines.append(f"     理由: {item['reason']}")

        if rejected:
            lines.append(f"\n### 排除策略 ({len(rejected)}个)")
            for item in rejected[:5]:  # show top 5 rejections
                p = item['profile']
                lines.append(f"  ❌ {p.name}: {item['reason']}")

        lines.extend([
            "",
            "### 融合信号",
            f"  方向: {fused['direction']} | 信心: {fused['confidence']}%",
            f"  买入权重: {fused.get('buy_weight', 0):.2f} | "
            f"卖出权重: {fused.get('sell_weight', 0):.2f} | "
            f"持有权重: {fused.get('hold_weight', 0):.2f}",
        ])

        if fused.get('risk_veto'):
            lines.append("  ⚠️ 风险控制策略行使了否决权")

        return '\n'.join(lines)

    def get_decision_summary(self) -> dict[str, Any]:
        """Get summary of all decisions made."""
        if not self._decision_history:
            return {'total_decisions': 0}

        directions = defaultdict(int)
        for d in self._decision_history:
            directions[d['fused_signal']['direction']] += 1

        return {
            'total_decisions': len(self._decision_history),
            'direction_distribution': dict(directions),
            'avg_confidence': round(
                sum(d['fused_signal']['confidence'] for d in self._decision_history)
                / len(self._decision_history), 1
            ),
            'avg_strategies_per_decision': round(
                sum(len(d['selected_strategies']) for d in self._decision_history)
                / len(self._decision_history), 1
            ),
        }


def build_adaptive_decision_prompt(decision: dict[str, Any]) -> str:
    """Build a prompt section from an AdaptiveDecisionEngine result.

    Unlike the simpler ``build_adaptive_prompt_section`` in meta_strategy.py
    which only shows selected strategies, this version ALSO includes:
      - The fused trading signal (direction + confidence)
      - Risk veto status
      - Rejected strategies with reasons
      - Full decision explanation

    This is the recommended prompt builder for the live autopilot mega-prompt.

    Args:
        decision: Result dict from AdaptiveDecisionEngine.make_decision().

    Returns:
        Formatted prompt section string.
    """
    mc = decision.get('market_condition', {})
    fused = decision.get('fused_signal', {})
    selected = decision.get('selected_strategies', [])
    rejected = decision.get('rejected_strategies', [])

    lines = [
        "═══════════════════════════════════════",
        "## 第二部分: 自适应决策引擎 (Adaptive Decision Engine)",
        "═══════════════════════════════════════",
        "",
        f"### 📊 市场状态诊断 (截至 {mc.get('as_of', 'N/A')})",
        f"  市场体制: {_regime_label(mc.get('regime', 'unknown'))} ({mc.get('regime', 'unknown')})",
        f"  波动率: {mc.get('volatility', 'normal')}",
        f"  趋势强度: {mc.get('trend_strength', 0):+.2f} (-1=强跌, +1=强涨)",
        f"  情绪得分: {mc.get('sentiment_score', 0):+.2f} (-1=恐慌, +1=贪婪)",
        f"  风险信号: {mc.get('risk_signal', 0):.2f}",
        f"  政策活跃度: {mc.get('policy_signal', 0):.2f}",
        f"  情报流速: {mc.get('intel_velocity', 0):.1f} 条/天",
        "",
    ]

    # ── Fused trading signal (the KEY innovation) ──
    direction_labels = {
        'buy': '🟢 买入', 'sell': '🔴 卖出', 'hold': '🟡 持有',
        'reduce': '🔴 减仓(风控否决)',
    }
    dir_label = direction_labels.get(fused.get('direction', 'hold'), fused.get('direction', 'hold'))
    confidence = fused.get('confidence', 0)
    conf_bar = '█' * (confidence // 10) + '░' * (10 - confidence // 10)

    lines.extend([
        "### 🎯 融合交易信号 (Fused Signal)",
        f"  **方向: {dir_label}**  信心: [{conf_bar}] {confidence}%",
        f"  买入权重: {fused.get('buy_weight', 0):.2f} | "
        f"卖出权重: {fused.get('sell_weight', 0):.2f} | "
        f"持有权重: {fused.get('hold_weight', 0):.2f}",
    ])
    if fused.get('risk_veto'):
        lines.append("  ⚠️ **风险控制策略行使了否决权 — 不得加仓，应减少风险暴露**")
    lines.append("")

    # ── Selected strategies ──
    lines.extend([
        f"### ✅ 自动选中策略 ({len(selected)}个)",
        "_以下策略由决策引擎根据市场状态+历史学习数据动态选中。_",
        "",
    ])
    for i, s in enumerate(selected, 1):
        p = s.get('profile', {})
        score = s.get('selection_score', 0)
        bar = '█' * int(score * 10) + '░' * (10 - int(score * 10))
        lines.append(
            f"  {i}. [{p.get('type', '?')}] **{p.get('name', '?')}** "
            f"[{bar}] {score:.0%}"
        )
        lines.append(f"     逻辑: {p.get('logic', 'N/A')[:200]}")
        lines.append(f"     有效性: {p.get('effectiveness', 0):.0%} | "
                     f"胜率: {p.get('win_rate', 0):.0%}")
        lines.append(f"     选择理由: {s.get('reason', 'N/A')}")
        lines.append("")

    # ── Rejected strategies (top 5) ──
    if rejected:
        lines.append(f"### ❌ 排除策略 (前{min(5, len(rejected))}个)")
        for r in rejected[:5]:
            p = r.get('profile', {})
            lines.append(f"  - {p.get('name', '?')}: {r.get('reason', '?')}")
        lines.append("")

    # ── Usage instructions ──
    lines.extend([
        "### ⚠️ 决策纪律",
        "1. 融合信号方向=reduce时, 你**必须**优先减仓, 不得增持",
        "2. 融合信号信心<30%时, 采用最保守策略, 控制单笔仓位≤5%",
        "3. 风控否决生效时, 即使情报面利好, 也必须先降低风险暴露",
        "4. 适配分数>70%的策略为主力策略, <40%仅作参考",
        "5. 如果你认为引擎选择有误, 必须在strategy_updates中说明理由",
    ])

    return '\n'.join(lines)


def _regime_label(regime: str) -> str:
    """Human-readable regime label."""
    _LABELS = {
        'strong_uptrend': '强势上涨', 'uptrend': '上涨趋势',
        'recovery': '触底回升', 'sideways': '横盘震荡',
        'ranging': '区间震荡', 'distribution': '高位派发',
        'downtrend': '下跌趋势', 'capitulation': '恐慌抛售',
        'unknown': '未知',
    }
    return _LABELS.get(regime, regime)
