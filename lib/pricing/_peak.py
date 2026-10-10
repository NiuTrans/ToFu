"""lib/pricing/_peak.py — Peak/off-peak price schedules.

DeepSeek peak-hour pricing (api-docs.deepseek.com/quick_start/pricing):
the CURRENT V4.1 Flash schedule (2026-09-10) is 2x on ALL billing items
during 01:00-04:00 and 06:00-10:00 UTC on WEEKDAYS (Mon–Fri), in force
since 2026-09-10 04:00 UTC (1786900800). It REPLACES the retired
2026-08-16 schedule (09:00-12:00 + 14:00-18:00 Beijing time).

A pricing row (a ``MODEL_PRICING`` value or a provider-template override)
opts in with a ``'peak'`` block::

    'peak': {
        'mul': 2.0,                      # billing multiplier during peak
        'windows': [(9, 12), (14, 18)],  # local-hour windows, [start, end)
        'tz_offset': 8,                  # hours east of UTC (Beijing: no DST)
        'weekdays': [0, 1, 2, 3, 4],     # optional — local-time weekday()
                                         # (Mon=0); omitted = every day
        'effective_from': None,          # UTC epoch seconds; None = announced
                                         # but NOT yet in force → inert
    }

The multiplier scales the row's ``input`` / ``output`` unit prices inside
:func:`lib.pricing._provider.lookup_pricing`; the cache multipliers are
RELATIVE to input, so all four billing items (uncached in / cache write /
cache read / out) scale together — matching "applicable to all billing
items".
"""

import time
from datetime import datetime, timezone

from lib.log import get_logger

logger = get_logger(__name__)


def _utc_now() -> float:
    """Current UTC epoch seconds — the single seam tests monkeypatch."""
    return time.time()


def peak_multiplier(pricing: dict, at: float | None = None) -> float:
    """Return the price multiplier in force for *pricing* at unix time *at*.

    Returns 1.0 when the row carries no ``peak`` block, the schedule is not
    yet effective (``effective_from`` None or in the future), or *at* falls
    outside every window. NEVER raises — a malformed block degrades to 1.0
    (cost paths run on the request hot path).

    Args:
        pricing: One pricing row (dict with an optional ``peak`` block).
        at: UTC epoch seconds; defaults to now. Historical cost paths
            (daily_report backfill) pass the MESSAGE's timestamp so a
            rescan bills each message at its own time, not at wall clock.
    """
    if not isinstance(pricing, dict):
        return 1.0
    peak = pricing.get('peak')
    if not isinstance(peak, dict):
        return 1.0
    effective_from = peak.get('effective_from')
    if not effective_from:
        return 1.0
    if at is None:
        at = _utc_now()
    try:
        if at < float(effective_from):
            return 1.0
        tz_offset = float(peak.get('tz_offset', 0) or 0)
        local = at + tz_offset * 3600.0
        weekdays = peak.get('weekdays')
        if weekdays is not None:
            try:
                allowed = {int(d) for d in weekdays}
            except (TypeError, ValueError):
                allowed = set()
            if datetime.fromtimestamp(local, tz=timezone.utc).weekday() not in allowed:
                return 1.0
        local_hour = (local % 86400.0) / 3600.0
        for start, end in peak.get('windows') or []:
            if float(start) <= local_hour < float(end):
                return float(peak.get('mul', 1.0) or 1.0)
    except (TypeError, ValueError) as e:
        logger.warning('[Pricing] malformed peak block ignored (%r): %s',
                       peak, e)
        return 1.0
    return 1.0
