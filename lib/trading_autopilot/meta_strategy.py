"""lib/trading_autopilot/meta_strategy.py — Adaptive Meta-Strategy Selector.

The core innovation: the decision system is NO LONGER separated from
strategies.  Instead, this module dynamically selects and combines
strategies based on:

  1. Current market regime (bull/bear/sideways/volatile)
  2. Intel-derived features (sentiment, policy signals, risk signals)
  3. Historical strategy-combo performance (from strategy_learner)
  4. Strategy compatibility matrix (which combos work together)

This replaces the static "strategy groups" with a living, adaptive system
that learns which strategies to deploy in which conditions.

Architecture:
  MarketCondition    — data class capturing current market state
  detect_market_condition — reads quant signals + intel features → MarketCondition
  select_strategies  — picks the best strategy combination for current conditions
  build_adaptive_prompt_section — generates prompt context for the autopilot
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'MarketCondition',
    'detect_market_condition',
    'select_strategies',
    'build_adaptive_prompt_section',
    'record_combo_deployment',
]


# ═══════════════════════════════════════════════════════════
#  Market Condition Detection
# ═══════════════════════════════════════════════════════════

class MarketCondition:
    """Snapshot of current market conditions for strategy selection."""

    def __init__(
        self,
        regime: str = 'unknown',          # strong_uptrend / uptrend / sideways / downtrend / capitulation
        volatility: str = 'normal',       # low / normal / high / extreme
        trend_strength: float = 0.0,      # [-1, +1]  negative=downtrend, positive=uptrend
        sentiment_score: float = 0.0,     # [-1, +1]  from intel analysis
        policy_signal: float = 0.0,       # [0, 1]    policy activity level
        risk_signal: float = 0.0,         # [0, 2]    composite risk level
        opportunity_signal: float = 0.0,  # [0, 2]    composite opportunity level
        intel_velocity: float = 0.0,      # items/day  news flow speed
        as_of: str = '',                  # date this condition was measured
    ):
        self.regime = regime
        self.volatility = volatility
        self.trend_strength = trend_strength
        self.sentiment_score = sentiment_score
        self.policy_signal = policy_signal
        self.risk_signal = risk_signal
        self.opportunity_signal = opportunity_signal
        self.intel_velocity = intel_velocity
        self.as_of = as_of or datetime.now().strftime('%Y-%m-%d')

    def to_dict(self) -> dict[str, Any]:
        return {
            'regime': self.regime,
            'volatility': self.volatility,
            'trend_strength': round(self.trend_strength, 3),
            'sentiment_score': round(self.sentiment_score, 3),
            'policy_signal': round(self.policy_signal, 3),
            'risk_signal': round(self.risk_signal, 3),
            'opportunity_signal': round(self.opportunity_signal, 3),
            'intel_velocity': round(self.intel_velocity, 2),
            'as_of': self.as_of,
        }

    @property
    def risk_level(self) -> str:
        """Classify overall risk level."""
        if self.risk_signal >= 1.2 or self.volatility == 'extreme':
            return 'critical'
        if self.risk_signal >= 0.8 or self.volatility == 'high':
            return 'high'
        if self.risk_signal >= 0.4:
            return 'elevated'
        return 'normal'

    @property
    def regime_label(self) -> str:
        """Human-readable regime label in Chinese."""
        _LABELS = {
            'strong_uptrend': '强势上涨',
            'uptrend': '上涨趋势',
            'recovery': '触底回升',
            'sideways': '横盘震荡',
            'ranging': '区间震荡',
            'distribution': '高位派发',
            'downtrend': '下跌趋势',
            'capitulation': '恐慌抛售',
            'unknown': '未知',
        }
        return _LABELS.get(self.regime, self.regime)


def detect_market_condition(
    db: Any,
    quant_signals: dict[str, dict[str, Any]] | None = None,
    as_of: str = '',
) -> MarketCondition:
    """Detect current market conditions from quant signals + intel features.

    Combines:
      1. Technical signals (regime, volatility) from held assets
      2. Intel-derived features (sentiment, policy, risk) from intel_timeline

    Args:
        db:             Database connection.
        quant_signals:  Dict of {symbol: signal_snapshot} from compute_signal_snapshot.
                        If None, will attempt to compute from held assets.
        as_of:          Date string for time-locked analysis.  Empty = now.

    Returns:
        MarketCondition object.
    """
    if not as_of:
        as_of = datetime.now().strftime('%Y-%m-%d')

    # ── Aggregate quant signals across all held assets ──
    regime_votes: dict[str, int] = {}
    vol_votes: dict[str, int] = {}
    composite_scores: list[float] = []

    if quant_signals:
        for _code, snap in quant_signals.items():
            if 'error' in snap:
                continue
            r = snap.get('trend_regime', 'unknown')
            regime_votes[r] = regime_votes.get(r, 0) + 1
            v = snap.get('volatility_regime', 'normal_vol')
            vol_votes[v] = vol_votes.get(v, 0) + 1
            cs = snap.get('composite_score', 0)
            composite_scores.append(cs)

    # Majority-vote regime
    if regime_votes:
        regime = max(regime_votes, key=regime_votes.get)
    else:
        regime = 'unknown'

    # Majority-vote volatility
    if vol_votes:
        vol_raw = max(vol_votes, key=vol_votes.get)
        _VOL_MAP = {
            'low_vol': 'low', 'normal_vol': 'normal',
            'high_vol': 'high', 'extreme_vol': 'extreme',
        }
        volatility = _VOL_MAP.get(vol_raw, 'normal')
    else:
        volatility = 'normal'

    # Trend strength = normalized average composite score
    if composite_scores:
        avg_score = sum(composite_scores) / len(composite_scores)
        trend_strength = max(-1, min(1, avg_score / 50.0))  # normalize to [-1, 1]
    else:
        trend_strength = 0.0

    # ── Intel-derived features ──
    try:
        from lib.trading.intel_timeline import build_regime_intel_features
        intel_features = build_regime_intel_features(db, as_of, lookback_days=14)
    except Exception as e:
        logger.warning('[MetaStrategy] Intel feature extraction failed: %s', e, exc_info=True)
        intel_features = {}

    return MarketCondition(
        regime=regime,
        volatility=volatility,
        trend_strength=trend_strength,
        sentiment_score=intel_features.get('sentiment_score', 0.0),
        policy_signal=intel_features.get('policy_signal', 0.0),
        risk_signal=intel_features.get('risk_signal', 0.0),
        opportunity_signal=intel_features.get('opportunity_signal', 0.0),
        intel_velocity=intel_features.get('intel_velocity', 0.0),
        as_of=as_of,
    )


# ═══════════════════════════════════════════════════════════
#  Adaptive Strategy Selection
# ═══════════════════════════════════════════════════════════

# Strategy suitability matrix: (strategy_type, regime) → base suitability [0, 1]
_SUITABILITY_MATRIX = {
    # risk_control is always suitable
    ('risk_control', 'strong_uptrend'): 0.5,
    ('risk_control', 'uptrend'):        0.6,
    ('risk_control', 'recovery'):       0.7,
    ('risk_control', 'sideways'):       0.8,
    ('risk_control', 'ranging'):        0.8,
    ('risk_control', 'distribution'):   0.9,
    ('risk_control', 'downtrend'):      1.0,
    ('risk_control', 'capitulation'):   1.0,

    # buy_signal: best in recovery and early uptrend
    ('buy_signal', 'strong_uptrend'):   0.3,  # too late to buy
    ('buy_signal', 'uptrend'):          0.7,
    ('buy_signal', 'recovery'):         0.9,
    ('buy_signal', 'sideways'):         0.5,
    ('buy_signal', 'ranging'):          0.5,
    ('buy_signal', 'distribution'):     0.2,
    ('buy_signal', 'downtrend'):        0.4,  # dip buying
    ('buy_signal', 'capitulation'):     0.6,  # crisis buying

    # sell_signal: best in distribution and downtrend
    ('sell_signal', 'strong_uptrend'):  0.3,
    ('sell_signal', 'uptrend'):         0.4,
    ('sell_signal', 'recovery'):        0.2,
    ('sell_signal', 'sideways'):        0.5,
    ('sell_signal', 'ranging'):         0.5,
    ('sell_signal', 'distribution'):    0.9,
    ('sell_signal', 'downtrend'):       0.8,
    ('sell_signal', 'capitulation'):    0.6,

    # allocation: best in stable environments
    ('allocation', 'strong_uptrend'):   0.7,
    ('allocation', 'uptrend'):          0.8,
    ('allocation', 'recovery'):         0.7,
    ('allocation', 'sideways'):         0.9,
    ('allocation', 'ranging'):          0.8,
    ('allocation', 'distribution'):     0.6,
    ('allocation', 'downtrend'):        0.5,
    ('allocation', 'capitulation'):     0.3,

    # timing: best in volatile / sideways markets
    ('timing', 'strong_uptrend'):       0.4,
    ('timing', 'uptrend'):              0.5,
    ('timing', 'recovery'):             0.6,
    ('timing', 'sideways'):             0.9,
    ('timing', 'ranging'):              0.9,
    ('timing', 'distribution'):         0.7,
    ('timing', 'downtrend'):            0.5,
    ('timing', 'capitulation'):         0.3,

    # observation: always useful (provides info, not actions)
    ('observation', 'strong_uptrend'):  0.6,
    ('observation', 'uptrend'):         0.6,
    ('observation', 'recovery'):        0.7,
    ('observation', 'sideways'):        0.7,
    ('observation', 'ranging'):         0.7,
    ('observation', 'distribution'):    0.8,
    ('observation', 'downtrend'):       0.8,
    ('observation', 'capitulation'):    0.9,

    # autopilot: LLM-generated strategies — neutral suitability
    ('autopilot', 'strong_uptrend'):    0.5,
    ('autopilot', 'uptrend'):           0.5,
    ('autopilot', 'recovery'):          0.5,
    ('autopilot', 'sideways'):          0.5,
    ('autopilot', 'ranging'):           0.5,
    ('autopilot', 'distribution'):      0.5,
    ('autopilot', 'downtrend'):         0.5,
    ('autopilot', 'capitulation'):      0.5,
}


def select_strategies(
    db: Any,
    condition: MarketCondition,
    max_strategies: int = 8,
) -> list[dict[str, Any]]:
    """Select optimal strategy combination for current market conditions.

    Algorithm:
      1. Score each active strategy by: base suitability × intel modifiers × historical performance
      2. Apply incompatibility penalties from strategy_learner
      3. Select top-K strategies ensuring type diversity
      4. Return ordered list with selection reasoning

    Args:
        db:              Database connection.
        condition:       Current MarketCondition snapshot.
        max_strategies:  Max number of strategies to activate.

    Returns:
        List of strategy dicts, each with added fields:
          selection_score, selection_reason, suitability_score
    """
    # Load all active strategies
    strategies = db.execute(
        "SELECT * FROM trading_strategies WHERE status='active' ORDER BY updated_at DESC"
    ).fetchall()
    strategies = [dict(s) for s in strategies]

    if not strategies:
        logger.warning('[MetaStrategy] No active strategies found')
        return []

    # ── Load strategy learner data (compatibility + failure records) ──
    try:
        from lib.trading_autopilot.strategy_learner import (
            get_strategy_compatibility_scores,
            get_strategy_failure_summary,
        )
        compatibility = get_strategy_compatibility_scores(db)
        failures = get_strategy_failure_summary(db)
    except Exception as e:
        logger.warning('[MetaStrategy] Strategy learner data unavailable: %s', e)
        compatibility = {}
        failures = {}

    # ── Load historical performance ──
    perf_cache: dict[int, float] = {}
    try:
        from lib.trading_autopilot.strategy_evolution import evaluate_strategy_history
        for s in strategies:
            perf = evaluate_strategy_history(db, s['id'], lookback_days=90)
            if perf.get('total_decisions', 0) >= 3 and perf.get('win_rate') is not None:
                perf_cache[s['id']] = perf['win_rate'] / 100.0  # normalize to [0, 1]
    except Exception as e:
        logger.warning('[MetaStrategy] Historical performance unavailable: %s', e)

    # ── Score each strategy ──
    scored: list[tuple[float, dict[str, Any], str]] = []

    for s in strategies:
        stype = s.get('type', 'observation')
        regime = condition.regime

        # Base suitability from matrix
        base = _SUITABILITY_MATRIX.get((stype, regime), 0.5)
        # Fallback for unknown regime
        if regime not in ('strong_uptrend', 'uptrend', 'recovery', 'sideways',
                          'ranging', 'distribution', 'downtrend', 'capitulation'):
            base = _SUITABILITY_MATRIX.get((stype, 'sideways'), 0.5)

        reasons = [f"基础适配={base:.1%}({stype}@{condition.regime_label})"]

        # ── Intel modifiers ──
        modifier = 1.0

        # High risk signal → boost risk_control and sell_signal
        if condition.risk_level in ('critical', 'high'):
            if stype == 'risk_control':
                modifier *= 1.3
                reasons.append("风险信号增强+30%")
            elif stype == 'buy_signal':
                modifier *= 0.7
                reasons.append("高风险时买入信号降权-30%")

        # Positive sentiment → boost buy signals
        if condition.sentiment_score > 0.3:
            if stype == 'buy_signal':
                modifier *= 1.15
                reasons.append("正面情绪+15%")
        elif condition.sentiment_score < -0.3:
            if stype == 'sell_signal':
                modifier *= 1.15
                reasons.append("负面情绪卖出+15%")

        # Policy activity → boost allocation strategies (rebalancing needed)
        if condition.policy_signal > 0.3:
            if stype == 'allocation':
                modifier *= 1.2
                reasons.append("政策活跃配置+20%")

        # ── Historical performance modifier ──
        if s['id'] in perf_cache:
            wr = perf_cache[s['id']]
            # Win rate > 60% → bonus; < 40% → penalty
            if wr >= 0.6:
                modifier *= 1.0 + (wr - 0.6) * 0.5
                reasons.append(f"历史胜率{wr:.0%}加成")
            elif wr < 0.4:
                modifier *= 0.6 + wr  # range [0.6, 1.0]
                reasons.append(f"历史胜率{wr:.0%}降权")

        # ── Failure penalty ──
        sid = str(s['id'])
        if sid in failures:
            failure_count = failures[sid].get('total_failures', 0)
            if failure_count >= 5:
                modifier *= 0.5
                reasons.append(f"多次失败({failure_count}次)严重降权")
            elif failure_count >= 3:
                modifier *= 0.7
                reasons.append(f"失败{failure_count}次降权")

        final_score = base * modifier
        scored.append((final_score, s, '; '.join(reasons)))

    # ── Sort by score and select with type diversity ──
    scored.sort(key=lambda x: x[0], reverse=True)

    selected: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    MAX_PER_TYPE = 3  # don't over-concentrate in one type

    for score, s, reason in scored:
        if len(selected) >= max_strategies:
            break

        stype = s.get('type', 'observation')
        if type_counts.get(stype, 0) >= MAX_PER_TYPE:
            continue

        # ── Compatibility check against already-selected ──
        combo_penalty = 0.0
        for already in selected:
            pair_key = _combo_key(s['id'], already['id'])
            if pair_key in compatibility:
                compat_score = compatibility[pair_key]
                if compat_score < -0.3:
                    combo_penalty += abs(compat_score) * 0.2
                    reason += f"; ⚠️与{already['name']}不兼容(penalty={compat_score:.2f})"

        final_adjusted = max(0.05, score - combo_penalty)

        s_enriched = {
            **s,
            'selection_score': round(final_adjusted, 3),
            'suitability_score': round(score, 3),
            'selection_reason': reason,
        }
        selected.append(s_enriched)
        type_counts[stype] = type_counts.get(stype, 0) + 1

    logger.info(
        '[MetaStrategy] Selected %d/%d strategies for regime=%s, risk=%s',
        len(selected), len(strategies), condition.regime, condition.risk_level,
    )

    return selected


def _combo_key(id_a: int, id_b: int) -> str:
    """Canonical key for a strategy pair (order-independent)."""
    return f"{min(id_a, id_b)}_{max(id_a, id_b)}"


# ═══════════════════════════════════════════════════════════
#  Deployment Recording
# ═══════════════════════════════════════════════════════════

def record_combo_deployment(
    db: Any,
    cycle_id: str,
    condition: MarketCondition,
    selected_strategies: list[dict[str, Any]],
) -> None:
    """Record which strategy combo was deployed for a given market condition.

    This feeds the strategy_learner module with deployment data so it can
    later correlate with outcomes.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    strategy_ids = [s['id'] for s in selected_strategies]
    strategy_names = [s['name'] for s in selected_strategies]

    try:
        db.execute(
            '''INSERT INTO trading_strategy_deployments
               (cycle_id, market_condition_json, strategy_ids_json,
                strategy_names_json, deployed_at)
               VALUES (?, ?, ?, ?, ?)''',
            (
                cycle_id,
                json.dumps(condition.to_dict(), ensure_ascii=False),
                json.dumps(strategy_ids),
                json.dumps(strategy_names, ensure_ascii=False),
                now,
            )
        )
        db.commit()
        logger.debug('[MetaStrategy] Recorded deployment: cycle=%s, %d strategies',
                     cycle_id, len(strategy_ids))
    except Exception as e:
        logger.error('[MetaStrategy] Failed to record deployment: %s', e, exc_info=True)


# ═══════════════════════════════════════════════════════════
#  Prompt Section Builder
# ═══════════════════════════════════════════════════════════

def build_adaptive_prompt_section(
    condition: MarketCondition,
    selected_strategies: list[dict[str, Any]],
) -> str:
    """Build the adaptive strategy section for the autopilot mega-prompt.

    Replaces the static strategy listing with a context-aware selection
    that tells the LLM:
      - WHY these strategies were chosen
      - WHAT the current market condition is
      - HOW confident we are in each strategy for this regime
    """
    lines = [
        "═══════════════════════════════════════",
        "## 第二部分: 自适应策略选择 (Adaptive Meta-Strategy)",
        "═══════════════════════════════════════",
        "",
        f"### 📊 当前市场状态 (截至 {condition.as_of})",
        f"  市场体制: {condition.regime_label} ({condition.regime})",
        f"  波动率: {condition.volatility}",
        f"  趋势强度: {condition.trend_strength:+.2f} (-1=强跌, +1=强涨)",
        f"  情绪得分: {condition.sentiment_score:+.2f} (-1=恐慌, +1=贪婪)",
        f"  风险等级: {condition.risk_level}",
        f"  政策活跃度: {condition.policy_signal:.2f}",
        f"  情报流速: {condition.intel_velocity:.1f} 条/天",
        "",
        f"### 🎯 自动选择的策略组合 ({len(selected_strategies)}个)",
        "_以下策略由Meta-Strategy引擎根据当前市场状态动态选中，_",
        "_每个策略的适配分数反映其在当前体制下的预期有效性。_",
        "",
    ]

    for i, s in enumerate(selected_strategies, 1):
        score = s.get('selection_score', 0)
        bar = '█' * int(score * 10) + '░' * (10 - int(score * 10))
        lines.append(
            f"  {i}. [{s.get('type', '?')}] **{s['name']}** "
            f"[{bar}] {score:.0%}"
        )
        lines.append(f"     逻辑: {s.get('logic', 'N/A')[:200]}")
        lines.append(f"     选择理由: {s.get('selection_reason', 'N/A')}")
        lines.append("")

    lines.extend([
        "### ⚠️ 策略使用指引",
        "1. 你必须优先执行适配分数>70%的策略所指示的操作",
        "2. 适配分数<40%的策略仅作参考，不应作为主要决策依据",
        "3. 如果你认为Meta-Strategy的选择有误，请在strategy_updates中说明理由",
        "4. 当市场状态不明确(unknown)时，采用最保守的策略组合",
    ])

    return '\n'.join(lines)
