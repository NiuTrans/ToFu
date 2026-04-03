"""lib/trading_autopilot/debate.py — Bull vs Bear Debate Engine.

Inspired by TradingAgents (UCLA/MIT): before making any investment
decision, two specialized analyst agents debate the evidence.

  🐂 Bull Researcher — finds all bullish signals, optimistic
     interpretations, upside catalysts.
  🐻 Bear Researcher — finds all risks, hidden dangers, historical
     cautionary parallels.

Both run in parallel (via ``smart_chat_batch``), and their outputs
are injected into the final mega-prompt so the Super-Analyst must
explicitly weigh both perspectives.  This dialectical approach
reduces confirmation bias and surfaces blind spots.
"""

from __future__ import annotations

from typing import Any, Optional

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'run_bull_bear_debate',
    'build_debate_context',
]


# ═══════════════════════════════════════════════════════════
#  Prompt Templates
# ═══════════════════════════════════════════════════════════

_BULL_SYSTEM = (
    "你是一位资深的看多投资分析师 (Bull Researcher)。\n"
    "你的职责是从所有可用数据中寻找看多的证据和机会。\n"
    "你必须客观引用具体数据（RSI值、均线位置、资金流向、政策信号等），\n"
    "但你的分析立场是寻找上行机会。即使是中性信号，也要分析其潜在的乐观面。\n"
    "你不需要给出最终投资建议，只需要呈现所有看多论据。"
)

_BEAR_SYSTEM = (
    "你是一位资深的风险分析师 (Bear Researcher)。\n"
    "你的职责是从所有可用数据中寻找风险因子和看空信号。\n"
    "你必须客观引用具体数据（RSI值、最大回撤、波动率、政策风险等），\n"
    "特别关注：隐藏的关联风险、历史相似情境的不良结局、过度乐观的陷阱、\n"
    "市场情绪过热信号。你不需要给出最终投资建议，只需要呈现所有风险论据。"
)


def _build_kpi_summary(kpi_evaluations: dict[str, Any]) -> str:
    """Build a compact KPI summary for debate prompts (shorter than mega-prompt version)."""
    if not kpi_evaluations:
        return "(暂无KPI数据)"

    lines = []
    for code, eval_data in kpi_evaluations.items():
        if 'error' in eval_data:
            continue
        k = eval_data['kpis']
        name = eval_data.get('asset_name', '')
        line = (
            f"  {code} {name}: "
            f"总收益{k['total_return']}%, 年化{k['annual_return']}%, "
            f"最大回撤{k['max_drawdown']}%, 波动率{k['volatility']}%, "
            f"夏普{k['sharpe_ratio']}, 胜率{k['win_days_pct']}%"
        )

        # Append quant signals if available
        qs = eval_data.get('quant_signals', {})
        if qs and 'error' not in qs:
            comp = qs.get('composite', {})
            regime = qs.get('regime', {})
            rsi = qs.get('rsi', {})
            macd = qs.get('macd', {})
            parts = []
            if comp:
                parts.append(f"综合信号={comp.get('signal', 'N/A')}({comp.get('score', '?')}/100)")
            if regime:
                parts.append(f"体制={regime.get('regime', 'N/A')}")
            if rsi:
                parts.append(f"RSI={rsi.get('value', 'N/A')}")
            if macd:
                parts.append(f"MACD={macd.get('signal', 'N/A')}")
            if parts:
                line += " | " + ", ".join(parts)

        lines.append(line)

    return "\n".join(lines) if lines else "(暂无KPI数据)"


def _build_bull_prompt(
    intel_ctx: str,
    kpi_summary: str,
    holdings_ctx: str,
    correlation_ctx: str,
    cash: float,
) -> str:
    return f"""请基于以下数据，从看多角度进行深度分析。

## 市场情报
{intel_ctx if intel_ctx else "(暂无情报)"}

{correlation_ctx if correlation_ctx else ""}

## 持仓标的量化指标
{kpi_summary}

## 当前持仓
{holdings_ctx if holdings_ctx else "暂无持仓"}
可支配资金: ¥{cash:,.2f}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
请输出以下结构化分析（用中文）:

### 🐂 看多论据 (至少5个，按强度排序)
对每个论据：引用具体数据 + 分析逻辑 + 潜在催化剂时间窗口

### 📈 机会识别
- 哪些标的/板块有被低估的可能？
- 哪些量化信号暗示上行空间？
- 情报中有哪些积极信号尚未被市场充分定价？

### ⚡ 催化剂时间表
列出可能触发上涨的关键事件及其预期时间

### 🛡️ 对潜在看空观点的反驳
预判可能的看空论点，并给出你的反驳理由"""


def _build_bear_prompt(
    intel_ctx: str,
    kpi_summary: str,
    holdings_ctx: str,
    correlation_ctx: str,
    cash: float,
) -> str:
    return f"""请基于以下数据，从风控角度进行深度风险分析。

## 市场情报
{intel_ctx if intel_ctx else "(暂无情报)"}

{correlation_ctx if correlation_ctx else ""}

## 持仓标的量化指标
{kpi_summary}

## 当前持仓
{holdings_ctx if holdings_ctx else "暂无持仓"}
可支配资金: ¥{cash:,.2f}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
请输出以下结构化分析（用中文）:

### 🐻 看空/风险论据 (至少5个，按威胁程度排序)
对每个论据：引用具体数据 + 风险传导链 + 潜在爆发时间

### ⚠️ 隐藏风险识别
- 哪些当前「正常」的指标其实已处于危险区间？
- 情报之间有哪些看似无关但实际上会共振放大的风险？
- 历史上类似情境（相近的RSI/体制/政策组合）最终走向如何？

### 💣 黑天鹅情景推演
列出2-3个小概率但高影响的极端情景

### 🔍 对潜在看多观点的质疑
预判可能的看多论点，指出其中的逻辑漏洞或忽视的风险"""


# ═══════════════════════════════════════════════════════════
#  Debate Runner
# ═══════════════════════════════════════════════════════════

def run_bull_bear_debate(
    ctx: dict[str, Any],
    *,
    llm=None,
    max_tokens: int = 4096,
    temperature: float = 0.4,
) -> tuple[str, str, str]:
    """Run a parallel Bull vs Bear debate on the gathered context.

    Both analysts run concurrently via ``smart_chat_batch`` to minimize
    wall-clock time.  Uses ``capability='text'`` (cheaper/faster model)
    since the debate is focused argumentation, not final synthesis.

    Args:
        ctx: The context dict returned by ``_gather_context()``.
        llm: Optional LLM service for testing.  ``None`` uses dispatch.
        max_tokens: Max tokens per debate agent response.
        temperature: Higher = more creative arguments (0.4 recommended).

    Returns:
        (bull_content, bear_content, debate_ctx)
        where ``debate_ctx`` is the formatted text block for injection
        into the mega-prompt.
    """
    kpi_summary = _build_kpi_summary(ctx.get('kpi_evaluations', {}))

    bull_messages = [
        {'role': 'system', 'content': _BULL_SYSTEM},
        {'role': 'user', 'content': _build_bull_prompt(
            ctx['intel_ctx'], kpi_summary, ctx['holdings_ctx'],
            ctx.get('correlation_ctx', ''), ctx['cash'],
        )},
    ]

    bear_messages = [
        {'role': 'system', 'content': _BEAR_SYSTEM},
        {'role': 'user', 'content': _build_bear_prompt(
            ctx['intel_ctx'], kpi_summary, ctx['holdings_ctx'],
            ctx.get('correlation_ctx', ''), ctx['cash'],
        )},
    ]

    logger.info('[Debate] Starting Bull vs Bear debate (parallel)...')

    if llm is not None:
        # Testing path — sequential calls via injected LLM service
        bull_content, _bull_usage = llm.chat(
            messages=bull_messages, max_tokens=max_tokens,
            temperature=temperature, log_prefix='[Debate-Bull]',
        )
        bear_content, _bear_usage = llm.chat(
            messages=bear_messages, max_tokens=max_tokens,
            temperature=temperature, log_prefix='[Debate-Bear]',
        )
    else:
        # Production path — parallel via smart_chat_batch
        from lib.llm_dispatch import smart_chat_batch
        results = smart_chat_batch(
            [bull_messages, bear_messages],
            max_tokens=max_tokens,
            temperature=temperature,
            capability='text',          # Use faster/cheaper model for debate
            log_prefix='[Debate]',
            max_concurrent=2,
        )
        bull_content, _bull_usage = results[0]
        bear_content, _bear_usage = results[1]

    logger.info(
        '[Debate] Debate complete. Bull: %d chars, Bear: %d chars',
        len(bull_content), len(bear_content),
    )

    debate_ctx = build_debate_context(bull_content, bear_content)
    return bull_content, bear_content, debate_ctx


# ═══════════════════════════════════════════════════════════
#  Context Formatting
# ═══════════════════════════════════════════════════════════

def build_debate_context(bull_content: str, bear_content: str) -> str:
    """Format debate results for injection into the mega-prompt.

    The output is a self-contained section that the Super-Analyst
    can reference.  It includes explicit instructions for how
    the analyst should weigh the debate.
    """
    return f"""═══════════════════════════════════════
## 第五部分: 多空辩论 (Bull vs Bear Debate)
═══════════════════════════════════════
以下是两位专业分析师从对立视角对当前市场和持仓的独立分析。
你必须认真权衡双方论据，不可忽视任何一方的有效论点。

┌─────────────────────────────────────
│ 🐂 看多分析师 (Bull Researcher)
└─────────────────────────────────────
{bull_content}

┌─────────────────────────────────────
│ 🐻 看空分析师 (Bear Researcher)
└─────────────────────────────────────
{bear_content}

┌─────────────────────────────────────
│ ⚖️ 辩论评判指引
└─────────────────────────────────────
请在你的分析中:
1. 明确指出你更认同哪方的哪些论据，并解释为什么
2. 如果双方在某个因子上严重分歧，你必须给出自己的独立判断
3. 当看空方指出的风险论据有数据支撑时，即使你倾向看多，也必须在仓位建议中体现风控
4. 你的 confidence_score 应反映多空分歧程度 — 分歧越大，confidence 应越低"""
