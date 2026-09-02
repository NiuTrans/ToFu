"""lib/optimizer/analyzer/_audit.py — audit.log parsing + collectors.

JSON audit-line parsing, timestamp coercion, and the two audit scanners
(``_collect_audit_events`` for optimizer-related rows, ``_collect_audit_secondary``
for model-switch events).  The mutable ``AUDIT_LOG_FILE`` path is read via the
facade package so tests can monkeypatch it on ``analyzer``.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime

from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _safe_tail_lines

logger = get_logger(__name__)


def _entry_matches_owner(
    entry: dict,
    *,
    owner_user_id: int,
    allow_unowned: bool,
) -> bool:
    """Keep explicitly matching audit rows; unowned rows are personal-only."""
    raw_owner = entry.get('owner_user_id', entry.get('user_id'))
    principal = entry.get('principal')
    if raw_owner in (None, '') and isinstance(principal, dict):
        raw_owner = principal.get('owner_user_id')
    if raw_owner in (None, ''):
        return allow_unowned
    try:
        return int(raw_owner) == owner_user_id
    except (TypeError, ValueError):
        return False


def _parse_audit_line(line: str) -> dict | None:
    try:
        return json.loads(line)
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Optimizer.analyzer] non-JSON audit line (len=%d): %s', len(line), e)
        return None


def _audit_ts_aware(entry: dict) -> datetime | None:
    ts = entry.get('timestamp') or ''
    if not ts:
        return None
    try:
        if ts.endswith('Z'):
            ts = ts[:-1] + '+00:00'
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError) as e:
        logger.debug('[Optimizer.analyzer] bad audit ts %r: %s', ts, e)
        return None


def _collect_audit_events(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
) -> tuple[dict[str, int], list[dict]]:
    """Return (event_counts, optimizer-related rows)."""
    counts: Counter = Counter()
    optimizer_events: list[dict] = []
    for line in _safe_tail_lines(_facade.AUDIT_LOG_FILE):
        entry = _parse_audit_line(line)
        if not entry:
            continue
        if not _entry_matches_owner(
                entry,
                owner_user_id=owner_user_id,
                allow_unowned=allow_unowned):
            continue
        ts = _audit_ts_aware(entry)
        if ts is None or ts < cutoff_utc:
            continue
        ev = str(entry.get('event') or 'unknown')
        counts[ev] += 1
        if ev.startswith('optimizer_'):
            # Keep a small summary (never the whole entry)
            optimizer_events.append({
                'event': ev,
                'timestamp': entry.get('timestamp'),
                'details_preview': json.dumps(
                    {k: v for k, v in entry.items() if k not in ('event', 'timestamp')},
                    ensure_ascii=False, default=str)[:300],
            })
    return dict(counts), optimizer_events


def _collect_audit_secondary(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
) -> dict:
    """Scan audit.log for structured cost / routing events.

    Returns ``model_switch_events`` (most recent 10).
    """
    model_switches: list[dict] = []
    for line in _safe_tail_lines(_facade.AUDIT_LOG_FILE):
        entry = _parse_audit_line(line)
        if not entry:
            continue
        if not _entry_matches_owner(
                entry,
                owner_user_id=owner_user_id,
                allow_unowned=allow_unowned):
            continue
        ts = _audit_ts_aware(entry)
        if ts is None or ts < cutoff_utc:
            continue
        if entry.get('event') == 'model_switch':
            model_switches.append({
                'timestamp': entry.get('timestamp'),
                'old': str(entry.get('old') or '')[:80],
                'new': str(entry.get('new') or '')[:80],
                'reason': str(entry.get('reason') or '')[:80],
                'error': str(entry.get('error') or '')[:160],
            })
    return {
        'model_switch_events': model_switches[-10:],
    }
