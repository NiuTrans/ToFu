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
from typing import Any

import lib as _lib  # module ref for hot-reload
from lib.log import get_logger
from lib.protocols import BodyBuilder, LLMService
from tofu_trading.protocols import TradingDataProvider
from tofu_trading.trading._common import TradingClient
from tofu_trading.trading_autopilot.correlation import build_correlation_context, correlate_intel_items
from tofu_trading.trading_autopilot.kpi import pre_backtest_evaluate
from tofu_trading.trading_autopilot.meta_strategy import (
    build_adaptive_prompt_section,
    detect_market_condition,
    record_combo_deployment,
    select_strategies,
)
from tofu_trading.trading_autopilot.reasoning import build_autopilot_prompt, parse_autopilot_result

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
    uid: int,
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
        uid:           Owning user id. REQUIRED and keyword-only — this runs in
                       a background scheduler thread with no request context,
                       so the caller must resolve it and pass it in. Making it
                       mandatory means a new call site cannot silently inherit
                       another user's portfolio.
        client:        Optional :class:`~tofu_trading.trading._common.TradingClient` instance for
                       dependency injection.  Passed through to concrete
                       ``get_latest_price`` / ``fetch_asset_info`` when no
                       *trading_provider* is given.
        trading_provider: Optional :class:`~tofu_trading.protocols.TradingDataProvider` for
                       dependency injection.  When provided, all trading data
                       calls are dispatched through this protocol instead of
                       importing concrete ``tofu_trading.trading`` functions.  Pass a mock
                       for testing.  ``None`` (default) falls back to the
                       concrete ``tofu_trading.trading`` imports for backward compat.

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
        from tofu_trading.trading import build_intel_context, fetch_asset_info, get_latest_price
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

    # ── Step 3: KPI evaluation for held assets ──
    # NOTE: the former "strategy evolution" step was removed — its only writer
    # (record_decision_outcome) had zero callers, so evaluate_strategy_history
    # could never return anything but 'No lessons yet.'
    evolution_ctx, evolution_items = '', []
    holdings = db.execute('SELECT * FROM trading_holdings WHERE user_id=?', (uid,)).fetchall()
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
    quant_signals = {}
    if kpi_evaluations:
        quant_signals = {
            code: ev.get('quant_signals', {})
            for code, ev in kpi_evaluations.items()
            if 'quant_signals' in ev and 'error' not in ev
        }

    adaptive_decision = None
    try:
        market_condition = detect_market_condition(db, quant_signals=quant_signals)
        selected_strategies = select_strategies(db, market_condition, uid=uid)
        adaptive_strategies_ctx = build_adaptive_prompt_section(
            market_condition, selected_strategies,
        )
    except Exception as e2:
        logger.warning('[Autopilot] Meta-strategy failed: %s', e2, exc_info=True)
        market_condition = None
        selected_strategies = None
        strategies = db.execute(
            "SELECT * FROM trading_strategies WHERE status='active' AND user_id=? "
            "ORDER BY updated_at DESC", (uid,)
        ).fetchall()
        adaptive_strategies_ctx = "\n".join([
            f"- [{dict(s)['type']}] {dict(s)['name']}: {dict(s)['logic']}"
            for s in strategies
            ])

    # NOTE: the former "strategy learning report" step was removed with
    # strategy_learner — it read trading_strategy_deployments, written only by
    # meta_strategy's never-invoked recording path.
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
    uid: int,
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
        client:     Optional :class:`~tofu_trading.trading._common.TradingClient` for trading
                    data HTTP requests.  Passed through to ``_gather_context``.
        trading_provider: Optional :class:`~tofu_trading.protocols.TradingDataProvider` for
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
    # trading_autopilot_cycles.cycle_id is UNIQUE and the old second-resolution
    # format collided on two cycles started in the same second.
    from tofu_trading.run_ids import mint_run_id
    cycle_id = mint_run_id('autopilot', uid=uid)

    # ── Gather all context ──
    ctx = _gather_context(db, news_items, uid=uid, client=client,
                          trading_provider=trading_provider)
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
        _apply_strategy_updates(db, structured['strategy_updates'], uid)

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
    uid: int,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
    body_builder: BodyBuilder | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the request body for a streaming autopilot call.

    Returns ``(body, context_dict)`` where *context_dict* has all the
    gathered context for later storage.

    Args:
        client:        Optional :class:`~tofu_trading.trading._common.TradingClient` for trading
                       data HTTP requests.
        trading_provider: Optional :class:`~tofu_trading.protocols.TradingDataProvider` for
                       trading data access.  Passed through to ``_gather_context``.
        body_builder:  Optional :class:`~lib.protocols.BodyBuilder` for LLM
                       request body construction.  Defaults to
                       ``lib.llm.build_body`` when ``None``.
    """
    if body_builder is None:
        from lib.llm import build_body
        _build_body = build_body
    else:
        _build_body = body_builder

    # ── Gather all context (same helper as sync path) ──
    ctx = _gather_context(db, news_items, uid=uid, client=client,
                          trading_provider=trading_provider)
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

def _store_cycle_result(db, cycle_id, cycle_number, content, structured, uid,
                        kpi_evaluations, correlations):
    """Persist autopilot cycle to database."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # Sanitize confidence_score — LLM may return non-numeric values
    try:
        conf_score = float(structured.get('confidence_score', 0)) if structured else 0
    except (ValueError, TypeError) as _e_audit:
        logger.debug('[cycle] _store_cycle_result caught %s: %s', type(_e_audit).__name__, _e_audit)
        conf_score = 0
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
        conf_score,
        structured.get('market_outlook', 'unknown') if structured else 'unknown',
        'completed', now,
    ))

    # Store position recommendations
    if structured and structured.get('position_recommendations'):
        for rec in structured['position_recommendations']:
            # Sanitize numeric fields — LLM may return strings like "全部持仓"
            try:
                amount = float(rec.get('amount') or 0)
            except (ValueError, TypeError) as _e_audit:
                logger.debug('[cycle] _store_cycle_result caught %s: %s', type(_e_audit).__name__, _e_audit)
                amount = 0
            try:
                confidence = float(rec.get('confidence') or 0)
            except (ValueError, TypeError) as _e_audit:
                logger.debug('[cycle] _store_cycle_result caught %s: %s', type(_e_audit).__name__, _e_audit)
                confidence = 0
            db.execute('''
                INSERT INTO trading_autopilot_recommendations
                (user_id, cycle_id, symbol, asset_name, action, amount,
                 confidence, reason, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                uid, cycle_id, rec.get('symbol', ''), rec.get('asset_name', ''),
                rec.get('action', 'hold'), amount,
                confidence, rec.get('reason', ''),
                'pending', now,
            ))

    db.commit()


def _apply_strategy_updates(db, strategy_updates, uid):
    """Apply strategy updates proposed by the autopilot, for one user."""
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for update in strategy_updates:
        action = update.get('action', '')
        name = update.get('name', '')
        logic = update.get('logic', '')

        if action == 'new' and name and logic:
            # Check for duplicate
            existing = db.execute(
                'SELECT id FROM trading_strategies WHERE name=? AND user_id=?',
                (name, uid)
            ).fetchone()
            if not existing:
                db.execute('''
                    INSERT INTO trading_strategies
                    (user_id, name, type, status, logic, scenario, assets, result,
                     source, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (uid, name, 'autopilot', 'active', logic,
                      update.get('reason', ''), '', '', 'autopilot', now, now))

        elif action == 'update' and name:
            db.execute('''
                UPDATE trading_strategies SET logic=?, updated_at=?, result=?
                WHERE name=? AND status='active' AND user_id=?
            ''', (logic, now,
                  f"[Autopilot更新] {update.get('reason', '')}", name, uid))

        elif action == 'retire' and name:
            db.execute('''
                UPDATE trading_strategies SET status='retired', updated_at=?,
                result=? WHERE name=? AND status='active' AND user_id=?
            ''', (now,
                  f"[Autopilot退役] {update.get('reason', '')}", name, uid))

    db.commit()
    logger.info(
        '[Autopilot] Applied %d strategy updates', len(strategy_updates),
    )
