"""lib/optimizer/analyzer/_audit.py — audit.log parsing + collectors.

JSON audit-line parsing, timestamp coercion, and focused/combined scanners for
event counts, model switches, and tool-error fingerprints. The mutable
``AUDIT_LOG_FILE`` path is read via the facade package so tests can monkeypatch
it on ``analyzer``.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
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


def _scan_audit_log(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
    log_lines: Sequence[str] | None,
    collect_events: bool,
    collect_model_switches: bool,
    collect_tool_errors: bool,
) -> dict:
    """Parse one audit tail into only the requested bounded projections."""
    counts: Counter = Counter()
    optimizer_events: list[dict] = []
    model_switches: list[dict] = []
    tool_error_clusters: dict[str, dict] = {}
    lines = log_lines
    if lines is None:
        lines = _safe_tail_lines(_facade.AUDIT_LOG_FILE)
    for line in lines:
        entry = _parse_audit_line(line)
        if not entry:
            continue
        if not _entry_matches_owner(
                entry,
                owner_user_id=owner_user_id,
                allow_unowned=allow_unowned):
            continue
        timestamp = _audit_ts_aware(entry)
        if timestamp is None or timestamp < cutoff_utc:
            continue
        raw_event = entry.get('event')
        event = str(raw_event or 'unknown')
        if collect_events:
            counts[event] += 1
            if event.startswith('optimizer_'):
                optimizer_events.append({
                    'event': event,
                    'timestamp': entry.get('timestamp'),
                    'details_preview': json.dumps(
                        {
                            key: value
                            for key, value in entry.items()
                            if key not in ('event', 'timestamp')
                        },
                        ensure_ascii=False,
                        default=str,
                    )[:300],
                })
        if collect_model_switches and raw_event == 'model_switch':
            model_switches.append({
                'timestamp': entry.get('timestamp'),
                'old': str(entry.get('old') or '')[:80],
                'new': str(entry.get('new') or '')[:80],
                'reason': str(entry.get('reason') or '')[:80],
                'error': str(entry.get('error') or '')[:160],
            })
        if collect_tool_errors and raw_event == 'tool_error':
            fingerprint = str(
                entry.get('fingerprint') or entry.get('detail') or 'unknown')
            key = f'tool_error::{fingerprint}'
            cluster = tool_error_clusters.get(key)
            if cluster is None:
                cluster = {
                    'fingerprint': key,
                    'source': 'tool_error',
                    'count': 0,
                    'first_seen': timestamp,
                    'last_seen': timestamp,
                    'example': str(
                        entry.get('detail') or fingerprint)[:240],
                    'tool': entry.get('tool', '?'),
                    'exc_type': entry.get('exc_type', ''),
                }
                tool_error_clusters[key] = cluster
            cluster['count'] += 1
            if timestamp < cluster['first_seen']:
                cluster['first_seen'] = timestamp
            if timestamp > cluster['last_seen']:
                cluster['last_seen'] = timestamp
    return {
        'audit_event_counts': dict(counts),
        'optimizer_events': optimizer_events,
        'model_switch_events': model_switches[-10:],
        'tool_error_clusters': tool_error_clusters,
    }


def _collect_audit_log_evidence(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
    log_lines: Sequence[str] | None = None,
) -> dict:
    """Collect every optimizer audit projection in one JSON-decoding pass."""
    return _scan_audit_log(
        cutoff_utc,
        owner_user_id=owner_user_id,
        allow_unowned=allow_unowned,
        log_lines=log_lines,
        collect_events=True,
        collect_model_switches=True,
        collect_tool_errors=True,
    )


def _collect_audit_tool_error_clusters(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
    log_lines: Sequence[str] | None = None,
) -> dict[str, dict]:
    return _scan_audit_log(
        cutoff_utc,
        owner_user_id=owner_user_id,
        allow_unowned=allow_unowned,
        log_lines=log_lines,
        collect_events=False,
        collect_model_switches=False,
        collect_tool_errors=True,
    )['tool_error_clusters']


def _collect_audit_events(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
    log_lines: Sequence[str] | None = None,
) -> tuple[dict[str, int], list[dict]]:
    """Return (event_counts, optimizer-related rows)."""
    evidence = _scan_audit_log(
        cutoff_utc,
        owner_user_id=owner_user_id,
        allow_unowned=allow_unowned,
        log_lines=log_lines,
        collect_events=True,
        collect_model_switches=False,
        collect_tool_errors=False,
    )
    return evidence['audit_event_counts'], evidence['optimizer_events']


def _collect_audit_secondary(
    cutoff_utc: datetime,
    *,
    owner_user_id: int,
    allow_unowned: bool,
    log_lines: Sequence[str] | None = None,
) -> dict:
    """Scan audit.log for structured cost / routing events.

    Returns ``model_switch_events`` (most recent 10).
    """
    evidence = _scan_audit_log(
        cutoff_utc,
        owner_user_id=owner_user_id,
        allow_unowned=allow_unowned,
        log_lines=log_lines,
        collect_events=False,
        collect_model_switches=True,
        collect_tool_errors=False,
    )
    return {'model_switch_events': evidence['model_switch_events']}
