"""lib/trading_autopilot/outcome.py — Recommendation Outcome Tracker.

Checks past recommendations against actual asset performance to
evaluate whether the autopilot's advice was correct, feeding
back into strategy evolution.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from lib.log import get_logger
from lib.protocols import TradingDataProvider
from lib.trading import fetch_price_history, get_latest_price
from lib.trading._common import TradingClient

logger = get_logger(__name__)

__all__ = [
    'track_recommendation_outcomes',
]


def track_recommendation_outcomes(
    db: Any,
    days_after: int = 7,
    *,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
) -> list[dict[str, Any]]:
    """Check recommendations from N days ago and evaluate their outcomes.

    For each past recommendation, compare the asset's price at recommendation
    time vs now to determine if the advice was good.

    Args:
        db:             Database connection.
        days_after:     Number of days to wait before evaluating.
        client:         Optional :class:`~lib.trading._common.TradingClient` instance
                        for dependency injection.  Passed through to concrete
                        ``get_latest_price`` / ``fetch_price_history`` when no
                        *trading_provider* is given.
        trading_provider:  Optional :class:`~lib.protocols.TradingDataProvider` for
                        dependency injection.  When provided, all trading data
                        calls are dispatched through this protocol.  Pass a
                        mock/stub for testing.  ``None`` (default) falls back
                        to the concrete ``lib.trading`` imports.

    Returns:
        List of outcome dicts with keys: symbol, action, actual_return, outcome.
    """
    # ── Resolve trading data functions via protocol or concrete imports ──
    if trading_provider is not None:
        _get_latest_price = trading_provider.get_latest_price
        _fetch_nav_history = trading_provider.fetch_price_history
    else:
        _get_latest_price = lambda code: get_latest_price(code, client=client)  # noqa: E731
        _fetch_nav_history = lambda code, start, end: fetch_price_history(  # noqa: E731
            code, start, end, client=client,
        )

    cutoff = (datetime.now() - timedelta(days=days_after)).strftime('%Y-%m-%d %H:%M:%S')
    old_cutoff = (datetime.now() - timedelta(days=days_after + 30)).strftime('%Y-%m-%d %H:%M:%S')

    recs = db.execute('''
        SELECT * FROM trading_autopilot_recommendations
        WHERE status='pending' AND created_at <= ? AND created_at >= ?
        ORDER BY created_at ASC
    ''', (cutoff, old_cutoff)).fetchall()

    outcomes: list[dict[str, Any]] = []
    for rec in recs:
        rec = dict(rec)
        code = rec['symbol']
        if not code:
            continue

        try:
            nav_now, _ = _get_latest_price(code)
            # Get price at recommendation time
            rec_date = rec['created_at'][:10]
            nav_history = _fetch_nav_history(
                code, rec_date, datetime.now().strftime('%Y-%m-%d'),
            )
            if nav_history and len(nav_history) >= 2:
                nav_then = nav_history[0]['nav']
                actual_return = ((nav_now - nav_then) / nav_then * 100) if nav_then > 0 else 0

                action = rec['action']
                # Determine if the recommendation was correct
                if action in ('buy', 'add') and actual_return > 0:
                    outcome = 'correct'
                elif action in ('sell', 'reduce') and actual_return < 0:
                    outcome = 'correct'
                elif action == 'hold' and abs(actual_return) < 3:
                    outcome = 'correct'
                else:
                    outcome = 'incorrect'

                db.execute('''
                    UPDATE trading_autopilot_recommendations
                    SET status=?, actual_return=?, evaluated_at=?
                    WHERE id=?
                ''', (outcome, actual_return,
                      datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                      rec['id']))

                outcomes.append({
                    'symbol': code,
                    'action': action,
                    'actual_return': round(actual_return, 2),
                    'outcome': outcome,
                })
        except Exception as e:
            logger.error('Outcome tracking error for %s: %s', code, e, exc_info=True)
            continue

    if outcomes:
        db.commit()

    return outcomes
