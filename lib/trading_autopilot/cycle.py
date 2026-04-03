"""lib/trading_autopilot/cycle.py — Autopilot Cycle Runner & Streaming.

Orchestrates a full autopilot analysis cycle: gathers context,
calls the LLM, parses results, stores them, and applies strategy
updates.  Also provides a streaming variant for SSE frontends.

Design: the duplicated context-gathering logic is extracted into
``_gather_context()`` — a single helper used by both ``run_autopilot_cycle``
and ``build_autopilot_streaming_body``.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

import lib as _lib  # module ref for hot-reload
from lib.log import get_logger
from lib.protocols import BodyBuilder, LLMService, TradingDataProvider
from lib.trading._common import TradingClient
from lib.trading_autopilot.adaptive_decision_engine import (
    AdaptiveDecisionEngine,
    build_adaptive_decision_prompt,
)
from lib.trading_autopilot.correlation import build_correlation_context, correlate_intel_items
from lib.trading_autopilot.debate import run_bull_bear_debate
from lib.trading_autopilot.kpi import pre_backtest_evaluate
from lib.trading_autopilot.meta_strategy import (
    build_adaptive_prompt_section,
    detect_market_condition,
    record_combo_deployment,
    select_strategies,
)
from lib.trading_autopilot.reasoning import build_autopilot_prompt, parse_autopilot_result
from lib.trading_autopilot.strategy_evolution import evolve_strategies
from lib.trading_autopilot.strategy_learner import build_learning_prompt_section

logger = get_logger(__name__)

__all__ = [
    'run_autopilot_cycle',
    'build_autopilot_streaming_body',
    '_store_cycle_result',
    '_apply_strategy_updates',
]


# ═══════════════════════════════════════════════════════════
#  Context Gathering (shared by cycle runner + streaming)
# ═══════════════════════════════════════════════════════════

def _gather_context(
    db: Any,
    news_items: list[dict[str, Any]] | None = None,
    *,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
) -> dict[str, Any]:
    """Gather all context needed for an autopilot analysis.

    This is the single source of truth for context assembly — used by both
    ``run_autopilot_cycle()`` (sync) and ``build_autopilot_streaming_body()``
    (streaming).  Deduplicating this logic eliminates the drift-prone copy-paste
    that previously existed between the two call sites.

    Args:
        db:            Database connection.
        news_items:    Optional list of live news dicts with 'title' / 'snippet'.
        client:        Optional :class:`~lib.trading._common.TradingClient` instance for
                       dependency injection.  Passed through to concrete
                       ``get_latest_price`` / ``fetch_asset_info`` when no
                       *trading_provider* is given.
        trading_provider: Optional :class:`~lib.protocols.TradingDataProvider` for
                       dependency injection.  When provided, all trading data
                       calls are dispatched through this protocol instead of
                       importing concrete ``lib.trading`` functions.  Pass a mock
                       for testing.  ``None`` (default) falls back to the
                       concrete ``lib.trading`` imports for backward compat.

    Returns:
        dict with keys:
            intel_ctx, intel_count, correlations, correlation_ctx,
            evolution_ctx, evolution_items, kpi_evaluations,
            holdings_ctx, holdings, held_codes, cash, strategies_ctx
    """
    # ── Resolve trading data functions via protocol or concrete imports ──
    if trading_provider is not None:
        _get_latest_price = trading_provider.get_latest_price
        _fetch_asset_info = trading_provider.fetch_asset_info
        _build_intel_context = trading_provider.build_intel_context
    else:
        from lib.trading import build_intel_context, fetch_asset_info, get_latest_price
        # Wrap concrete functions so the call-sites below are uniform
        # (concrete functions take client= kwarg; protocol methods do not).
        _build_intel_context = build_intel_context
        _get_latest_price = lambda code: get_latest_price(code, client=client)  # noqa: E731
        _fetch_asset_info = lambda code: fetch_asset_info(code, client=client)  # noqa: E731

    # ── Step 1: Intelligence context (time-layered) ──
    intel_ctx, intel_count = _build_intel_context(db)
    if news_items:
        news_lines = ["### 实时新闻"]
        for n in news_items[:15]:
            news_lines.append(f"- [{n.get('title', '')}] {n.get('snippet', '')}")
        intel_ctx = "\n".join(news_lines) + "\n\n" + intel_ctx

    # ── Step 2: Correlations ──
    correlations = correlate_intel_items(db)
    correlation_ctx = build_correlation_context(correlations)

    # ── Step 3: Strategy evolution ──
    evolution_ctx, evolution_items = evolve_strategies(db)

    # ── Step 4: KPI evaluation for held assets ──
    holdings = db.execute('SELECT * FROM trading_holdings').fetchall()
    holdings = [dict(h) for h in holdings]
    held_codes = [h['symbol'] for h in holdings]

    kpi_evaluations = {}
    if held_codes:
        kpi_evaluations = pre_backtest_evaluate(db, held_codes, lookback_days=90)

    # ── Step 5: Build human-readable holdings context ──
    holdings_ctx = ""
    for h in holdings:
        try:
            nav_val, nav_date = _get_latest_price(h['symbol'])
            info = _fetch_asset_info(h['symbol'])
            name = info.get('name', '') if info else ''
            cost = h.get('buy_price', 0)
            pnl = ((nav_val - cost) / cost * 100) if nav_val and cost else 0
            holdings_ctx += (
                f"- {h['symbol']} {name}: {h['shares']}份, "
                f"成本¥{cost}, 现价¥{nav_val or 'N/A'}, 盈亏{pnl:+.2f}%\n"
            )
        except Exception as e:
            logger.debug(
                '[Autopilot] NAV fetch degraded for %s, using cost-only: %s',
                h['symbol'], e, exc_info=True,
            )
            holdings_ctx += (
                f"- {h['symbol']}: {h['shares']}份, "
                f"成本¥{h.get('buy_price', 0)}\n"
            )

    # ── Step 6: Available cash ──
    cfg = db.execute(
        "SELECT value FROM trading_config WHERE key='available_cash'"
    ).fetchone()
    cash = float(cfg['value']) if cfg else 0

    # ── Step 7: Unified Adaptive Decision Engine ──
    # Uses the full AdaptiveDecisionEngine which integrates:
    #   - Market condition detection (quant + intel)
    #   - Strategy registry with learning data
    #   - Signal fusion (buy/sell/hold with risk veto)
    #   - Compatibility checks from strategy_learner
    #   - Failure-based restrictions from backtest_learner
    quant_signals = {}
    if kpi_evaluations:
        quant_signals = {
            code: ev.get('quant_signals', {})
            for code, ev in kpi_evaluations.items()
            if 'quant_signals' in ev and 'error' not in ev
        }

    adaptive_decision = None
    try:
        ade = AdaptiveDecisionEngine(db)
        adaptive_decision = ade.make_decision(
            quant_signals=quant_signals,
            max_strategies=8,
        )
        # Build the enriched prompt with fused signal
        adaptive_strategies_ctx = build_adaptive_decision_prompt(adaptive_decision)

        # Extract market_condition and selected_strategies for downstream use
        mc_dict = adaptive_decision.get('market_condition', {})
        from lib.trading_autopilot.meta_strategy import MarketCondition
        market_condition = MarketCondition(
            regime=mc_dict.get('regime', 'unknown'),
            volatility=mc_dict.get('volatility', 'normal'),
            trend_strength=mc_dict.get('trend_strength', 0),
            sentiment_score=mc_dict.get('sentiment_score', 0),
            policy_signal=mc_dict.get('policy_signal', 0),
            risk_signal=mc_dict.get('risk_signal', 0),
            opportunity_signal=mc_dict.get('opportunity_signal', 0),
            intel_velocity=mc_dict.get('intel_velocity', 0),
            as_of=mc_dict.get('as_of', ''),
        )
        # Convert selected profiles back to strategy dicts for recording
        selected_strategies = []
        for s in adaptive_decision.get('selected_strategies', []):
            p = s.get('profile', {})
            selected_strategies.append({
                'id': p.get('strategy_id', 0),
                'name': p.get('name', ''),
                'type': p.get('type', 'observation'),
                'logic': p.get('logic', ''),
                'selection_score': s.get('selection_score', 0),
            })

        logger.info(
            '[Autopilot] AdaptiveDecisionEngine: regime=%s, direction=%s, '
            'confidence=%d%%, %d strategies selected',
            mc_dict.get('regime'), adaptive_decision.get('fused_signal', {}).get('direction'),
            adaptive_decision.get('fused_signal', {}).get('confidence', 0),
            len(selected_strategies),
        )
    except Exception as e:
        logger.warning(
            '[Autopilot] AdaptiveDecisionEngine failed, falling back to meta_strategy: %s',
            e, exc_info=True,
        )
        # Fallback: use the simpler meta_strategy path
        try:
            market_condition = detect_market_condition(db, quant_signals=quant_signals)
            selected_strategies = select_strategies(db, market_condition)
            adaptive_strategies_ctx = build_adaptive_prompt_section(
                market_condition, selected_strategies,
            )
        except Exception as e2:
            logger.warning('[Autopilot] Meta-strategy also failed: %s', e2, exc_info=True)
            market_condition = None
            selected_strategies = None
            strategies = db.execute(
                "SELECT * FROM trading_strategies WHERE status='active' "
                "ORDER BY updated_at DESC"
            ).fetchall()
            adaptive_strategies_ctx = "\n".join([
                f"- [{dict(s)['type']}] {dict(s)['name']}: {dict(s)['logic']}"
                for s in strategies
            ])

    # ── Step 8: Strategy learning report ──
    try:
        learning_ctx = build_learning_prompt_section(db)
    except Exception as e:
        logger.warning('[Autopilot] Strategy learning report failed: %s', e, exc_info=True)
        learning_ctx = ''

    return {
        'intel_ctx': intel_ctx,
        'intel_count': intel_count,
        'correlations': correlations,
        'correlation_ctx': correlation_ctx,
        'evolution_ctx': evolution_ctx,
        'evolution_items': evolution_items,
        'kpi_evaluations': kpi_evaluations,
        'holdings': holdings,
        'holdings_ctx': holdings_ctx,
        'held_codes': held_codes,
        'cash': cash,
        'strategies_ctx': adaptive_strategies_ctx,
        'learning_ctx': learning_ctx,
        'market_condition': market_condition,
        'selected_strategies': selected_strategies,
        'adaptive_decision': adaptive_decision,
    }


# ═══════════════════════════════════════════════════════════
#  Sync Cycle Runner
# ═══════════════════════════════════════════════════════════

def run_autopilot_cycle(
    db: Any,
    news_items: list[dict[str, Any]] | None = None,
    cycle_number: int = 1,
    *,
    llm: LLMService | None = None,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
) -> dict[str, Any]:
    """Execute one full autopilot analysis cycle.

    Steps:
      1. Gather intelligence context           (_gather_context)
      2. Build mega-prompt & call LLM
      3. Parse & store results
      4. Auto-update strategies

    Args:
        db:         Database connection.
        news_items: Optional live news dicts.
        cycle_number: Cycle sequence number.
        llm:        Optional ``LLMService`` for LLM calls.  Defaults to
                    ``lib.llm_dispatch.smart_chat`` (production singleton).
                    Pass a mock/stub for testing.
        client:     Optional :class:`~lib.trading._common.TradingClient` for trading
                    data HTTP requests.  Passed through to ``_gather_context``.
        trading_provider: Optional :class:`~lib.protocols.TradingDataProvider` for
                    trading data access.  Passed through to ``_gather_context``.

    Returns:
      { cycle_id, analysis_content, structured_result, kpi_evaluations, timestamp }
    """
    if llm is None:
        from lib.llm_dispatch import smart_chat
        _chat_fn = smart_chat
    else:
        _chat_fn = llm.chat
    now = datetime.now()
    cycle_id = f"autopilot_{now.strftime('%Y%m%d_%H%M%S')}"

    # ── Gather all context ──
    ctx = _gather_context(db, news_items, client=client, trading_provider=trading_provider)

    # ── Run Bull vs Bear Debate (parallel) ──
    try:
        bull_content, bear_content, debate_ctx = run_bull_bear_debate(
            ctx, llm=llm, max_tokens=4096, temperature=0.4,
        )
        ctx['debate_ctx'] = debate_ctx
        logger.info('[Autopilot] Bull vs Bear debate completed successfully')
    except Exception as e:
        logger.warning('[Autopilot] Debate failed, proceeding without: %s', e, exc_info=True)
        ctx['debate_ctx'] = None

    # ── Record strategy combo deployment (for learner feedback loop) ──
    if ctx.get('market_condition') and ctx.get('selected_strategies'):
        try:
            record_combo_deployment(
                db, cycle_id, ctx['market_condition'], ctx['selected_strategies'],
            )
        except Exception as e:
            logger.warning('[Autopilot] Failed to record combo deployment: %s', e, exc_info=True)

    # ── Build mega-prompt & call LLM ──
    prompt = build_autopilot_prompt(
        ctx['holdings_ctx'], ctx['cash'], ctx['strategies_ctx'],
        ctx['intel_ctx'], ctx['correlation_ctx'], ctx['evolution_ctx'],
        ctx['kpi_evaluations'], cycle_number, debate_ctx=ctx['debate_ctx'],
        learning_ctx=ctx.get('learning_ctx', ''),
    )

    messages = [
        {'role': 'system', 'content': '你是一个自主运行的投资超级分析师AI。请用中文回答，分析要深入、专业、有数据支撑。'},
        {'role': 'user', 'content': prompt},
    ]

    content, usage = _chat_fn(
        messages=messages,
        max_tokens=16384, temperature=0.3,
        capability='thinking',
        timeout=180, log_prefix='[Autopilot]',
    )

    # ── Parse structured result ──
    structured = parse_autopilot_result(content)

    # ── Store ──
    _store_cycle_result(
        db, cycle_id, cycle_number, content, structured,
        ctx['kpi_evaluations'], ctx['correlations'],
    )

    # ── Auto-update strategies ──
    if structured and structured.get('strategy_updates'):
        _apply_strategy_updates(db, structured['strategy_updates'])

    return {
        'cycle_id': cycle_id,
        'cycle_number': cycle_number,
        'analysis_content': content,
        'structured_result': structured,
        'kpi_evaluations': ctx['kpi_evaluations'],
        'correlations': ctx['correlations'],
        'timestamp': now.strftime('%Y-%m-%d %H:%M:%S'),
        'usage': usage,
    }


# ═══════════════════════════════════════════════════════════
#  Streaming Variant
# ═══════════════════════════════════════════════════════════

def build_autopilot_streaming_body(
    db: Any,
    news_items: list[dict[str, Any]] | None = None,
    cycle_number: int = 1,
    *,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
    body_builder: BodyBuilder | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the request body for a streaming autopilot call.

    Returns ``(body, context_dict)`` where *context_dict* has all the
    gathered context for later storage.

    Args:
        client:        Optional :class:`~lib.trading._common.TradingClient` for trading
                       data HTTP requests.
        trading_provider: Optional :class:`~lib.protocols.TradingDataProvider` for
                       trading data access.  Passed through to ``_gather_context``.
        body_builder:  Optional :class:`~lib.protocols.BodyBuilder` for LLM
                       request body construction.  Defaults to
                       ``lib.llm_client.build_body`` when ``None``.
    """
    if body_builder is None:
        from lib.llm_client import build_body
        _build_body = build_body
    else:
        _build_body = body_builder

    # ── Gather all context (same helper as sync path) ──
    ctx = _gather_context(db, news_items, client=client, trading_provider=trading_provider)

    # ── Run Bull vs Bear Debate (parallel) ──
    try:
        bull_content, bear_content, debate_ctx = run_bull_bear_debate(
            ctx, max_tokens=4096, temperature=0.4,
        )
        ctx['debate_ctx'] = debate_ctx
        logger.info('[Autopilot-Stream] Bull vs Bear debate completed')
    except Exception as e:
        logger.warning('[Autopilot-Stream] Debate failed, proceeding without: %s', e, exc_info=True)
        ctx['debate_ctx'] = None

    prompt = build_autopilot_prompt(
        ctx['holdings_ctx'], ctx['cash'], ctx['strategies_ctx'],
        ctx['intel_ctx'], ctx['correlation_ctx'], ctx['evolution_ctx'],
        ctx['kpi_evaluations'], cycle_number, debate_ctx=ctx['debate_ctx'],
        learning_ctx=ctx.get('learning_ctx', ''),
    )

    messages = [
        {'role': 'system', 'content': '你是一个自主运行的投资超级分析师AI。请用中文回答，分析要深入、专业、有数据支撑。'},
        {'role': 'user', 'content': prompt},
    ]

    body = _build_body(
        _lib.LLM_MODEL, messages,
        max_tokens=16384, temperature=0.3,
        thinking_enabled=True, preset='high',
        stream=True,
    )

    context = {
        'cycle_number': cycle_number,
        'kpi_evaluations': ctx['kpi_evaluations'],
        'correlations': ctx['correlations'],
        'evolution_items': ctx['evolution_items'],
        'holdings_count': len(ctx['holdings']),
        'intel_count': ctx['intel_count'],
        'cash': ctx['cash'],
        'debate_completed': ctx.get('debate_ctx') is not None,
        'meta_strategy_active': ctx.get('market_condition') is not None,
        'selected_strategy_count': len(ctx.get('selected_strategies') or []),
    }

    return body, context


# ═══════════════════════════════════════════════════════════
#  Storage & Strategy Application
# ═══════════════════════════════════════════════════════════

def _store_cycle_result(db, cycle_id, cycle_number, content, structured,
                        kpi_evaluations, correlations):
    """Persist autopilot cycle to database."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute('''
        INSERT INTO trading_autopilot_cycles
        (cycle_id, cycle_number, analysis_content, structured_result,
         kpi_evaluations, correlations, confidence_score, market_outlook,
         status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        cycle_id, cycle_number, content,
        json.dumps(structured, ensure_ascii=False) if structured else '{}',
        json.dumps(kpi_evaluations, ensure_ascii=False),
        json.dumps([c for c in correlations], ensure_ascii=False),
        structured.get('confidence_score', 0) if structured else 0,
        structured.get('market_outlook', 'unknown') if structured else 'unknown',
        'completed', now,
    ))

    # Store position recommendations
    if structured and structured.get('position_recommendations'):
        for rec in structured['position_recommendations']:
            db.execute('''
                INSERT INTO trading_autopilot_recommendations
                (cycle_id, symbol, asset_name, action, amount,
                 confidence, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                cycle_id, rec.get('symbol', ''), rec.get('asset_name', ''),
                rec.get('action', 'hold'), rec.get('amount') or 0,
                rec.get('confidence') or 0, rec.get('reason', ''),
                'pending', now,
            ))

    db.commit()


def _apply_strategy_updates(db, strategy_updates):
    """Apply strategy updates proposed by the autopilot."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for update in strategy_updates:
        action = update.get('action', '')
        name = update.get('name', '')
        logic = update.get('logic', '')

        if action == 'new' and name and logic:
            # Check for duplicate
            existing = db.execute(
                'SELECT id FROM trading_strategies WHERE name=?', (name,)
            ).fetchone()
            if not existing:
                db.execute('''
                    INSERT INTO trading_strategies
                    (name, type, status, logic, scenario, assets, result,
                     source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (name, 'autopilot', 'active', logic,
                      update.get('reason', ''), '', '', 'autopilot', now, now))

        elif action == 'update' and name:
            db.execute('''
                UPDATE trading_strategies SET logic=?, updated_at=?, result=?
                WHERE name=? AND status='active'
            ''', (logic, now,
                  f"[Autopilot更新] {update.get('reason', '')}", name))

        elif action == 'retire' and name:
            db.execute('''
                UPDATE trading_strategies SET status='retired', updated_at=?,
                result=? WHERE name=? AND status='active'
            ''', (now,
                  f"[Autopilot退役] {update.get('reason', '')}", name))

    db.commit()
    logger.info(
        '[Autopilot] Applied %d strategy updates', len(strategy_updates),
    )
