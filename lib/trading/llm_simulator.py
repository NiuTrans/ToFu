"""lib/trading/llm_simulator.py — LLM-Driven Historical Simulation Engine.

Merges livetest's LLM decision pipeline with backtest's time-travel capability.

The core loop:
  for each decision_date in [start, start+step, start+2*step, ...]:
      1. Build time-locked context (prices, intel, signals — all ≤ decision_date)
      2. Call LLM with full context → get buy/sell/hold decisions
      3. Execute virtual trades (T+1, fees, stop-loss/take-profit)
      4. Record everything (journal, reasoning, signals)
      5. Advance to next decision point
  End: compute performance metrics (return, drawdown, Sharpe, etc.)

Key difference from backtest.py:
  - Decisions are made by the LLM, not hard-coded rules.
  - Every decision includes full reasoning chain.

Key difference from livetest.py:
  - Time is simulated, not real.
  - All data is historical and time-locked.
  - Runs to completion in one session (minutes, not months).
"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any, Optional

from lib.log import get_logger, log_context

logger = get_logger(__name__)

__all__ = [
    'SimulatorConfig',
    'run_simulation',
    'get_sim_session',
    'get_sim_positions',
    'get_sim_journal',
    'get_sim_stats',
    'list_sim_sessions',
]

# Asset name cache — pre-populated with common ETFs, dynamically expanded
# when user adds custom stocks/funds via register_sim_asset_name().
_SIM_FUND_NAMES = {
    '510300': '华泰柏瑞沪深300ETF',
    '510500': '南方中证500ETF',
    '159915': '易方达创业板ETF',
    '510050': '华夏上证50ETF',
    '512100': '南方中证1000ETF',
    '512880': '国泰中证全指证券公司ETF',
    '512010': '易方达沪深300医药卫生ETF',
    '515790': '华泰柏瑞中证光伏产业ETF',
    '159825': '天弘中证农业主题ETF',
    '512690': '鹏华中证酒ETF',
    '511010': '国泰上证5年期国债ETF',
    '511260': '国泰上证10年期国债ETF',
    '511220': '海富通上证城投债ETF',
    '159972': '华夏中证海外中国互联网50ETF',
    '513500': '博时标普500ETF',
}


def register_sim_asset_name(code: str, name: str):
    """Register a human-readable name for an asset code at runtime."""
    if code and name:
        _SIM_FUND_NAMES[code] = name


def _format_tradeable_symbols(symbols: list[str]) -> str:
    """Format symbol list with names for LLM prompt."""
    lines = []
    for code in symbols:
        name = _SIM_FUND_NAMES.get(code, code)
        lines.append(f'- {name}（{code}）')
    return '\n'.join(lines)


# ═══════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════
#  Strategy Toolbox — loaded from trading_strategies DB table
#
#  The LLM autonomously picks which strategies to use per decision.
#  After each simulation, per-strategy performance is recorded
#  back to the DB for an evolving feedback loop.
# ═══════════════════════════════════════════════════════════

_STRATEGY_TYPE_LABELS = {
    'buy_signal':   '📈 买入信号',
    'sell_signal':  '📉 卖出信号',
    'risk_control': '🛡️ 风险控制',
    'allocation':   '⚖️ 资产配置',
    'timing':       '⏰ 择时策略',
    'observation':  '👁️ 观察指标',
}


def _load_strategy_toolbox(db) -> tuple[str, list[dict]]:
    """Load all active strategies from DB and format as an LLM prompt section.

    Returns:
        Tuple of (prompt_text, strategy_list_for_tracking).
    """
    try:
        rows = db.execute(
            "SELECT id, name, type, logic, scenario, assets "
            "FROM trading_strategies WHERE status='active' "
            "ORDER BY type, name"
        ).fetchall()
    except Exception as e:
        logger.warning('[Sim] Failed to load strategies from DB: %s', e)
        rows = []

    if not rows:
        return '（策略库为空，请自由发挥）', []

    strategies = [dict(r) for r in rows]

    # Load performance summary per strategy
    perf_map = {}  # strategy_id → {win_rate, avg_return, total}
    try:
        perf_rows = db.execute(
            'SELECT strategy_id, return_pct FROM trading_strategy_performance'
        ).fetchall()
        from collections import defaultdict
        by_sid = defaultdict(list)
        for pr in perf_rows:
            by_sid[pr['strategy_id']].append(pr['return_pct'])
        for sid, returns in by_sid.items():
            wins = sum(1 for r in returns if r and r > 0)
            perf_map[sid] = {
                'win_rate': round(wins / len(returns) * 100, 1) if returns else None,
                'avg_return': round(sum(r for r in returns if r) / len(returns), 2) if returns else None,
                'total': len(returns),
            }
    except Exception as e:
        logger.debug('[Sim] Strategy performance load failed: %s', e)

    # Build prompt
    lines = ['## 🧰 策略工具箱（你必须从中选择并组合使用）']
    lines.append('以下是所有可用策略，每条都有历史胜率。')
    lines.append('你可以自由组合多条策略来做决策。每次操作必须在 <strategies_used> 中标注你用了哪些。')
    lines.append('')

    by_type = {}
    for s in strategies:
        by_type.setdefault(s['type'], []).append(s)

    for stype in ['risk_control', 'buy_signal', 'sell_signal', 'allocation', 'timing', 'observation']:
        items = by_type.get(stype, [])
        if not items:
            continue
        label = _STRATEGY_TYPE_LABELS.get(stype, stype)
        lines.append(f'### {label}')
        for s in items:
            perf = perf_map.get(s['id'])
            perf_tag = ''
            if perf and perf['total'] >= 1:
                wr = perf['win_rate']
                emoji = '🟢' if wr >= 60 else '🟡' if wr >= 40 else '🔴'
                perf_tag = f'  [{emoji} 胜率{wr}% | 平均收益{perf["avg_return"]}% | {perf["total"]}次]'
            lines.append(f'- **{s["name"]}** (ID:{s["id"]}){perf_tag}')
            lines.append(f'  逻辑: {s["logic"][:200]}')
            if s.get('scenario'):
                lines.append(f'  适用: {s["scenario"][:100]}')
        lines.append('')

    return '\n'.join(lines), strategies


def _load_strategy_analytics(db) -> dict[str, Any]:
    """Load strategy analytics for the frontend Strategy Lab display.

    Returns:
        Dict with strategy list, per-strategy performance, and aggregate stats.
    """
    try:
        rows = db.execute(
            "SELECT id, name, type, logic, scenario, status, source, created_at "
            "FROM trading_strategies ORDER BY type, name"
        ).fetchall()
    except Exception as e:
        logger.warning('[Sim] Failed to load strategy analytics: %s', e)
        return {'strategies': [], 'performance': {}, 'aggregate': {}}

    strategies = [dict(r) for r in rows]

    # Per-strategy performance
    perf_map = {}
    try:
        perf_rows = db.execute(
            'SELECT strategy_id, return_pct, source, created_at '
            'FROM trading_strategy_performance ORDER BY created_at DESC'
        ).fetchall()
        from collections import defaultdict
        by_sid = defaultdict(list)
        for pr in perf_rows:
            by_sid[pr['strategy_id']].append({
                'return_pct': pr['return_pct'],
                'source': pr.get('source', ''),
                'date': pr.get('created_at', ''),
            })
        for sid, records in by_sid.items():
            returns = [r['return_pct'] for r in records if r['return_pct'] is not None]
            wins = sum(1 for r in returns if r > 0)
            perf_map[sid] = {
                'win_rate': round(wins / len(returns) * 100, 1) if returns else None,
                'avg_return': round(sum(returns) / len(returns), 2) if returns else None,
                'best_return': round(max(returns), 2) if returns else None,
                'worst_return': round(min(returns), 2) if returns else None,
                'total_uses': len(returns),
                'recent': records[:5],
            }
    except Exception as e:
        logger.debug('[Sim] Strategy perf analytics failed: %s', e)

    # Aggregate stats
    all_returns = []
    for sid_perf in perf_map.values():
        avg = sid_perf.get('avg_return')
        if avg is not None:
            all_returns.append(avg)

    active_count = sum(1 for s in strategies if s.get('status') == 'active')
    with_data = sum(1 for sid, p in perf_map.items() if p['total_uses'] > 0)

    aggregate = {
        'total_strategies': len(strategies),
        'active_count': active_count,
        'with_performance_data': with_data,
        'avg_win_rate': round(
            sum(p['win_rate'] for p in perf_map.values() if p.get('win_rate') is not None)
            / max(with_data, 1), 1
        ) if with_data > 0 else None,
    }

    return {
        'strategies': strategies,
        'performance': perf_map,
        'aggregate': aggregate,
    }


def _record_sim_strategy_performance(db, session_id: str, strategies_used: list[dict],
                                      trade_return_pct: float, trade_info: dict):
    """Record which strategies contributed to a trade outcome.

    Called after each sell (closed trade) to update strategy performance.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for s in strategies_used:
        sid = s.get('id')
        if not sid:
            continue
        try:
            db.execute('''
                INSERT INTO trading_strategy_performance
                (strategy_id, period_start, period_end, return_pct,
                 source, detail_json, created_at)
                VALUES (?, ?, ?, ?, 'sim', ?, ?)
            ''', (
                sid,
                trade_info.get('buy_date', now),
                trade_info.get('sell_date', now),
                trade_return_pct,
                json.dumps({
                    'session_id': session_id,
                    'symbol': trade_info.get('symbol', ''),
                    'strategy_name': s.get('name', ''),
                }, ensure_ascii=False),
                now,
            ))
        except Exception as e:
            logger.debug('[Sim] Strategy perf record failed for sid=%s: %s', sid, e)


class SimulatorConfig:
    """Configuration for LLM simulation."""

    def __init__(self, **kwargs):
        self.initial_capital: float = kwargs.get('initial_capital', 100000)
        self.symbols: list[str] = kwargs.get('symbols', [])
        self.start_date: str = kwargs.get('start_date', '')
        self.end_date: str = kwargs.get('end_date', '')
        self.step_days: int = kwargs.get('step_days', 5)    # Decision frequency
        self.max_position_pct: float = kwargs.get('max_position_pct', 30)
        self.max_positions: int = kwargs.get('max_positions', 5)
        self.stop_loss_pct: float = kwargs.get('stop_loss_pct', 5)
        self.take_profit_pct: float = kwargs.get('take_profit_pct', 15)
        self.buy_fee_rate: float = kwargs.get('buy_fee_rate', 0.0015)   # 0.15%
        self.sell_fee_rate: float = kwargs.get('sell_fee_rate', 0.005)   # 0.5%
        self.min_confidence: int = kwargs.get('min_confidence', 50)
        self.t_plus_1: bool = kwargs.get('t_plus_1', True)   # T+1 trading rule
        self.benchmark_index: str = kwargs.get('benchmark_index', '1.000300')  # 沪深300
        self.strategy: str = kwargs.get('strategy',
                                        kwargs.get('risk_level', 'auto'))  # 'auto' = LLM picks

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith('_')}


# ═══════════════════════════════════════════════════════════
#  Main Simulation Loop
# ═══════════════════════════════════════════════════════════

def run_simulation(
    db: Any,
    config: SimulatorConfig,
    on_event: Callable | None = None,
) -> dict[str, Any]:
    """Run a complete LLM-driven historical simulation.

    This is the main entry point. It:
      1. Creates a session in DB
      2. Iterates through decision dates
      3. At each date: builds context → calls LLM → executes decisions
      4. Computes final performance metrics

    Args:
        db: Database connection.
        config: SimulatorConfig with all parameters.
        on_event: Optional SSE callback fn(event_type, data_dict).

    Returns:
        Complete simulation result dict.
    """
    from lib.trading.historical_data import (
        _ensure_sim_tables,
        build_market_snapshot,
        get_price_at,
        get_prices_range,
    )
    from lib.trading.intel_timeline import build_intel_context_at

    _ensure_sim_tables(db)

    def emit(event_type: str, data: dict):
        if on_event:
            try:
                on_event(event_type, data)
            except Exception as e:
                logger.debug('[Sim] Event emission failed: %s', e)

    # ── Create session ──
    session_id = f"sim_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    # Generate decision dates
    decision_dates = _generate_decision_dates(config.start_date, config.end_date,
                                               config.step_days, db)
    if not decision_dates:
        return {'error': 'No valid decision dates in range'}

    db.execute('''
        INSERT INTO trading_sim_sessions
        (session_id, status, initial_capital, current_cash, symbols,
         start_date, end_date, step_days, current_sim_date,
         total_steps, completed_steps, config_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        session_id, 'running', config.initial_capital, config.initial_capital,
        json.dumps(config.symbols), config.start_date, config.end_date,
        config.step_days, config.start_date,
        len(decision_dates), 0,
        json.dumps(config.to_dict(), ensure_ascii=False),
        now, now,
    ))
    db.commit()

    emit('sim_start', {
        'session_id': session_id,
        'total_steps': len(decision_dates),
        'start_date': config.start_date,
        'end_date': config.end_date,
    })

    logger.info('[Sim] Starting simulation %s: %s~%s, %d steps, ¥%.0f capital',
                session_id, config.start_date, config.end_date,
                len(decision_dates), config.initial_capital)

    # ── Portfolio state ──
    cash = config.initial_capital
    positions: dict[str, dict] = {}  # {symbol: {shares, buy_price, buy_date, name}}
    daily_values: list[dict] = []
    trade_log: list[dict] = []
    total_fees = 0.0
    total_trades = 0
    winning_trades = 0

    # ── Decision loop ──
    for step_idx, sim_date in enumerate(decision_dates):
        step_start = time.time()

        emit('sim_step', {
            'session_id': session_id,
            'step': step_idx + 1,
            'total': len(decision_dates),
            'sim_date': sim_date,
        })

        try:
            # ── 1. Update position prices to current sim_date ──
            portfolio_value = cash
            holdings_ctx_lines = []
            for sym, pos in list(positions.items()):
                price_data = get_price_at(db, sym, sim_date)
                if price_data:
                    current_nav = price_data['nav']
                    pos['current_price'] = current_nav
                    pos_value = current_nav * pos['shares']
                    pnl_pct = (current_nav - pos['buy_price']) / pos['buy_price'] * 100
                    portfolio_value += pos_value

                    holdings_ctx_lines.append(
                        f"- {sym} {pos.get('name', '')}: {pos['shares']:.2f}份, "
                        f"成本¥{pos['buy_price']:.4f}, 现价¥{current_nav:.4f}, "
                        f"市值¥{pos_value:,.2f}, 盈亏{pnl_pct:+.2f}%"
                    )

                    # ── Check stop-loss / take-profit ──
                    if pnl_pct <= -abs(config.stop_loss_pct):
                        # Stop-loss
                        proceeds = pos_value * (1 - config.sell_fee_rate)
                        fee = pos_value * config.sell_fee_rate
                        cash += proceeds
                        total_fees += fee
                        total_trades += 1
                        reason = f'⛔ 止损: {sym} 跌幅 {pnl_pct:.2f}% 超过止损线 -{config.stop_loss_pct}%'

                        _record_trade(db, session_id, sim_date, 'sell', sym,
                                      pos['shares'], current_nav, pos['buy_price'],
                                      pnl_pct, reason)
                        trade_log.append({
                            'date': sim_date, 'action': 'sell', 'symbol': sym,
                            'price': current_nav, 'pnl_pct': pnl_pct, 'trigger': 'stop_loss',
                        })

                        # Bug 7 fix: emit SSE event for stop-loss trades
                        emit('sim_trade', {
                            'step': step_idx + 1, 'sim_date': sim_date,
                            'action': 'sell', 'symbol': sym,
                            'price': current_nav, 'pnl_pct': pnl_pct,
                            'trigger': 'stop_loss',
                        })

                        del positions[sym]
                        portfolio_value = cash + sum(
                            p.get('current_price', p['buy_price']) * p['shares']
                            for p in positions.values()
                        )
                        continue

                    if pnl_pct >= abs(config.take_profit_pct):
                        # Take-profit
                        proceeds = pos_value * (1 - config.sell_fee_rate)
                        fee = pos_value * config.sell_fee_rate
                        cash += proceeds
                        total_fees += fee
                        total_trades += 1
                        winning_trades += 1
                        reason = f'✅ 止盈: {sym} 涨幅 {pnl_pct:.2f}% 达到止盈线 +{config.take_profit_pct}%'

                        _record_trade(db, session_id, sim_date, 'sell', sym,
                                      pos['shares'], current_nav, pos['buy_price'],
                                      pnl_pct, reason)
                        trade_log.append({
                            'date': sim_date, 'action': 'sell', 'symbol': sym,
                            'price': current_nav, 'pnl_pct': pnl_pct, 'trigger': 'take_profit',
                        })

                        # Bug 7 fix: emit SSE event for take-profit trades
                        emit('sim_trade', {
                            'step': step_idx + 1, 'sim_date': sim_date,
                            'action': 'sell', 'symbol': sym,
                            'price': current_nav, 'pnl_pct': pnl_pct,
                            'trigger': 'take_profit',
                        })

                        del positions[sym]
                        portfolio_value = cash + sum(
                            p.get('current_price', p['buy_price']) * p['shares']
                            for p in positions.values()
                        )
                        continue

            # Recalculate portfolio value
            portfolio_value = cash + sum(
                p.get('current_price', p['buy_price']) * p['shares']
                for p in positions.values()
            )

            # Record daily value
            daily_values.append({
                'date': sim_date,
                'value': portfolio_value,
                'cash': cash,
                'positions': len(positions),
            })

            # ── 2. Build quantitative signals ──
            signal_ctx = _build_signal_context(db, config.symbols, sim_date)

            # ── 3. Build market snapshot ──
            market_ctx = build_market_snapshot(db, sim_date)

            # ── 4. Build time-locked intel context ──
            intel_ctx = ''
            intel_count = 0
            try:
                intel_ctx, intel_count = build_intel_context_at(
                    db, sim_date, only_confident_dates=True
                )
            except Exception as e:
                logger.warning('[Sim] Intel context failed for %s: %s', sim_date, e)

            # ── 4.5 Emit analysis context for frontend ──
            _signal_highlights = _extract_signal_highlights(db, config.symbols, sim_date)
            _holdings_info = []
            for _sym, _pos in positions.items():
                _pnl = ((_pos.get('current_price', _pos['buy_price']) - _pos['buy_price'])
                        / _pos['buy_price'] * 100) if _pos['buy_price'] > 0 else 0
                _holdings_info.append({
                    'symbol': _sym,
                    'pnl_pct': round(_pnl, 2),
                })

            emit('sim_analyzing', {
                'step': step_idx + 1,
                'total': len(decision_dates),
                'sim_date': sim_date,
                'cash': round(cash, 2),
                'portfolio_value': round(portfolio_value, 2),
                'return_pct': round((portfolio_value - config.initial_capital) /
                                    config.initial_capital * 100, 2),
                'holdings': _holdings_info,
                'signals': _signal_highlights,
                'intel_count': intel_count,
                'has_market_ctx': bool(market_ctx),
            })

            # ── 5. Build LLM prompt ──
            holdings_text = '\n'.join(holdings_ctx_lines) if holdings_ctx_lines else '（无持仓）'
            return_pct = (portfolio_value - config.initial_capital) / config.initial_capital * 100
            win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

            # Historical journal context (last 5 decisions)
            journal_ctx = _get_recent_journal(db, session_id, limit=5)

            # Load strategy toolbox from DB (with live performance data)
            strategy_toolbox_prompt, strategy_list = _load_strategy_toolbox(db)

            prompt = f"""你正在进行历史模拟交易（股票、ETF、基金均可）。
⚠️ 当前模拟日期: {sim_date}（你只能看到此日期及之前的数据）

{strategy_toolbox_prompt}

## 账户状态
- 初始资金: ¥{config.initial_capital:,.2f}
- 当前现金: ¥{cash:,.2f}
- 持仓市值: ¥{portfolio_value - cash:,.2f}
- 组合总值: ¥{portfolio_value:,.2f}
- 累计收益: {return_pct:+.2f}%
- 历史胜率: {win_rate:.1f}% ({winning_trades}/{total_trades})
- 已完成步数: {step_idx + 1}/{len(decision_dates)}

## 当前持仓
{holdings_text}

## 可交易标的
{_format_tradeable_symbols(config.symbols)}

## 量化信号（截至{sim_date}）
{signal_ctx if signal_ctx else '（数据不足）'}

{market_ctx if market_ctx else ''}

## 市场情报（时间锁定至{sim_date}）
{intel_ctx[:4000] if intel_ctx else '（无情报数据）'}
（共{intel_count}条情报）

{journal_ctx}

## 交易规则
- 单笔不超过总资金的{config.max_position_pct}%
- 最多持有{config.max_positions}个标的
- 买入费率{config.buy_fee_rate*100:.2f}%，卖出费率{config.sell_fee_rate*100:.2f}%
- {'T+1交易（买入次日才能卖出）' if config.t_plus_1 else '无T+1限制'}
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

            # ── 6. Call LLM ──
            content = ''
            try:
                from lib.llm_dispatch import smart_chat
                content, usage = smart_chat(
                    messages=[
                        {'role': 'system', 'content': '你是一位专业的投资AI，拥有一套策略工具箱。你必须从工具箱中选择策略来指导决策，并在回答中标注使用了哪些策略。用中文回答。基于历史数据做决策，不要引用未来数据。'},
                        {'role': 'user', 'content': prompt},
                    ],
                    max_tokens=16384,
                    temperature=0.3,
                    capability='thinking',
                    timeout=120,
                    log_prefix='[Sim]',
                )
            except Exception as e:
                logger.error('[Sim] LLM call failed at %s: %s', sim_date, e, exc_info=True)
                content = ''
                emit('sim_error', {'step': step_idx + 1, 'sim_date': sim_date,
                                   'error': str(e)})

            # ── 7. Parse and execute decisions ──
            decisions = _parse_decisions(content)
            if content and not decisions:
                logger.warning('[Sim] No decisions parsed from LLM output at %s (content_len=%d). '
                               'Output may have been truncated by max_tokens.',
                               sim_date, len(content))
            elif decisions:
                logger.info('[Sim] Parsed %d decisions at %s: %s', len(decisions), sim_date,
                            [(d.get('action'), d.get('symbol', '')) for d in decisions])
            strategies_used = _parse_strategies_used(content, strategy_list)
            strategies_used_names = [s['name'] for s in strategies_used]

            # Record the full reasoning (include strategies used)
            _add_journal(db, session_id, sim_date, 'analysis', '', '', content[:10000],
                         {'strategies_used': strategies_used_names}, 0)

            for dec in decisions:
                action = dec.get('action', 'hold')
                symbol = dec.get('symbol', '')
                confidence = dec.get('confidence', 0)
                reason = dec.get('reason', '')

                if action == 'hold':
                    _add_journal(db, session_id, sim_date, 'decision', 'hold', symbol,
                                 reason, dec, confidence)
                    continue

                if confidence < config.min_confidence:
                    _add_journal(db, session_id, sim_date, 'skip', action, symbol,
                                 f'信心度{confidence}<{config.min_confidence}，跳过', dec, confidence)
                    continue

                if action == 'buy' and symbol:
                    if symbol in positions:
                        logger.info('[Sim] Skip buy %s at %s: already holding', symbol, sim_date)
                        _add_journal(db, session_id, sim_date, 'skip', 'buy', symbol,
                                     '已持有该标的，跳过重复买入', dec, confidence)
                        continue
                    if len(positions) >= config.max_positions:
                        logger.info('[Sim] Skip buy %s at %s: position limit (%d/%d)',
                                    symbol, sim_date, len(positions), config.max_positions)
                        _add_journal(db, session_id, sim_date, 'skip', 'buy', symbol,
                                     f'持仓数已达上限({len(positions)}/{config.max_positions})', dec, confidence)
                        continue

                    amount = float(dec.get('amount', 0))
                    if amount <= 0:
                        logger.info('[Sim] Skip buy %s at %s: amount<=0', symbol, sim_date)
                        _add_journal(db, session_id, sim_date, 'skip', 'buy', symbol,
                                     '买入金额<=0', dec, confidence)
                        continue
                    max_amount = config.initial_capital * config.max_position_pct / 100
                    amount = min(amount, cash * 0.95, max_amount)
                    if amount < 100:
                        logger.info('[Sim] Skip buy %s at %s: amount=%s < 100 after capping (cash=%.0f)',
                                    symbol, sim_date, amount, cash)
                        _add_journal(db, session_id, sim_date, 'skip', 'buy', symbol,
                                     f'可用金额不足(cash={cash:.0f}，计算后amount={amount:.0f}<100)', dec, confidence)
                        continue

                    price_data = get_price_at(db, symbol, sim_date)
                    if not price_data or price_data['nav'] <= 0:
                        _add_journal(db, session_id, sim_date, 'skip', 'buy', symbol,
                                     '无价格数据', dec, confidence)
                        continue

                    nav = price_data['nav']
                    fee = amount * config.buy_fee_rate
                    net_amount = amount - fee
                    shares = net_amount / nav
                    cash -= amount
                    total_fees += fee

                    positions[symbol] = {
                        'shares': shares,
                        'buy_price': nav,
                        'buy_date': sim_date,
                        'current_price': nav,
                        'name': symbol,
                        'strategies_used': strategies_used,  # track for perf recording
                    }

                    _add_journal(db, session_id, sim_date, 'decision', 'buy', symbol,
                                 reason, {'amount': amount, 'price': nav, 'shares': shares},
                                 confidence, amount)

                    _record_trade(db, session_id, sim_date, 'buy', symbol, shares, nav, nav, 0, reason)
                    trade_log.append({
                        'date': sim_date, 'action': 'buy', 'symbol': symbol,
                        'price': nav, 'amount': amount,
                    })

                    emit('sim_trade', {
                        'step': step_idx + 1, 'sim_date': sim_date,
                        'action': 'buy', 'symbol': symbol,
                        'amount': amount, 'price': nav,
                    })

                elif action == 'sell' and symbol:
                    if symbol not in positions:
                        logger.info('[Sim] Skip sell %s at %s: not in current positions', symbol, sim_date)
                        _add_journal(db, session_id, sim_date, 'skip', 'sell', symbol,
                                     '当前未持有该标的，无法卖出', dec, confidence)
                        continue

                    pos = positions[symbol]

                    # T+1 check
                    if config.t_plus_1 and pos['buy_date'] == sim_date:
                        _add_journal(db, session_id, sim_date, 'skip', 'sell', symbol,
                                     'T+1限制：买入当日不能卖出', dec, confidence)
                        continue

                    price_data = get_price_at(db, symbol, sim_date)
                    close_price = price_data['nav'] if price_data else pos['current_price']
                    pos_value = close_price * pos['shares']
                    fee = pos_value * config.sell_fee_rate
                    cash += pos_value - fee
                    total_fees += fee
                    total_trades += 1

                    pnl_pct = (close_price - pos['buy_price']) / pos['buy_price'] * 100
                    if pnl_pct > 0:
                        winning_trades += 1

                    # Record per-strategy performance (feedback loop)
                    buy_strategies = pos.get('strategies_used', [])
                    sell_strategies = strategies_used
                    # Credit both buy-side and sell-side strategies
                    all_strats = {s.get('name', ''): s for s in buy_strategies + sell_strategies}
                    _record_sim_strategy_performance(
                        db, session_id, list(all_strats.values()), pnl_pct,
                        {'symbol': symbol, 'buy_date': pos['buy_date'], 'sell_date': sim_date}
                    )

                    _add_journal(db, session_id, sim_date, 'decision', 'sell', symbol,
                                 reason, {'price': close_price, 'pnl_pct': pnl_pct,
                                          'strategies_used': [s.get('name', '') for s in all_strats.values()]},
                                 confidence)

                    _record_trade(db, session_id, sim_date, 'sell', symbol,
                                  pos['shares'], close_price, pos['buy_price'],
                                  pnl_pct, reason)
                    trade_log.append({
                        'date': sim_date, 'action': 'sell', 'symbol': symbol,
                        'price': close_price, 'pnl_pct': pnl_pct,
                    })

                    del positions[symbol]

                    emit('sim_trade', {
                        'step': step_idx + 1, 'sim_date': sim_date,
                        'action': 'sell', 'symbol': symbol,
                        'price': close_price, 'pnl_pct': pnl_pct,
                    })

            # ── Update session in DB ──
            portfolio_value = cash + sum(
                p.get('current_price', p['buy_price']) * p['shares']
                for p in positions.values()
            )
            total_pnl = portfolio_value - config.initial_capital

            db.execute('''
                UPDATE trading_sim_sessions SET
                  current_cash=?, current_sim_date=?, completed_steps=?,
                  total_pnl=?, total_trades=?, winning_trades=?, updated_at=?
                WHERE session_id=?
            ''', (cash, sim_date, step_idx + 1, total_pnl,
                  total_trades, winning_trades,
                  datetime.now().strftime('%Y-%m-%d %H:%M:%S'), session_id))
            db.commit()

            step_elapsed = time.time() - step_start

            # Build actions list for frontend timeline display
            # ★ Only include ACTUALLY EXECUTED trades, not just LLM intentions.
            # Executed trades are already recorded in trade_log during this step.
            step_actions = []
            for t in trade_log:
                if t['date'] == sim_date:
                    step_actions.append({
                        'action': t['action'],
                        'symbol': t['symbol'],
                        'amount': t.get('amount', 0),
                        'confidence': 0,
                    })

            # Extract a short summary from reasoning (first useful line)
            summary = ''
            if content:
                for line in content.split('\n'):
                    line = line.strip()
                    if len(line) > 20 and not line.startswith('#') and not line.startswith('<'):
                        summary = line[:200]
                        break

            # Extract reasoning text for frontend (content without XML blocks)
            reasoning_text = ''
            if content:
                reasoning_text = re.sub(
                    r'<decisions>.*?</decisions>', '', content, flags=re.DOTALL
                ).strip()
                reasoning_text = re.sub(
                    r'<strategies_used>.*?</strategies_used>', '', reasoning_text, flags=re.DOTALL
                ).strip()
                # Keep full reasoning — no aggressive truncation
                reasoning_text = reasoning_text[:10000]

            return_pct = round((portfolio_value - config.initial_capital) /
                               config.initial_capital * 100, 2)

            emit('sim_step_done', {
                'step': step_idx + 1,
                'sim_date': sim_date,
                'portfolio_value': round(portfolio_value, 2),
                'cash': round(cash, 2),
                'return_pct': return_pct,
                'positions': len(positions),
                'decisions': len(decisions),
                'actions': step_actions,
                'summary': summary,
                'reasoning': reasoning_text,
                'elapsed': round(step_elapsed, 1),
                'strategies_used': strategies_used_names,
            })

            # ── Persist step summary to journal for historical review ──
            # The 'analysis'/'decision' entries above are per-decision;
            # this 'step_summary' aggregates everything the frontend needs
            # when loading results later (portfolio_value, return_pct, actions).
            _add_journal(
                db, session_id, sim_date, 'step_summary', '', '',
                reasoning=reasoning_text,
                signals={
                    'portfolio_value': round(portfolio_value, 2),
                    'cash': round(cash, 2),
                    'return_pct': return_pct,
                    'actions': step_actions,
                    'summary': summary,
                    'step': step_idx + 1,
                    'positions_count': len(positions),
                },
                confidence=0,
                amount=round(portfolio_value, 2),
            )

        except Exception as e:
            logger.error('[Sim] Step %d (%s) failed: %s', step_idx + 1, sim_date, e, exc_info=True)
            emit('sim_error', {'step': step_idx + 1, 'sim_date': sim_date, 'error': str(e)})

    # ── Close all remaining positions at end ──
    for sym, pos in list(positions.items()):
        close_price = pos.get('current_price', pos['buy_price'])
        pos_value = close_price * pos['shares']
        fee = pos_value * config.sell_fee_rate
        cash += pos_value - fee
        total_fees += fee
        total_trades += 1
        pnl_pct = (close_price - pos['buy_price']) / pos['buy_price'] * 100
        if pnl_pct > 0:
            winning_trades += 1
        _record_trade(db, session_id, config.end_date, 'sell', sym,
                      pos['shares'], close_price, pos['buy_price'],
                      pnl_pct, '模拟结束，强制平仓')

    # ── Compute final metrics ──
    final_value = cash
    metrics = _compute_metrics(daily_values, config.initial_capital, total_fees,
                               total_trades, winning_trades)

    # ── Compute benchmark ──
    benchmark = _compute_benchmark(db, config.benchmark_index,
                                    config.start_date, config.end_date)

    # ── Save final result ──
    result = {
        'session_id': session_id,
        'status': 'completed',
        'config': config.to_dict(),
        'metrics': metrics,
        'benchmark': benchmark,
        'daily_values': daily_values,
        'trade_log': trade_log,
        'total_fees': round(total_fees, 2),
    }

    db.execute('''
        UPDATE trading_sim_sessions SET
          status='completed', current_cash=?,
          total_pnl=?, total_trades=?, winning_trades=?,
          result_json=?, updated_at=?
        WHERE session_id=?
    ''', (cash, final_value - config.initial_capital,
          total_trades, winning_trades,
          json.dumps(result, ensure_ascii=False, default=str),
          datetime.now().strftime('%Y-%m-%d %H:%M:%S'), session_id))
    db.commit()

    emit('sim_complete', {
        'session_id': session_id,
        'metrics': metrics,
        'benchmark': benchmark,
        'total_fees': round(total_fees, 2),
        'trade_count': total_trades,
    })

    logger.info('[Sim] Completed %s: return=%.2f%%, max_dd=%.2f%%, trades=%d, sharpe=%.2f',
                session_id, metrics.get('total_return_pct', 0),
                metrics.get('max_drawdown_pct', 0), total_trades,
                metrics.get('sharpe_ratio', 0))

    return result


# ═══════════════════════════════════════════════════════════
#  Session Queries
# ═══════════════════════════════════════════════════════════

def get_sim_session(db: Any, session_id: str) -> dict | None:
    """Get simulation session by ID."""
    from lib.trading.historical_data import _ensure_sim_tables
    _ensure_sim_tables(db)
    row = db.execute(
        'SELECT * FROM trading_sim_sessions WHERE session_id=?', (session_id,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d['config'] = json.loads(d.get('config_json', '{}'))
    except Exception as _e:
        logger.debug('[Sim] JSON parse failed for session %s config: %s', session_id, _e)
        d['config'] = {}
    try:
        d['result'] = json.loads(d.get('result_json', '{}'))
    except Exception as _e:
        logger.debug('[Sim] JSON parse failed for session %s result: %s', session_id, _e)
        d['result'] = {}
    return d


def list_sim_sessions(db: Any, limit: int = 20) -> list[dict]:
    """List all simulation sessions."""
    from lib.trading.historical_data import _ensure_sim_tables
    _ensure_sim_tables(db)
    rows = db.execute(
        'SELECT * FROM trading_sim_sessions ORDER BY created_at DESC LIMIT ?', (limit,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['config'] = json.loads(d.get('config_json', '{}'))
        except Exception as _e:
            logger.debug('[Sim] JSON parse failed for session list config: %s', _e)
            d['config'] = {}
        # Bug 3 fix: parse result_json so metrics/benchmark are available
        try:
            parsed_result = json.loads(d.get('result_json', '{}'))
            d['metrics'] = parsed_result.get('metrics', {})
            d['benchmark'] = parsed_result.get('benchmark', {})
            d['strategy'] = d['config'].get('strategy',
                                            d['config'].get('risk_level', 'balanced'))
        except Exception as _e:
            logger.debug('[Sim] JSON parse failed for session list result: %s', _e)
            d['metrics'] = {}
            d['benchmark'] = {}
        result.append(d)
    return result


def get_sim_positions(db: Any, session_id: str) -> list[dict]:
    """Get simulation positions."""
    rows = db.execute(
        'SELECT * FROM trading_sim_positions WHERE session_id=? ORDER BY created_at DESC',
        (session_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_sim_journal(db: Any, session_id: str, limit: int = 100,
                    entry_type: str = '') -> list[dict]:
    """Get simulation decision journal.

    Args:
        db: Database connection.
        session_id: Session to query.
        limit: Max rows.
        entry_type: Optional filter — e.g. 'step_summary' for aggregated
                    per-step data that includes portfolio_value and actions.
    """
    if entry_type:
        rows = db.execute(
            'SELECT * FROM trading_sim_journal '
            'WHERE session_id=? AND entry_type=? '
            'ORDER BY sim_date ASC, id ASC LIMIT ?',
            (session_id, entry_type, limit)
        ).fetchall()
    else:
        rows = db.execute(
            'SELECT * FROM trading_sim_journal '
            'WHERE session_id=? '
            'ORDER BY sim_date DESC, id DESC LIMIT ?',
            (session_id, limit)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d['signals'] = json.loads(d.get('signals_json', '{}'))
        except Exception as _e:
            logger.debug('[Sim] JSON parse failed for journal signals: %s', _e)
            d['signals'] = {}
        result.append(d)
    return result


def get_sim_stats(db: Any, session_id: str) -> dict:
    """Get comprehensive stats for a simulation."""
    session = get_sim_session(db, session_id)
    if not session:
        return {'error': 'Session not found'}

    result = session.get('result', {})
    metrics = result.get('metrics', {})
    return {
        'session': {
            'session_id': session['session_id'],
            'status': session['status'],
            'start_date': session['start_date'],
            'end_date': session['end_date'],
            'initial_capital': session['initial_capital'],
        },
        'metrics': metrics,
        'benchmark': result.get('benchmark', {}),
        'total_trades': session.get('total_trades', 0),
        'winning_trades': session.get('winning_trades', 0),
        'total_pnl': session.get('total_pnl', 0),
        # Bug 2 fix: include fields the frontend actually reads
        'trade_count': session.get('total_trades', 0),
        'total_fees': metrics.get('total_fees', result.get('total_fees', 0)),
    }


# ═══════════════════════════════════════════════════════════
#  Private Helpers
# ═══════════════════════════════════════════════════════════

def _generate_decision_dates(
    start: str, end: str, step_days: int, db: Any
) -> list[str]:
    """Generate list of decision dates, preferring actual trading days.

    Uses stored price data to identify valid trading days.
    Falls back to calendar dates if no price data.
    """
    from lib.trading.historical_data import _ensure_sim_tables
    _ensure_sim_tables(db)

    # Try to get actual trading dates from stored data
    rows = db.execute(
        "SELECT DISTINCT date FROM trading_sim_prices "
        "WHERE date>=? AND date<=? ORDER BY date",
        (start, end)
    ).fetchall()

    if rows and len(rows) > 0:
        trading_dates = [r['date'] for r in rows]
        # Sample every N-th trading day
        return trading_dates[::step_days]

    # Fallback: calendar dates skipping weekends
    dates = []
    try:
        current = datetime.strptime(start, '%Y-%m-%d')
        end_dt = datetime.strptime(end, '%Y-%m-%d')
        while current <= end_dt:
            if current.weekday() < 5:  # Monday-Friday
                dates.append(current.strftime('%Y-%m-%d'))
            current += timedelta(days=1)
        return dates[::step_days]
    except Exception as e:
        logger.error('[Sim] Date generation failed: %s', e)
        return []


def _build_signal_context(db: Any, symbols: list[str], as_of: str) -> str:
    """Build quantitative signal context for a given date."""
    from lib.trading.historical_data import get_prices_range

    lines = []
    # Look back 60 trading days for signal computation
    try:
        lookback_start = (datetime.strptime(as_of, '%Y-%m-%d') - timedelta(days=90)).strftime('%Y-%m-%d')
    except ValueError as _e:
        logger.debug('[Sim] Date parse failed in _build_signal_context: %s', _e)
        return ''

    for symbol in symbols:
        navs = get_prices_range(db, symbol, lookback_start, as_of)
        if len(navs) < 20:
            lines.append(f'{symbol}: 数据不足（{len(navs)}天）')
            continue

        prices = [n['nav'] for n in navs]
        dates = [n['date'] for n in navs]

        # Simple technical indicators
        current = prices[-1]
        ma5 = sum(prices[-5:]) / 5 if len(prices) >= 5 else current
        ma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else current
        ma60 = sum(prices[-60:]) / 60 if len(prices) >= 60 else current

        # RSI(14)
        rsi = _compute_rsi(prices, 14)

        # Recent return
        ret_5d = (prices[-1] / prices[-6] - 1) * 100 if len(prices) >= 6 else 0
        ret_20d = (prices[-1] / prices[-21] - 1) * 100 if len(prices) >= 21 else 0

        # Volatility (20-day)
        if len(prices) >= 21:
            returns = [(prices[i] / prices[i-1] - 1) for i in range(-20, 0)]
            vol = (sum(r*r for r in returns) / len(returns)) ** 0.5 * (252 ** 0.5) * 100
        else:
            vol = 0

        trend = '📈多头' if ma5 > ma20 > ma60 else '📉空头' if ma5 < ma20 < ma60 else '↔️震荡'

        lines.append(
            f'**{symbol}** (截至{as_of}):\n'
            f'  现价: {current:.4f} | MA5: {ma5:.4f} | MA20: {ma20:.4f} | MA60: {ma60:.4f}\n'
            f'  趋势: {trend} | RSI(14): {rsi:.1f} | 波动率: {vol:.1f}%\n'
            f'  近5日: {ret_5d:+.2f}% | 近20日: {ret_20d:+.2f}%'
        )

    return '\n'.join(lines)


def _compute_rsi(prices: list[float], period: int = 14) -> float:
    """Compute RSI for a price series."""
    if len(prices) < period + 1:
        return 50.0
    changes = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    recent = changes[-period:]
    gains = [c for c in recent if c > 0]
    losses = [-c for c in recent if c < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _extract_signal_highlights(db, symbols: list[str], as_of: str) -> list[dict]:
    """Extract structured signal highlights for frontend display.

    Returns a list of per-symbol dicts with trend, RSI, and recent returns.
    Used by the sim_analyzing event to show users what the AI is reviewing.
    """
    from lib.trading.historical_data import get_prices_range

    highlights = []
    try:
        lookback_start = (datetime.strptime(as_of, '%Y-%m-%d')
                          - timedelta(days=90)).strftime('%Y-%m-%d')
    except (ValueError, TypeError) as _e:
        logger.debug('[Sim] Date parse failed in signal_highlights: %s', _e)
        return highlights

    for symbol in symbols:
        try:
            navs = get_prices_range(db, symbol, lookback_start, as_of)
        except Exception as e:
            logger.debug('[Sim] Signal highlights fetch failed for %s: %s', symbol, e)
            highlights.append({'symbol': symbol, 'data': 'error'})
            continue

        if len(navs) < 5:
            highlights.append({'symbol': symbol, 'data': 'insufficient'})
            continue

        prices = [n['nav'] for n in navs]
        current = prices[-1]
        ma5 = sum(prices[-5:]) / 5
        ma20 = sum(prices[-20:]) / 20 if len(prices) >= 20 else current
        ma60 = sum(prices[-60:]) / 60 if len(prices) >= 60 else current
        rsi = _compute_rsi(prices, 14)
        ret_5d = (prices[-1] / prices[-6] - 1) * 100 if len(prices) >= 6 else 0

        if ma5 > ma20 > ma60:
            trend = 'bullish'
        elif ma5 < ma20 < ma60:
            trend = 'bearish'
        else:
            trend = 'sideways'

        highlights.append({
            'symbol': symbol,
            'trend': trend,
            'rsi': round(rsi, 1),
            'ret_5d': round(ret_5d, 2),
            'price': round(current, 4),
        })

    return highlights


def _parse_decisions(content: str) -> list[dict]:
    """Parse LLM output for <decisions>...</decisions> JSON."""
    if not content:
        return []
    match = re.search(r'<decisions>\s*(.*?)\s*</decisions>', content, re.DOTALL)
    if not match:
        return []
    try:
        decisions = json.loads(match.group(1))
        if isinstance(decisions, list):
            return decisions
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning('[Sim] Decision JSON parse failed: %s', e)
    return []


def _parse_strategies_used(content: str, all_strategies: list[dict]) -> list[dict]:
    """Parse <strategies_used>["name1", ...]</strategies_used> from LLM output.

    Returns list of strategy dicts (with id, name) that the LLM cited.
    """
    if not content:
        return []
    match = re.search(r'<strategies_used>\s*(.*?)\s*</strategies_used>', content, re.DOTALL)
    if not match:
        return []
    try:
        names = json.loads(match.group(1))
        if not isinstance(names, list):
            return []
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Sim] strategies_used parse failed: %s', e)
        return []

    # Map names back to strategy dicts
    name_to_strat = {s['name']: s for s in all_strategies}
    result = []
    for n in names:
        if isinstance(n, str) and n in name_to_strat:
            result.append(name_to_strat[n])
    return result


def _add_journal(db, session_id, sim_date, entry_type, action, symbol,
                 reasoning, signals=None, confidence=0, amount=0):
    """Add entry to simulation journal."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        db.execute('''
            INSERT INTO trading_sim_journal
            (session_id, sim_date, entry_type, action, symbol, amount,
             reasoning, signals_json, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            session_id, sim_date, entry_type, action, symbol, amount,
            reasoning[:10000] if reasoning else '',
            json.dumps(signals or {}, ensure_ascii=False, default=str),
            confidence, now,
        ))
    except Exception as e:
        logger.warning('[Sim] Journal insert error (session=%s, date=%s, type=%s): %s',
                       session_id, sim_date, entry_type, e)


def _record_trade(db, session_id, sim_date, action, symbol, shares, price, buy_price, pnl_pct, reason):
    """Record a trade in sim_positions table.

    Args:
        db: Database connection.
        session_id: Session ID.
        sim_date: Trade date.
        action: 'buy' or 'sell'.
        symbol: Fund/ETF code.
        shares: Number of shares.
        price: Trade price (nav).
        buy_price: Original buy price (for PNL calc on sell; same as price for buy).
        pnl_pct: P&L percentage (0 for buy).
        reason: Trade reason text.
    """
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        if action == 'buy':
            db.execute('''
                INSERT INTO trading_sim_positions
                (session_id, symbol, shares, buy_price, buy_date,
                 current_price, status, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
            ''', (session_id, symbol, shares, price, sim_date, price, reason, now))
        elif action == 'sell':
            pnl_abs = (price - buy_price) * shares
            db.execute('''
                UPDATE trading_sim_positions SET
                  status='closed', close_price=?, close_date=?,
                  pnl=?, pnl_pct=?
                WHERE session_id=? AND symbol=? AND status='open'
            ''', (price, sim_date, pnl_abs, pnl_pct,
                  session_id, symbol))
    except Exception as e:
        logger.warning('[Sim] Trade record error: %s', e)


def _get_recent_journal(db, session_id, limit=5) -> str:
    """Get recent journal entries as context text."""
    rows = db.execute(
        "SELECT sim_date, action, symbol, reasoning FROM trading_sim_journal "
        "WHERE session_id=? AND entry_type='decision' "
        "ORDER BY id DESC LIMIT ?",
        (session_id, limit)
    ).fetchall()
    if not rows:
        return ''
    lines = ['## 近期决策记录']
    for r in rows:
        r = dict(r)
        lines.append(f"- [{r['sim_date']}] {r['action']} {r['symbol']}: "
                      f"{r['reasoning'][:120]}")
    return '\n'.join(lines)


def _compute_metrics(
    daily_values: list[dict],
    initial_capital: float,
    total_fees: float,
    total_trades: int,
    winning_trades: int,
) -> dict[str, Any]:
    """Compute performance metrics from daily values."""
    if not daily_values:
        return {}

    values = [d['value'] for d in daily_values]
    dates = [d['date'] for d in daily_values]

    final_value = values[-1]
    total_return = final_value - initial_capital
    total_return_pct = (total_return / initial_capital) * 100

    # Max drawdown
    peak = values[0]
    max_dd = 0
    max_dd_peak_date = dates[0]
    max_dd_trough_date = dates[0]
    current_peak_date = dates[0]
    for i, v in enumerate(values):
        if v > peak:
            peak = v
            current_peak_date = dates[i]
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd
            max_dd_peak_date = current_peak_date
            max_dd_trough_date = dates[i]

    # Daily returns for Sharpe ratio
    daily_returns = []
    for i in range(1, len(values)):
        if values[i-1] > 0:
            daily_returns.append(values[i] / values[i-1] - 1)

    # Annualized Sharpe (assuming 0% risk-free rate)
    # Bug 13 fix: daily_values are step_days apart, not truly daily.
    # Approximate periods-per-year from sample count + date range.
    sharpe = 0
    if daily_returns and len(daily_returns) > 5:
        avg_ret = sum(daily_returns) / len(daily_returns)
        std_ret = (sum((r - avg_ret) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
        if std_ret > 0:
            # Estimate annualization factor from actual data frequency
            try:
                d0 = datetime.strptime(dates[0], '%Y-%m-%d')
                d1 = datetime.strptime(dates[-1], '%Y-%m-%d')
                span_days = max((d1 - d0).days, 1)
                periods_per_year = len(daily_returns) / span_days * 365
            except (ValueError, TypeError) as _e:
                logger.debug('[Sim] Date parse failed for annualization, defaulting to 252: %s', _e)
                periods_per_year = 252  # fallback to daily
            sharpe = (avg_ret / std_ret) * (periods_per_year ** 0.5)

    # Win rate
    win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0

    # Annualized return
    days = len(daily_values)
    ann_return = ((final_value / initial_capital) ** (252 / max(days, 1)) - 1) * 100

    return {
        'total_return': round(total_return, 2),
        'total_return_pct': round(total_return_pct, 2),
        'annualized_return_pct': round(ann_return, 2),
        'max_drawdown_pct': round(max_dd * 100, 2),
        'max_dd_peak_date': max_dd_peak_date,
        'max_dd_trough_date': max_dd_trough_date,
        'sharpe_ratio': round(sharpe, 2),
        'win_rate': round(win_rate, 1),
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'total_fees': round(total_fees, 2),
        'final_value': round(final_value, 2),
        'simulation_days': days,
    }


def _compute_benchmark(
    db: Any,
    benchmark_secid: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Compute benchmark (buy-and-hold index) return for comparison."""
    from lib.trading.historical_data import get_index_at

    start_data = get_index_at(db, benchmark_secid, start_date)
    end_data = get_index_at(db, benchmark_secid, end_date)

    if not start_data or not end_data:
        return {'error': 'Benchmark data not available'}

    start_price = start_data['close']
    end_price = end_data['close']

    if start_price <= 0:
        return {'error': 'Invalid benchmark start price'}

    return_pct = (end_price / start_price - 1) * 100

    return {
        'name': start_data.get('name', benchmark_secid),
        'start_price': start_price,
        'end_price': end_price,
        'return_pct': round(return_pct, 2),
        'start_date': start_data['date'],
        'end_date': end_data['date'],
    }
