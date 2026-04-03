"""lib/trading_autopilot/scheduler.py — Autopilot Scheduler State.

Manages the autopilot enable/disable toggle and periodic scheduler
tick that triggers new analysis cycles from a background thread.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from lib.log import get_logger
from lib.protocols import LLMService, TradingDataProvider
from lib.trading._common import TradingClient
from lib.trading_autopilot._constants import AUTOPILOT_CYCLE_MINUTES

logger = get_logger(__name__)

__all__ = [
    'get_autopilot_state',
    'set_autopilot_enabled',
    'autopilot_scheduler_tick',
]

_autopilot_state = {
    'enabled': False,
    'running': False,
    'last_cycle': None,
    'last_cycle_id': None,
    'cycle_count': 0,
    'next_run': None,
    'error': None,
}


def get_autopilot_state():
    return dict(_autopilot_state)


def set_autopilot_enabled(enabled):
    _autopilot_state['enabled'] = enabled
    if not enabled:
        _autopilot_state['next_run'] = None


def autopilot_scheduler_tick(
    db_path: str,
    *,
    llm: LLMService | None = None,
    client: TradingClient | None = None,
    trading_provider: TradingDataProvider | None = None,
) -> None:
    """Called periodically (e.g., every minute) from a background thread.

    Checks if it's time to run a new cycle.

    Args:
        db_path:        Legacy parameter (unused in PostgreSQL era).
        llm:            Optional :class:`~lib.protocols.LLMService` for LLM calls.
                        Forwarded to ``run_autopilot_cycle``.  ``None`` (default)
                        uses the production ``lib.llm_dispatch.smart_chat``.
        client:         Optional :class:`~lib.trading._common.TradingClient` instance
                        for trading data HTTP requests.  Forwarded to
                        ``run_autopilot_cycle``.
        trading_provider:  Optional :class:`~lib.protocols.TradingDataProvider` for
                        trading data access.  Forwarded to ``run_autopilot_cycle``.
    """
    if not _autopilot_state['enabled'] or _autopilot_state['running']:
        return

    now = datetime.now()
    next_run = _autopilot_state.get('next_run')
    if next_run and now < next_run:
        return

    # Time to run
    _autopilot_state['running'] = True
    _autopilot_state['error'] = None

    db = None
    try:
        from lib.database import DOMAIN_TRADING, get_thread_db
        db = get_thread_db(DOMAIN_TRADING)

        from lib.trading_autopilot.cycle import run_autopilot_cycle

        _autopilot_state['cycle_count'] += 1
        result = run_autopilot_cycle(
            db,
            cycle_number=_autopilot_state['cycle_count'],
            llm=llm,
            client=client,
            trading_provider=trading_provider,
        )

        _autopilot_state['last_cycle'] = result['timestamp']
        _autopilot_state['last_cycle_id'] = result['cycle_id']
        _autopilot_state['next_run'] = now + timedelta(minutes=AUTOPILOT_CYCLE_MINUTES)
    except Exception as e:
        logger.error('[Autopilot] Scheduled cycle failed: %s', e, exc_info=True)
        _autopilot_state['error'] = str(e)
        _autopilot_state['next_run'] = now + timedelta(minutes=5)  # Retry sooner on error
    finally:
        # Don't close thread-local connection — it's reused
        _autopilot_state['running'] = False
