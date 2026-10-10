"""Thread-free compatibility facade for billing reserve recovery.

The authoritative sweep and policy live in :mod:`lib.billing.wallet_janitor`;
one durably claimed scheduler task owns cadence. Historical imports of
``sweep_once`` remain valid, while ``start_janitor`` deliberately creates no
second process-local worker.
"""

from __future__ import annotations

from lib.log import get_logger
from lib.billing.wallet_janitor import sweep_stale_reserves

logger = get_logger(__name__)


def sweep_once() -> dict:
    """Run the canonical sweep and preserve the legacy result field names."""
    result = sweep_stale_reserves()
    return {
        'candidates': int(result.get('candidates', 0)),
        'released': int(result.get('reclaimed', 0)),
        'skipped_running': int(result.get('skipped_running', 0)),
        'failed': int(result.get('errors', 0)),
    }


def start_janitor() -> bool:
    """Compatibility no-op; the durable scheduler owns the only cadence."""
    logger.debug('[Janitor] process-local worker retired; scheduler owns sweep')
    return False


def stop_janitor(timeout: float = 5.0) -> bool:
    """Compatibility no-op; retained for old shutdown integrations."""
    del timeout
    return True


__all__ = ['start_janitor', 'stop_janitor', 'sweep_once']
