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

from datetime import datetime
from typing import Any

import lib as _lib  # module ref for hot-reload
from lib.log import get_logger, log_context
from tofu_trading.trading._common import TradingClient

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
    uid: int,
    client: TradingClient | None = None,
    scan_new_candidates: bool = True,
) -> dict[str, Any]:
    """Phase 1 + 2 + 3: Gather all context for brain analysis.

    Delegates core context gathering to ``cycle._gather_context()`` (the single
    source of truth for intel, correlations, KPI, holdings, strategies, adaptive
    engine, and learning context), then adds Brain-specific extras:
      - Candidate screening (Phase 2b)
      - Fee context for holdings
      - Pending alerts from Radar

    Args:
        db: Database connection
        trigger: what triggered this analysis
        news_items: optional live news dicts
        client: optional TradingClient for DI
        scan_new_candidates: whether to run candidate screening (Phase 2b)
    """
    from tofu_trading.trading_autopilot.cycle import _gather_context

    # ── Core context from autopilot (single source of truth) ──
    ctx = _gather_context(db, news_items, uid=uid, client=client)
    ctx['trigger'] = trigger
    ctx['timestamp'] = datetime.now().isoformat()

    # ── Brain-specific Phase 2b: Scan for new candidates (funds + stocks) ──
    held_codes = ctx.get('held_codes', [])
    held_set = set(held_codes)
    ctx['new_candidates'] = []
    ctx['stock_candidates'] = []
    if scan_new_candidates and ctx.get('cash', 0) > 1000:
        # 2b-i: Screen funds/ETFs (existing pipeline)
        try:
            from tofu_trading.trading.screening import screen_assets
            screening_result = screen_assets(
                criteria={
                    'asset_type': 'all',
                    'sort': '3month',
                    'top_n': 10,
                    'min_size': 1.0,
                },
                client=client, db=db, uid=uid,
            )
            candidates = screening_result.get('candidates', [])
            ctx['new_candidates'] = [
                c for c in candidates
                if c.get('code') not in held_set
            ][:8]
            logger.info('[Brain] Found %d fund/ETF candidates (after excluding %d held)',
                        len(ctx['new_candidates']), len(held_codes))
        except Exception as e:
            logger.warning('[Brain] Fund candidate screening failed: %s', e, exc_info=True)

        # 2b-ii: Screen stocks with deep scoring (same depth as funds)
        try:
            from tofu_trading.trading.screening import screen_and_score_stocks
            stock_result = screen_and_score_stocks(
                criteria={
                    'market': 'all',
                    'sort': 'amount',          # sort by trading volume
                    'min_market_cap': 80,      # min 80亿 market cap
                    'min_pe': 5,               # exclude PE < 5 (financials anomalies)
                    'max_pe': 60,              # exclude high PE speculative
                    'limit': 30,               # over-fetch for filtering
                    'top_n': 12,               # deeply score top 12
                },
                client=client, db=db,
                uid=uid,
            )
            scored_stocks = stock_result.get('candidates', [])
            ctx['stock_candidates'] = [
                c for c in scored_stocks
                if c.get('code') not in held_set
            ][:10]
            logger.info('[Brain] Found %d deeply-scored stock candidates (after excluding %d held)',
                        len(ctx['stock_candidates']), len(held_codes))
        except Exception as e:
            logger.warning('[Brain] Stock candidate screening failed: %s', e, exc_info=True)

    # ── Brain-specific: Fee context for holdings ──
    fee_ctx = ""
    try:
        from tofu_trading.trading import calc_sell_fee, fetch_trading_fees
        from tofu_trading.trading._common import classify_asset_code
        for h in ctx.get('holdings', []):
            symbol = h['symbol']
            fees = fetch_trading_fees(symbol, client=client)
            asset_type = classify_asset_code(symbol)
            if asset_type in ('stock', 'etf'):
                fee_ctx += (
                    f"- {symbol} [{asset_type.upper()}]: {fees.get('summary', '')}\n"
                )
            else:
                sell_info = calc_sell_fee(h, client=client)
                fee_ctx += (
                    f"- {symbol} [基金]: 申购费{fees['buy_fee_rate']*100:.2f}% | "
                    f"管理费{fees.get('management_fee', 0)*100:.2f}%/年 | "
                    f"当前赎回费{sell_info['fee_rate']*100:.2f}%"
                    f"（持有{sell_info['holding_days']}天）\n"
                )
    except Exception as e:
        logger.debug('[Brain] Fee context failed: %s', e, exc_info=True)
    ctx['fee_ctx'] = fee_ctx

    # ── Brain-specific: Pending alerts from Radar ──
    try:
        from tofu_trading.trading.radar.alert import get_pending_alerts
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

    # Build candidates text (funds/ETFs + stocks)
    candidates_text = ""
    cand_lines = []

    new_candidates = ctx.get('new_candidates', [])
    if new_candidates:
        cand_lines.append("## 新候选标的 — 基金/ETF (量化筛选结果)")
        for c in new_candidates[:8]:
            cand_lines.append(
                f"- {c.get('code', '')} {c.get('name', '')}: "
                f"综合评分 {c.get('total_score', 0):.1f}, "
                f"推荐: {c.get('recommendation', 'N/A')}, "
                f"3月收益: {c.get('returns', {}).get('3m', 'N/A')}%"
            )

    stock_candidates = ctx.get('stock_candidates', [])
    if stock_candidates:
        cand_lines.append("\n## 新候选标的 — A股个股 (量化筛选+深度评分结果)")
        for s in stock_candidates[:10]:
            # Deeply-scored stock candidates have full evaluation data
            fundamentals = s.get('stock_fundamentals', {})
            pe = fundamentals.get('pe', s.get('pe', 0))
            pb = fundamentals.get('pb', s.get('pb', 0))
            mv = fundamentals.get('market_cap_yi', s.get('total_mv_yi', 0))
            turnover = fundamentals.get('turnover', s.get('turnover', 0))
            price = fundamentals.get('price', s.get('price', 0))

            pe_str = f"PE:{pe:.1f}" if pe else 'PE:N/A'
            pb_str = f"PB:{pb:.1f}" if pb else 'PB:N/A'
            mv_str = f"市值:{mv:.0f}亿" if mv else ''

            # Include quantitative scores if available (from score_stock_candidate)
            score_str = ''
            if s.get('total_score') is not None:
                score_str = f"综合评分 {s['total_score']:.1f}, 推荐: {s.get('recommendation', 'N/A')}, "
            returns = s.get('returns', {})
            ret_str = ''
            if returns.get('3m') is not None:
                ret_str = f"3月收益: {returns['3m']}%, "
            elif returns.get('1m') is not None:
                ret_str = f"1月收益: {returns['1m']}%, "
            risk = s.get('risk_metrics', {})
            risk_str = ''
            if risk.get('sharpe') is not None:
                risk_str = f"夏普: {risk['sharpe']}, 最大回撤: {risk.get('max_drawdown', 'N/A')}%, "

            cand_lines.append(
                f"- {s.get('code', '')} {s.get('name', '')}: "
                f"{score_str}"
                f"价格 ¥{price:.2f}, "
                f"{ret_str}"
                f"{risk_str}"
                f"{pe_str}, {pb_str}, {mv_str}, "
                f"换手率 {turnover:.1f}%"
            )

    if cand_lines:
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

    return f"""你是一位全球顶级投资超级分析师 (Autonomous Super-Analyst)。
第 {cycle_number} 轮分析周期 | 触发方式: {trigger_label}

你的投资范围涵盖所有A股可交易品种:
- **A股个股**: 沪深主板(60xxxx/000xxx/001xxx)、中小板(002xxx)、创业板(300xxx)、科创板(688xxx)
- **ETF**: 宽基指数、行业、债券、跨境(51xxxx/15xxxx/16xxxx)
- **开放式基金**: 股票型、混合型、债券型、QDII

你应积极考虑A股个股，而不仅仅是基金/ETF。个股具有更高的収益潜力和更精细的行业/公司暴露。
根据市场环境灵活配置个股和基金/ETF的比例。

你的核心纪律:
1. 当量化信号与情报矛盾时，优先相信量化信号
2. RSI>75时不追高，RSI<25时关注抄底机会
3. 综合信号得分<-30时启动防御模式
4. 每笔建议必须包含止损位和目标收益率
5. 考虑T+1交易规则和交易费用对操作时机的影响
6. 个股分析必须关注PE/PB估值、市值、行业竞争格局、换手率、股息率等基本面指标
7. 股票佣金约万2.5（单笔最低5元），卖出有0.05%印花税；基金关注申购/赎回费率
8. 推荐个股时应说明行业逻辑、公司护城河、估值安全边际
9. 个股仓位建议不超过总资产的15%（分散风险），基金/ETF可适当放宽到25%

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
对每只标的给出具体操作建议。对新候选标的（含个股和基金/ETF）评估是否值得建仓。
个股建仓建议必须包含: 行业逻辑、估值安全边际(PE/PB分析)、仓位建议。

### 📈 E. 风险评估
列出当前主要风险因子（含个股特有风险如行业政策、业绩地雷、流动性等）。

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
      "asset_type": "stock|etf|fund",
      "action": "buy|sell|hold|add|reduce",
      "amount": 金额,
      "stop_loss_pct": "止损线%",
      "take_profit_pct": "止盈线%",
      "confidence": 0-100,
      "reason": "核心理由（个股需包含估值分析+行业逻辑）"
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
    uid: int,
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
        from tofu_trading.run_ids import mint_run_id
        cycle_id = mint_run_id('brain', uid=uid)
        now = datetime.now()

        # ── Phases 1-3: Gather context ──
        ctx = _gather_full_context(
            db, trigger=trigger, news_items=news_items, uid=uid,
            client=client, scan_new_candidates=scan_new_candidates,
        )

        # NOTE: the bull-vs-bear debate phase was removed with debate.py — two
        # prompt templates whose output was concatenated into the prompt and
        # never persisted, at the cost of an extra LLM round-trip per cycle.
        ctx['debate_ctx'] = None

        # ── Phase 5: LLM Synthesis ──
        prompt = _build_brain_prompt(ctx, cycle_number)
        messages = [
            {'role': 'system', 'content': '你是一个自主运行的投资超级分析师AI，精通A股个股、ETF和基金分析。积极推荐个股，不要只关注基金/ETF。请用中文回答。'},
            {'role': 'user', 'content': prompt},
        ]

        from lib.llm_dispatch import smart_chat
        content, usage = smart_chat(
            messages=messages, max_tokens=16384, temperature=0.3,
            capability='thinking', timeout=180,
            log_prefix='[Brain]',
        )

        # ── Parse structured result ──
        from tofu_trading.trading_autopilot.reasoning import parse_autopilot_result
        structured = parse_autopilot_result(content)

        # ── Phase 6: Store + Strategy Evolution ──
        from tofu_trading.trading_autopilot.cycle import _apply_strategy_updates, _store_cycle_result
        _store_cycle_result(
            db, cycle_id, cycle_number, content, structured, uid,
            ctx.get('kpi_evaluations', {}), ctx.get('correlations', []),
        )

        if structured and structured.get('strategy_updates'):
            _apply_strategy_updates(db, structured['strategy_updates'], uid)

        # ── Auto-extract and queue trades ──
        _extract_and_queue_trades_from_result(db, structured, cycle_id, uid)

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
    uid: int,
    client: TradingClient | None = None,
    scan_new_candidates: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build streaming request body for Brain analysis (SSE variant).

    Returns (body, context_dict).
    """
    # ── Phases 1-3 ──
    ctx = _gather_full_context(
        db, trigger=trigger, news_items=news_items, uid=uid,
        client=client, scan_new_candidates=scan_new_candidates,
    )

    # NOTE: debate phase removed with debate.py (see the sync path above).
    ctx['debate_ctx'] = None

    # ── Build prompt + body ──
    prompt = _build_brain_prompt(ctx, cycle_number)
    messages = [
        {'role': 'system', 'content': '你是一个自主运行的投资超级分析师AI，精通A股个股、ETF和基金分析。积极推荐个股，不要只关注基金/ETF。请用中文回答。'},
        {'role': 'user', 'content': prompt},
    ]

    from lib.llm import build_body
    body = build_body(
        _lib.LLM_MODEL, messages,
        max_tokens=16384, temperature=0.3,
        thinking_enabled=True, preset='high',
        stream=True,
    )

    # Build unified candidate list (funds + stocks) for frontend
    fund_cands = [
        {'code': c.get('code', ''), 'name': c.get('name', ''),
         'score': c.get('total_score', 0), 'rec': c.get('recommendation', ''),
         'asset_type': 'fund'}
        for c in ctx.get('new_candidates', [])[:5]
    ]
    stock_cands = [
        {'code': c.get('code', ''), 'name': c.get('name', ''),
         'score': c.get('total_score', 0), 'rec': c.get('recommendation', ''),
         'asset_type': 'stock',
         'pe': c.get('stock_fundamentals', {}).get('pe', 0),
         'pb': c.get('stock_fundamentals', {}).get('pb', 0),
         'market_cap_yi': c.get('stock_fundamentals', {}).get('market_cap_yi', 0)}
        for c in ctx.get('stock_candidates', [])[:5]
    ]

    context = {
        'cycle_number': cycle_number,
        'trigger': trigger,
        'kpi_evaluations': ctx.get('kpi_evaluations', {}),
        'correlations': ctx.get('correlations', []),
        'new_candidates': fund_cands + stock_cands,
        'alerts': ctx.get('alerts', []),
        'holdings_count': len(ctx.get('holdings', [])),
        'intel_count': ctx.get('intel_count', 0),
        'cash': ctx.get('cash', 0),
        'debate_completed': ctx.get('debate_ctx') is not None,
    }

    return body, context


def _extract_and_queue_trades_from_result(db, structured, cycle_id, uid):
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
            (user_id, batch_id, symbol, asset_name, action, shares, amount,
             price, est_fee, fee_detail, reason, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            uid, batch_id, symbol, rec.get('asset_name', ''),
            action, 0, amount, 0, 0, '',
            f"[Brain] {rec.get('reason', '')}",
            'pending', now,
        ))
        queued += 1

    if queued:
        db.commit()
        logger.info('[Brain] Queued %d trades in batch %s', queued, batch_id)
