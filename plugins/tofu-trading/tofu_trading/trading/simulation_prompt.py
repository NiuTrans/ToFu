"""Render time-locked simulation model context without I/O or execution.

Entry: build_decision_prompt. The coordinator supplies current state and context;
FeeBook and symbol presentation callbacks remain the formatting authorities.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .fee_book import FeeBook
    from .llm_simulator import SimulatorConfig
    from .simulation_ledger import SimulationLedger


SIMULATION_SYSTEM_PROMPT = (
    "你是一位专业的投资AI，精通A股个股、ETF和基金交易。你拥有一套策略工具箱，必须从中选择策略来指导决策，并在回答中标注使用了哪些策略。\n"
    "\n"
    "个股分析要点: 估值(PE/PB)、行业竞争格局、护城河、成长性、股息率、市值规模。\n"
    "交易费用以本次模拟的「交易成本」一节为准（那是账本实扣的数字），不要凭记忆估算费率。\n"
    "积极发现和交易个股，不要只关注ETF/基金。用中文回答。基于历史数据做决策，不要引用未来数据。"
)


def build_decision_prompt(
    *,
    config: SimulatorConfig,
    ledger: SimulationLedger,
    sim_date: str,
    step_index: int,
    total_steps: int,
    portfolio_value: float,
    return_pct: float,
    win_rate: float,
    holdings_text: str,
    strategy_toolbox_prompt: str,
    signal_ctx: str,
    market_ctx: str,
    intel_ctx: str,
    intel_count: int,
    journal_ctx: str,
    tracked_symbols: list[str],
    format_symbols: Callable[[list[str]], str],
    describe_fees: Callable[[FeeBook, list[str], float], str],
) -> str:
    """Use the live ledger/configuration without changing model request policy."""
    return f"""你正在进行历史模拟交易，可交易A股个股、ETF、基金。
⚠️ 当前模拟日期: {sim_date}（你只能看到此日期及之前的数据）

📊 **个股分析框架** — 当你考虑买入个股时，必须评估:
- 估值安全边际: PE/PB是否在行业合理区间
- 行业地位: 行业龙头还是跟随者，护城河强度
- 成长性: 营收/利润增速趋势
- 市值规模: 大盘蓝筹更稳定，中小盘波动大但潜力高
- 股息回报: 高股息股在震荡市提供安全垫

{strategy_toolbox_prompt}

## 账户状态
- 初始资金: ¥{config.initial_capital:,.2f}
- 当前现金: ¥{ledger.cash:,.2f}
- 持仓市值: ¥{portfolio_value - ledger.cash:,.2f}
- 组合总值: ¥{portfolio_value:,.2f}
- 累计收益: {return_pct:+.2f}%
- 历史胜率: {win_rate:.1f}% ({ledger.winning_trades}/{ledger.total_trades})
- 已完成步数: {step_index + 1}/{total_steps}

## 当前持仓
{holdings_text}

## 可交易标的
{("**用户关注的标的（已有完整数据和信号）：**" + chr(10) + format_symbols(config.symbols) + chr(10) + chr(10)) if config.symbols else ""}**你可以自由交易 A 股市场的任意股票、ETF、基金。** 使用 6 位证券代码下单，系统会自动获取价格数据。
{("- 用户关注的标的已有完整量化信号，可优先参考" + chr(10)) if config.symbols else ""}- 积极发现和交易A股个股，不要只依赖ETF和基金
- 可以基于市场分析自主发现和交易任何标的
- 确保使用真实存在的 6 位证券代码（如 600519 贵州茅台、510300 沪深300ETF、300750 宁德时代）
- 股票代码: 60xxxx/68xxxx(沪)、000xxx/002xxx/300xxx(深)
- 不要编造不存在的代码

## 量化信号（截至{sim_date}）
{signal_ctx if signal_ctx else "（数据不足）"}

{market_ctx if market_ctx else ""}

## 市场情报（时间锁定至{sim_date}）
{intel_ctx[:4000] if intel_ctx else "（无情报数据）"}
（共{intel_count}条情报）

{journal_ctx}

## 交易规则
- 单笔不超过总资金的{config.max_position_pct}%
- 最多持有{config.max_positions}个标的

### 交易成本（账本实扣，与下单同一来源）
{describe_fees(config.fee_book, tracked_symbols or config.symbols, min(portfolio_value * config.max_position_pct / 100, max(ledger.cash, 1000)))}
- {"T+1交易（买入次日才能卖出）" if config.t_plus_1 else "无T+1限制"}
- 信心度 < {config.min_confidence} 的决策不执行

## 你的任务
1. 分析当前市场环境和持仓状态
2. 从策略工具箱中选择你认为当前最适合的策略组合
3. 基于所选策略做出买卖决策
4. 输出决策和你使用的策略

## 输出格式（必须严格遵守）

**重要：你必须首先输出 <decisions> 和 <strategies_used> 结构化块，然后再输出分析文本。**
即使分析内容很长，也必须确保 <decisions> 和 <strategies_used> 块完整输出在最前面。

<decisions>
[
  {{
    "action": "buy|sell|hold",
    "symbol": "标的代码",
    "amount": 买入金额（买入时填写）,
    "confidence": 0-100,
    "reason": "决策理由（需引用你选用的策略逻辑）"
  }}
]
</decisions>

<strategies_used>
["策略名称1", "策略名称2", ...]
</strategies_used>

然后输出你的详细分析和理由。

如果决定观望，输出 <decisions>[]</decisions> 和 <strategies_used>["策略名称"]</strategies_used> 并说明理由。"""
