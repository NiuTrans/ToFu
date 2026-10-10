"""lib/trading_autopilot/reasoning.py — Autonomous Reasoning Chain.

Builds the mega-prompt for the autonomous analyst and parses
the structured result from LLM output.
"""

import json
import re

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'build_autopilot_prompt',
    'parse_autopilot_result',
]


def build_autopilot_prompt(
    holdings_ctx, cash, strategies_ctx, intel_ctx,
    correlation_ctx, evolution_ctx, kpi_evaluations,
    cycle_number=1, debate_ctx=None, learning_ctx=None,
):
    """Build the mega-prompt for the autonomous analyst.

    This prompt instructs the LLM to:
      1. Analyze all intelligence with reasoning
      2. Cross-reference correlations
      3. Consider strategy evolution lessons
      4. Factor in KPI evaluations + quantitative signals
      5. Produce actionable position recommendations with risk sizing
    """
    kpi_text = ""
    if kpi_evaluations:
        kpi_lines = ["## 持仓标的KPI + 量化信号评估"]
        for code, eval_data in kpi_evaluations.items():
            if 'error' in eval_data:
                kpi_lines.append(f"\n### {code}: ⚠️ {eval_data['error']}")
                continue
            k = eval_data['kpis']
            kpi_lines.append(f"\n### {code} {eval_data.get('asset_name', '')}")
            kpi_lines.append(f"  评估区间: {eval_data.get('period', 'N/A')}")
            kpi_lines.append(f"  综合推荐分: {eval_data['recommendation_score']}/100")
            kpi_lines.append(f"  总收益: {k['total_return']}% | 年化: {k['annual_return']}%")
            kpi_lines.append(f"  最大回撤: {k['max_drawdown']}% | 波动率: {k['volatility']}%")
            kpi_lines.append(f"  夏普比率: {k['sharpe_ratio']} | 索提诺: {k['sortino_ratio']} | 卡玛: {k['calmar_ratio']}")
            kpi_lines.append(f"  胜率: {k['win_days_pct']}% | 最佳日: +{k['best_day']}% | 最差日: {k['worst_day']}%")
            kpi_lines.append(f"  VaR(95%): {k['var_95']}%")

            # Add quantitative signals if available
            qs = eval_data.get('quant_signals', {})
            if qs and 'error' not in qs:
                comp = qs.get('composite', {})
                regime = qs.get('regime', {})
                ma = qs.get('moving_averages', {})
                rsi = qs.get('rsi', {})
                macd = qs.get('macd', {})
                bb = qs.get('bollinger', {})

                kpi_lines.append("  ── 量化技术信号 ──")
                if comp:
                    kpi_lines.append(f"  综合信号: {comp.get('signal', 'N/A')} (得分: {comp.get('score', 'N/A')}/100, 强度: {comp.get('strength', 'N/A')})")
                if regime:
                    kpi_lines.append(f"  市场体制: {regime.get('regime', 'N/A')} (置信度: {regime.get('confidence', 'N/A')}%)")
                if ma:
                    kpi_lines.append(f"  均线: 短期趋势={ma.get('short_trend', 'N/A')}, 长期趋势={ma.get('long_trend', 'N/A')}, 金叉/死叉={ma.get('crossovers', [])}")
                if rsi:
                    kpi_lines.append(f"  RSI: {rsi.get('value', 'N/A')} ({rsi.get('signal', 'N/A')})")
                if macd:
                    kpi_lines.append(f"  MACD: {macd.get('signal', 'N/A')} (柱状={macd.get('histogram_trend', 'N/A')})")
                if bb:
                    kpi_lines.append(f"  布林带: 位置={bb.get('position', 'N/A')}%, 带宽={bb.get('bandwidth', 'N/A')}% ({bb.get('signal', 'N/A')})")
        kpi_text = "\n".join(kpi_lines)

    return f"""你是一位全球顶级投资超级分析师 (Autonomous Fund Super-Analyst), 第 {cycle_number} 轮分析周期。

你的核心能力:
1. 🔍 深度情报分析 — 不仅分析每条情报，还要思考情报之间的关联和传导链
2. 🧠 策略进化 — 从每次成功和失败的决策中学习，不断优化策略
3. 📊 量化评估 — 基于KPI指标 + 技术信号对标的进行客观评分
4. 🎯 自主决策 — 综合所有信息，给出有信心评分的持仓建议
5. 📉 量化信号驱动 — 你现在有真实的技术指标（RSI/MACD/均线/布林带/市场体制识别），请务必将这些客观信号作为决策的主要依据之一，而非纯粹依赖文字推理

⚠️ 关键决策纪律:
  - 当量化信号与情报分析矛盾时，优先相信量化信号（情报可能已被市场消化）
  - 市场体制(regime)处于"capitulation"或"downtrend"时，严禁大幅加仓
  - RSI>75时不追高，RSI<25时关注抄底机会
  - 综合信号得分<-30时启动防御模式，减仓至安全水位
  - 每笔建议必须包含止损位和目标收益率

═══════════════════════════════════════
## 第一部分: 全球情报 (Global Intelligence)
═══════════════════════════════════════
{intel_ctx if intel_ctx else "(暂无情报数据)"}

═══════════════════════════════════════
{correlation_ctx if correlation_ctx else "## 情报关联分析: (数据不足，暂无关联分析)"}

═══════════════════════════════════════
## 第二部分: 策略库 (Strategy Repository)
═══════════════════════════════════════
{strategies_ctx if strategies_ctx else "(暂无策略)"}

{evolution_ctx if evolution_ctx else ""}

═══════════════════════════════════════
## 第三部分: 量化评估 (KPI Evaluation)
═══════════════════════════════════════
{kpi_text if kpi_text else "(暂无KPI评估数据)"}

═══════════════════════════════════════
## 第四部分: 当前持仓与资金
═══════════════════════════════════════
{holdings_ctx if holdings_ctx else "用户暂无持仓。"}

可支配资金: ¥{cash:,.2f}

{debate_ctx if debate_ctx else ""}

{learning_ctx if learning_ctx else ""}

═══════════════════════════════════════

请按以下结构输出你的完整分析:

### 🔍 A. 情报深度解读 (Intelligence Deep Dive)
对每条关键情报进行思考，分析其对市场/标的的影响方向、影响程度、影响时间。
重点分析: 不同情报之间如何相互强化或相互抵消。

### 🔗 B. 关联推理链 (Correlation Reasoning Chain)
画出你的推理链: 事件A → 影响X → 导致Y → 因此建议Z
每个推理步骤都要有证据支持。

### ⚖️ B2. 多空辩论评判 (Bull vs Bear Verdict)
（如果上方包含多空辩论，必须在此输出你的裁决）
- 逐一评判看多和看空的核心论据，哪些有效、哪些有瑕疵
- 给出你的多空倾向比 (如 60:40 偏多)
- 标出双方最有价值的 1 个论据和最弱的 1 个论据
- 如果分歧过大，降低你的整体 confidence_score

### 📊 C. 策略评估与进化建议 (Strategy Evolution)
评估现有策略在当前市场环境下的有效性。
基于历史胜率和教训，建议策略调整。
如果有表现不佳的策略，提出改进方案。

### 🎯 D. 持仓建议 (Position Recommendations)
对每只持仓标的和推荐的新标的:
- 操作建议 (买入/加仓/减仓/卖出/观望)
- 建议金额或比例
- 信心评分 (0-100)
- 核心理由 (2-3句话)

### 📈 E. 风险评估矩阵 (Risk Matrix)
列出当前主要风险因子及其概率和影响程度。

### 🧬 F. 新策略提炼 (New Strategy Extraction)
从本次分析中提炼1-3条新的可执行策略。

请在 <autopilot_result> 标签中输出 JSON:
<autopilot_result>
{{
  "confidence_score": 0-100,
  "market_outlook": "bullish|bearish|neutral|cautious",
  "debate_verdict": {{
    "bull_bear_ratio": "多空倾向比，如 60:40",
    "bull_best_point": "看多方最有力的论据",
    "bear_best_point": "看空方最有力的论据",
    "key_disagreement": "核心分歧点",
    "your_judgment": "你的最终判断及理由"
  }},
  "reasoning_chain": [
    {{"step": 1, "from": "情报/事件", "to": "影响/结论", "evidence": "支持证据", "confidence": 0-100}}
  ],
  "position_recommendations": [
    {{
      "symbol": "标的代码",
      "asset_name": "标的名称",
      "action": "buy|sell|hold|add|reduce",
      "amount": 金额,
      "target_position_pct": "目标仓位百分比 0-100",
      "stop_loss_pct": "止损线 (从买入价下跌X%时止损)",
      "take_profit_pct": "止盈线 (从买入价上涨X%时止盈)",
      "time_horizon": "short(1-2周)|medium(1-3月)|long(3月以上)",
      "confidence": 0-100,
      "quant_signal_alignment": "信号是否支持该操作: aligned|neutral|contrary",
      "reason": "核心理由 (必须引用至少一个量化指标)"
    }}
  ],
  "risk_factors": [
    {{"factor": "风险因子", "probability": "high|medium|low", "impact": "high|medium|low", "mitigation": "应对措施"}}
  ],
  "strategy_updates": [
    {{"action": "new|update|retire", "name": "策略名称", "logic": "策略逻辑", "reason": "为什么"}}
  ],
  "next_review": "下次建议分析时间 (YYYY-MM-DD HH:MM)"
}}
</autopilot_result>"""


def parse_autopilot_result(content):
    """Extract structured data from autopilot output.

    Supports two formats:
      1. <autopilot_result>{...}</autopilot_result> tag
      2. Markdown-style with ### sections containing ```json blocks
    """
    # Method 1: Try <autopilot_result> tags
    match = re.search(r'<autopilot_result>\s*(.*?)\s*</autopilot_result>', content, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            raw = match.group(1)
            raw = re.sub(r',\s*}', '}', raw)
            raw = re.sub(r',\s*]', ']', raw)
            try:
                return json.loads(raw)
            except Exception as e:
                logger.warning('[Autopilot] JSON repair failed for <autopilot_result> tag: %s — falling back to markdown parsing', e, exc_info=True)

    # Method 2: Parse markdown sections
    result = {
        'recommendations': [],
        'strategy_updates': [],
        'market_outlook': '',
        'confidence_score': 0,
    }

    # Extract recommendations JSON
    reco_match = re.search(
        r'(?:recommendations_json|recommendations|持仓建议)\s*\n*```(?:json)?\s*\n(.*?)\n```',
        content, re.DOTALL | re.IGNORECASE
    )
    if reco_match:
        try:
            result['recommendations'] = json.loads(reco_match.group(1))
        except Exception as e:
            logger.warning('[Autopilot] recommendations JSON parse failed from markdown block: %s', e, exc_info=True)

    # Extract strategy updates JSON
    strat_match = re.search(
        r'(?:strategy_updates_json|strategy_updates|策略更新)\s*\n*```(?:json)?\s*\n(.*?)\n```',
        content, re.DOTALL | re.IGNORECASE
    )
    if strat_match:
        try:
            result['strategy_updates'] = json.loads(strat_match.group(1))
        except Exception as e:
            logger.warning('[Autopilot] strategy_updates JSON parse failed from markdown block: %s', e, exc_info=True)

    # Extract market outlook
    outlook_match = re.search(
        r'(?:market_outlook|市场展望|市场观点|大盘研判)\s*[:\n:]+\s*(.*?)(?:\n###|\n\n|$)',
        content, re.IGNORECASE
    )
    if outlook_match:
        outlook_raw = outlook_match.group(1).strip().lower()
        result['market_outlook'] = outlook_raw
    # Also try to extract outlook from inline keywords if not found
    if not result['market_outlook']:
        for keyword, label in [
            ('极度看多', 'very_bullish'), ('强烈看多', 'very_bullish'),
            ('看多', 'bullish'), ('偏多', 'bullish'),
            ('谨慎乐观', 'cautious'), ('谨慎', 'cautious'), ('中性偏多', 'cautious'),
            ('中性', 'neutral'), ('震荡', 'neutral'),
            ('偏空', 'bearish'), ('看空', 'bearish'),
            ('极度看空', 'very_bearish'),
        ]:
            if keyword in content:
                result['market_outlook'] = label
                break

    # Extract confidence score — try multiple patterns
    conf_match = re.search(
        r'(?:confidence_score|置信度|confidence|信心指数|综合信心)\s*[:\n:]+\s*(\d+)',
        content, re.IGNORECASE
    )
    if conf_match:
        result['confidence_score'] = int(conf_match.group(1))

    # Return None only if we got absolutely nothing
    if not any([result['recommendations'], result['strategy_updates'],
                result['market_outlook'], result['confidence_score']]):
        return None

    return result
