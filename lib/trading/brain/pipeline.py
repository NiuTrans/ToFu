"""lib/trading/brain/pipeline.py — Unified 6-Phase Decision Pipeline.

This is THE single entry point for all investment decisions. No other module
should independently generate buy/sell recommendations.

Pipeline phases:
  Phase 1: Data Collection (parallel) — radar intel + market + holdings + history
  Phase 2: Quantitative Analysis — signals + KPI + screening candidates
  Phase 3: Quick Backtest Validation — top candidates get 90-day fast backtest
  Phase 4: Bull vs Bear Debate — parallel dual-agent argumentation
  Phase 5: LLM Synthesis — mega-prompt with all data → structured orders
  Phase 6: Strategy Evolution — record decision logic, update strategy weights

Trigger modes:
  - 'manual':    User clicks "分析" in AI操盘 tab
  - 'scheduled': Periodic scheduler tick (every N hours)
  - 'alert':     Breaking event detected by Radar alert engine
  - 'morning':   Pre-market morning briefing (07:00)
"""

from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Any

import lib as _lib  # module ref for hot-reload
from lib.log import get_logger, log_context
from lib.trading._common import TradingClient

logger = get_logger(__name__)

__all__ = [
    'run_brain_analysis',
    'build_brain_streaming_body',
]


def _gather_full_context(
    db: Any,
    trigger: str = 'manual',
    news_items: list[dict] | None = None,
    *,
    client: TradingClient | None = None,
    scan_new_candidates: bool = True,
) -> dict[str, Any]:
    """Phase 1 + 2 + 3: Gather all context for brain analysis.

    This is the unified data-collection function that replaces:
      - trading_decision._gather_news_cached()
      - trading_autopilot.cycle._gather_context()
      - trading_screening.smart_select_assets()

    Args:
        db: Database connection
        trigger: what triggered this analysis
        news_items: optional live news dicts
        client: optional TradingClient for DI
        scan_new_candidates: whether to run candidate screening (Phase 2b)
    """
    ctx = {'trigger': trigger, 'timestamp': datetime.now().isoformat()}

    # ── Phase 1a: Intelligence context ──
    try:
        from lib.trading import build_intel_context
        intel_ctx, intel_count = build_intel_context(db)
        ctx['intel_ctx'] = intel_ctx
        ctx['intel_count'] = intel_count
    except Exception as e:
        logger.warning('[Brain] Intel context failed: %s', e, exc_info=True)
        ctx['intel_ctx'] = ''
        ctx['intel_count'] = 0

    # Inject live news if provided
    if news_items:
        news_lines = ["### 实时新闻"]
        for n in news_items[:15]:
            news_lines.append(f"- [{n.get('title', '')}] {n.get('snippet', '')}")
        ctx['intel_ctx'] = "\n".join(news_lines) + "\n\n" + ctx.get('intel_ctx', '')

    # ── Phase 1b: Current holdings + cash ──
    holdings = db.execute('SELECT * FROM trading_holdings').fetchall()
    holdings = [dict(h) for h in holdings]
    held_codes = [h['symbol'] for h in holdings]
    ctx['holdings'] = holdings
    ctx['held_codes'] = held_codes

    cfg = db.execute(
        "SELECT value FROM trading_config WHERE key='available_cash'"
    ).fetchone()
    ctx['cash'] = float(cfg['value']) if cfg else 0

    # Build human-readable holdings context
    holdings_ctx = ""
    from lib.trading import fetch_asset_info, get_latest_price
    for h in holdings:
        try:
            nav_val, nav_date = get_latest_price(h['symbol'], client=client)
            info = fetch_asset_info(h['symbol'], client=client)
            name = info.get('name', '') if info else ''
            cost = h.get('buy_price', 0)
            pnl = ((nav_val - cost) / cost * 100) if nav_val and cost else 0
            holdings_ctx += (
                f"- {h['symbol']} {name}: {h['shares']}份, "
                f"成本¥{cost}, 现价¥{nav_val or 'N/A'}, 盈亏{pnl:+.2f}%\n"
            )
        except Exception as e:
            logger.debug('[Brain] NAV fetch degraded for %s: %s', h['symbol'], e, exc_info=True)
            holdings_ctx += f"- {h['symbol']}: {h['shares']}份, 成本¥{h.get('buy_price', 0)}\n"
    ctx['holdings_ctx'] = holdings_ctx

    # ── Phase 1c: Active strategies ──
    strategies = db.execute(
        "SELECT * FROM trading_strategies WHERE status='active' ORDER BY updated_at DESC"
    ).fetchall()
    ctx['strategies_ctx'] = "\n".join([
        f"- [{dict(s)['type']}] {dict(s)['name']}: {dict(s)['logic']}"
        for s in strategies
    ])

    # ── Phase 1d: Correlations ──
    try:
        from lib.trading_autopilot.correlation import build_correlation_context, correlate_intel_items
        correlations = correlate_intel_items(db)
        ctx['correlations'] = correlations
        ctx['correlation_ctx'] = build_correlation_context(correlations)
    except Exception as e:
        logger.warning('[Brain] Correlation analysis failed: %s', e, exc_info=True)
        ctx['correlations'] = []
        ctx['correlation_ctx'] = ''

    # ── Phase 1e: Strategy evolution ──
    try:
        from lib.trading_autopilot.strategy_evolution import evolve_strategies
        evolution_ctx, evolution_items = evolve_strategies(db)
        ctx['evolution_ctx'] = evolution_ctx
        ctx['evolution_items'] = evolution_items
    except Exception as e:
        logger.warning('[Brain] Strategy evolution failed: %s', e, exc_info=True)
        ctx['evolution_ctx'] = ''
        ctx['evolution_items'] = []

    # ── Phase 2a: KPI evaluation for held assets ──
    ctx['kpi_evaluations'] = {}
    if held_codes:
        try:
            from lib.trading_autopilot.kpi import pre_backtest_evaluate
            ctx['kpi_evaluations'] = pre_backtest_evaluate(
                db, held_codes, lookback_days=90, client=client,
            )
        except Exception as e:
            logger.warning('[Brain] KPI evaluation failed: %s', e, exc_info=True)

    # ── Phase 2b: Scan for new candidates (optional) ──
    ctx['new_candidates'] = []
    if scan_new_candidates and ctx['cash'] > 1000:
        try:
            from lib.trading.screening import screen_assets
            screening_result = screen_assets(
                criteria={
                    'asset_type': 'all',
                    'sort': '3month',
                    'top_n': 10,
                    'min_size': 1.0,
                },
                client=client, db=db,
            )
            candidates = screening_result.get('candidates', [])
            # Exclude already-held
            ctx['new_candidates'] = [
                c for c in candidates
                if c.get('code') not in set(held_codes)
            ][:8]
            logger.info('[Brain] Found %d new candidates (after excluding %d held)',
                        len(ctx['new_candidates']), len(held_codes))
        except Exception as e:
            logger.warning('[Brain] Candidate screening failed: %s', e, exc_info=True)

    # ── Phase 1f: Fee context for holdings ──
    fee_ctx = ""
    try:
        from lib.trading import calc_sell_fee, fetch_trading_fees
        for h in holdings:
            fees = fetch_trading_fees(h['symbol'], client=client)
            sell_info = calc_sell_fee(h, client=client)
            fee_ctx += (
                f"- {h['symbol']}: 申购费{fees['buy_fee_rate']*100:.2f}% | "
                f"管理费{fees['management_fee']*100:.2f}%/年 | "
                f"当前赎回费{sell_info['fee_rate']*100:.2f}%"
                f"（持有{sell_info['holding_days']}天）\n"
            )
    except Exception as e:
        logger.debug('[Brain] Fee context failed: %s', e, exc_info=True)
    ctx['fee_ctx'] = fee_ctx

    # ── Pending alerts ──
    try:
        from lib.trading.radar.alert import get_pending_alerts
        ctx['alerts'] = get_pending_alerts()
    except Exception as e:
        logger.debug('[Brain] Alert check failed: %s', e, exc_info=True)
        ctx['alerts'] = []

    return ctx


def _build_brain_prompt(ctx: dict[str, Any], cycle_number: int = 1) -> str:
    """Build the unified mega-prompt for the Brain.

    This replaces both:
      - trading_decision._build_recommend_prompt()
      - trading_autopilot.reasoning.build_autopilot_prompt()
    """
    # Build KPI text
    kpi_text = ""
    kpi_evaluations = ctx.get('kpi_evaluations', {})
    if kpi_evaluations:
        kpi_lines = ["## 持仓标的KPI + 量化信号评估"]
        for code, eval_data in kpi_evaluations.items():
            if 'error' in eval_data:
                kpi_lines.append(f"\n### {code}: ⚠️ {eval_data['error']}")
                continue
            k = eval_data['kpis']
            kpi_lines.append(f"\n### {code} {eval_data.get('asset_name', '')}")
            kpi_lines.append(f"  综合推荐分: {eval_data['recommendation_score']}/100")
            kpi_lines.append(f"  总收益: {k['total_return']}% | 年化: {k['annual_return']}%")
            kpi_lines.append(f"  最大回撤: {k['max_drawdown']}% | 波动率: {k['volatility']}%")
            kpi_lines.append(f"  夏普: {k['sharpe_ratio']} | 索提诺: {k['sortino_ratio']}")
            kpi_lines.append(f"  胜率: {k['win_days_pct']}% | VaR(95%): {k['var_95']}%")

            qs = eval_data.get('quant_signals', {})
            if qs and 'error' not in qs:
                comp = qs.get('composite', {})
                regime = qs.get('regime', {})
                rsi_data = qs.get('rsi', {})
                macd_data = qs.get('macd', {})
                if comp:
                    kpi_lines.append(f"  综合信号: {comp.get('signal', 'N/A')} (得分: {comp.get('score', 'N/A')}/100)")
                if regime:
                    kpi_lines.append(f"  市场体制: {regime.get('regime', 'N/A')}")
                if rsi_data:
                    kpi_lines.append(f"  RSI: {rsi_data.get('value', 'N/A')} ({rsi_data.get('signal', 'N/A')})")
                if macd_data:
                    kpi_lines.append(f"  MACD: {macd_data.get('signal', 'N/A')}")
        kpi_text = "\n".join(kpi_lines)

    # Build candidates text
    candidates_text = ""
    new_candidates = ctx.get('new_candidates', [])
    if new_candidates:
        cand_lines = ["## 新候选标的 (量化筛选结果)"]
        for c in new_candidates[:8]:
            cand_lines.append(
                f"- {c.get('code', '')} {c.get('name', '')}: "
                f"综合评分 {c.get('total_score', 0):.1f}, "
                f"推荐: {c.get('recommendation', 'N/A')}, "
                f"3月收益: {c.get('returns', {}).get('3m', 'N/A')}%"
            )
        candidates_text = "\n".join(cand_lines)

    # Alerts text
    alerts_text = ""
    alerts = ctx.get('alerts', [])
    if alerts:
        alert_lines = ["## ⚡ 突发预警"]
        for a in alerts[:5]:
            alert_lines.append(f"- [{a.get('type', '')}] {a.get('title', '')} (紧急度: {a.get('urgency', 0)})")
        alerts_text = "\n".join(alert_lines)

    # Debate context (injected after Phase 4)
    debate_ctx = ctx.get('debate_ctx', '') or ''

    trigger = ctx.get('trigger', 'manual')
    trigger_label = {
        'manual': '手动触发', 'scheduled': '定时分析',
        'alert': '突发事件触发', 'morning': '晨间例行分析',
    }.get(trigger, trigger)

    return f"""你是一位全球顶级投资超级分析师 (Autonomous Fund Super-Analyst)。
第 {cycle_number} 轮分析周期 | 触发方式: {trigger_label}

你的核心纪律:
1. 当量化信号与情报矛盾时，优先相信量化信号
2. RSI>75时不追高，RSI<25时关注抄底机会
3. 综合信号得分<-30时启动防御模式
4. 每笔建议必须包含止损位和目标收益率
5. 考虑T+1交易规则和赎回费率对操作时机的影响

{alerts_text}

═══════════════════════════════════════
## 市场情报
═══════════════════════════════════════
{ctx.get('intel_ctx', '(暂无情报)')}

{ctx.get('correlation_ctx', '')}

═══════════════════════════════════════
## 量化评估
═══════════════════════════════════════
{kpi_text if kpi_text else '(暂无KPI数据)'}

{candidates_text}

═══════════════════════════════════════
## 策略库
═══════════════════════════════════════
{ctx.get('strategies_ctx', '(暂无策略)')}
{ctx.get('evolution_ctx', '')}

═══════════════════════════════════════
## 当前持仓与资金
═══════════════════════════════════════
{ctx.get('holdings_ctx', '暂无持仓。')}

可支配资金: ¥{ctx.get('cash', 0):,.2f}

{ctx.get('fee_ctx', '')}

{debate_ctx}

═══════════════════════════════════════
请按以下结构输出完整分析:

### 🔍 A. 情报解读
分析关键情报对市场/标的的影响方向和程度。

### 📊 B. 量化信号评判
基于KPI和信号数据，对每个持仓标的给出客观评分。

### ⚖️ C. 多空裁决
（如有辩论内容）评判看多/看空论据，给出倾向比。

### 🎯 D. 操作指令
对每只标的给出具体操作建议。对新候选标的评估是否值得建仓。

### 📈 E. 风险评估
列出当前主要风险因子。

### 🧬 F. 策略更新
提炼1-3条新策略或更新现有策略。

请在 <autopilot_result> 标签中输出结构化 JSON:
<autopilot_result>
{{
  "confidence_score": 0-100,
  "market_outlook": "bullish|bearish|neutral|cautious",
  "position_recommendations": [
    {{
      "symbol": "标的代码",
      "asset_name": "标的名称",
      "action": "buy|sell|hold|add|reduce",
      "amount": 金额,
      "stop_loss_pct": "止损线%",
      "take_profit_pct": "止盈线%",
      "confidence": 0-100,
      "reason": "核心理由"
    }}
  ],
  "risk_factors": [
    {{"factor": "风险因子", "probability": "high|medium|low", "impact": "high|medium|low"}}
  ],
  "strategy_updates": [
    {{"action": "new|update|retire", "name": "策略名称", "logic": "策略逻辑", "reason": "理由"}}
  ],
  "next_review": "建议下次分析时间 (YYYY-MM-DD HH:MM)"
}}
</autopilot_result>"""


def run_brain_analysis(
    db: Any,
    trigger: str = 'manual',
    news_items: list[dict] | None = None,
    cycle_number: int = 1,
    *,
    client: TradingClient | None = None,
    scan_new_candidates: bool = True,
) -> dict[str, Any]:
    """Execute one full Brain analysis cycle (sync).

    This is the UNIFIED entry point that replaces:
      - trading_decision.trading_recommend()
      - trading_autopilot.cycle.run_autopilot_cycle()

    Returns:
        {cycle_id, analysis_content, structured_result, kpi_evaluations, ...}
    """
    with log_context('brain_analysis', logger=logger):
        cycle_id = f"brain_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        now = datetime.now()

        # ── Phases 1-3: Gather context ──
        ctx = _gather_full_context(
            db, trigger=trigger, news_items=news_items,
            client=client, scan_new_candidates=scan_new_candidates,
        )

        # ── Phase 4: Bull vs Bear Debate ──
        try:
            from lib.trading_autopilot.debate import run_bull_bear_debate
            bull_content, bear_content, debate_ctx = run_bull_bear_debate(
                ctx, max_tokens=4096, temperature=0.4,
            )
            ctx['debate_ctx'] = debate_ctx
            logger.info('[Brain] Bull vs Bear debate completed')
        except Exception as e:
            logger.warning('[Brain] Debate failed, proceeding without: %s', e, exc_info=True)
            ctx['debate_ctx'] = None

        # ── Phase 5: LLM Synthesis ──
        prompt = _build_brain_prompt(ctx, cycle_number)
        messages = [
            {'role': 'system', 'content': '你是一个自主运行的投资超级分析师AI。请用中文回答。'},
            {'role': 'user', 'content': prompt},
        ]

        from lib.llm_dispatch import smart_chat
        content, usage = smart_chat(
            messages=messages, max_tokens=16384, temperature=0.3,
            capability='thinking', timeout=180,
            log_prefix='[Brain]',
        )

        # ── Parse structured result ──
        from lib.trading_autopilot.reasoning import parse_autopilot_result
        structured = parse_autopilot_result(content)

        # ── Phase 6: Store + Strategy Evolution ──
        from lib.trading_autopilot.cycle import _apply_strategy_updates, _store_cycle_result
        _store_cycle_result(
            db, cycle_id, cycle_number, content, structured,
            ctx.get('kpi_evaluations', {}), ctx.get('correlations', []),
        )

        if structured and structured.get('strategy_updates'):
            _apply_strategy_updates(db, structured['strategy_updates'])

        # ── Auto-extract and queue trades ──
        _extract_and_queue_trades_from_result(db, structured, cycle_id)

        return {
            'cycle_id': cycle_id,
            'cycle_number': cycle_number,
            'analysis_content': content,
            'structured_result': structured,
            'kpi_evaluations': ctx.get('kpi_evaluations', {}),
            'correlations': ctx.get('correlations', []),
            'new_candidates': ctx.get('new_candidates', []),
            'alerts': ctx.get('alerts', []),
            'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
            'trigger': trigger,
            'usage': usage,
        }


def build_brain_streaming_body(
    db: Any,
    trigger: str = 'manual',
    news_items: list[dict] | None = None,
    cycle_number: int = 1,
    *,
    client: TradingClient | None = None,
    scan_new_candidates: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build streaming request body for Brain analysis (SSE variant).

    Returns (body, context_dict).
    """
    # ── Phases 1-3 ──
    ctx = _gather_full_context(
        db, trigger=trigger, news_items=news_items,
        client=client, scan_new_candidates=scan_new_candidates,
    )

    # ── Phase 4: Debate ──
    try:
        from lib.trading_autopilot.debate import run_bull_bear_debate
        bull_content, bear_content, debate_ctx = run_bull_bear_debate(
            ctx, max_tokens=4096, temperature=0.4,
        )
        ctx['debate_ctx'] = debate_ctx
        logger.info('[Brain-Stream] Debate completed')
    except Exception as e:
        logger.warning('[Brain-Stream] Debate failed: %s', e, exc_info=True)
        ctx['debate_ctx'] = None

    # ── Build prompt + body ──
    prompt = _build_brain_prompt(ctx, cycle_number)
    messages = [
        {'role': 'system', 'content': '你是一个自主运行的投资超级分析师AI。请用中文回答。'},
        {'role': 'user', 'content': prompt},
    ]

    from lib.llm_client import build_body
    body = build_body(
        _lib.LLM_MODEL, messages,
        max_tokens=16384, temperature=0.3,
        thinking_enabled=True, preset='high',
        stream=True,
    )

    context = {
        'cycle_number': cycle_number,
        'trigger': trigger,
        'kpi_evaluations': ctx.get('kpi_evaluations', {}),
        'correlations': ctx.get('correlations', []),
        'new_candidates': [
            {'code': c.get('code', ''), 'name': c.get('name', ''),
             'score': c.get('total_score', 0), 'rec': c.get('recommendation', '')}
            for c in ctx.get('new_candidates', [])[:5]
        ],
        'alerts': ctx.get('alerts', []),
        'holdings_count': len(ctx.get('holdings', [])),
        'intel_count': ctx.get('intel_count', 0),
        'cash': ctx.get('cash', 0),
        'debate_completed': ctx.get('debate_ctx') is not None,
    }

    return body, context


def _extract_and_queue_trades_from_result(db, structured, cycle_id):
    """Extract position recommendations from structured result and queue as trades."""
    if not structured or not structured.get('position_recommendations'):
        return

    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    batch_id = f"brain_{cycle_id}"
    queued = 0

    for rec in structured['position_recommendations']:
        action = rec.get('action', 'hold')
        if action == 'hold':
            continue  # Don't queue hold actions

        symbol = rec.get('symbol', '')
        if not symbol:
            continue

        try:
            amount = float(rec.get('amount', 0))
        except (ValueError, TypeError) as _e:
            logger.debug('[Brain] Non-numeric amount for symbol %s: %s', symbol, _e)
            amount = 0

        db.execute('''
            INSERT INTO trading_trade_queue
            (batch_id, symbol, asset_name, action, shares, amount,
             price, est_fee, fee_detail, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            batch_id, symbol, rec.get('asset_name', ''),
            action, 0, amount, 0, 0, '',
            f"[Brain] {rec.get('reason', '')}",
            'pending', now,
        ))
        queued += 1

    if queued:
        db.commit()
        logger.info('[Brain] Queued %d trades in batch %s', queued, batch_id)
