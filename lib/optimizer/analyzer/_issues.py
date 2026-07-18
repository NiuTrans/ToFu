"""lib/optimizer/analyzer/_issues.py — error-signature clustering.

The ``_ERROR_SIGNATURES`` table (mirrors debug/triage_errors.py) plus the
fingerprint-clustering ``_collect_recurring_issues`` that merges structured
``tool_error`` audit events with coarse ``error.log`` signature matches.
Mutable log paths are read via the facade package for test monkeypatching.
"""

from __future__ import annotations

import re
from datetime import datetime

from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _safe_tail_lines, _parse_app_log_ts
from ._audit import _parse_audit_line, _audit_ts_aware

logger = get_logger(__name__)


# Error-log signature table — mirrors debug/triage_errors.py SIGNATURES so the
# nightly loop clusters error.log the same way the manual triage CLI does.
# (Kept in sync intentionally; first match wins.)
_ERROR_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ('PREMATURE STREAM CLOSE',   re.compile(r'PREMATURE STREAM CLOSE', re.I)),
    ('PREFIX MUTATION',          re.compile(r'PREFIX MUTATION', re.I)),
    ('run_command timed out',    re.compile(r'run_command timed out', re.I)),
    ('429 rate-limited',         re.compile(r'\b429\b.*rate.?limit', re.I)),
    ('DISCONNECTED PREMATURELY', re.compile(r'DISCONNECTED PREMATURELY', re.I)),
    ('tool handler raised',      re.compile(r'Tool handler \S+ raised', re.I)),
    ('AttributeError',           re.compile(r'AttributeError')),
    ('KeyError',                 re.compile(r'KeyError')),
    ('ConnectionError',          re.compile(r'ConnectionError|ConnectionResetError')),
    ('Timeout',                  re.compile(r'\bTimeout(Error)?\b')),
    ('Traceback',                re.compile(r'Traceback \(most recent call last\)')),
]


def _classify_error_signature(line: str) -> str:
    for label, rx in _ERROR_SIGNATURES:
        if rx.search(line):
            return label
    return ''


def _collect_recurring_issues(cutoff_local: datetime,
                              cutoff_utc: datetime,
                              min_count: int = 2) -> list[dict]:
    """Cluster failures into recurring-issue groups.

    Two independent sources are merged into one fingerprint → stats map:

      1. Structured ``tool_error`` audit events (emitted by the executor on
         a genuine tool-handler bug) — grouped by their precomputed
         ``fingerprint`` so the SAME bug across many tasks collapses to one
         row. This is the high-signal path; it carries exc_type + the tool.
      2. ``error.log`` lines grouped by ``_ERROR_SIGNATURES`` — a coarse
         net for failures that never reached the structured event (e.g.
         crashes outside the tool path).

    A cluster is "recurring" once it has ``>= min_count`` occurrences in the
    window. Returns the clusters sorted by count desc (capped), each with
    first/last-seen timestamps and a representative example — exactly the
    recurring/unresolved-issue view the loop previously lacked.
    """
    clusters: dict[str, dict] = {}

    def _bump(key: str, *, source: str, ts: datetime | None,
              example: str, **extra) -> None:
        c = clusters.get(key)
        if c is None:
            c = {'fingerprint': key, 'source': source, 'count': 0,
                 'first_seen': None, 'last_seen': None, 'example': example[:240]}
            c.update(extra)
            clusters[key] = c
        c['count'] += 1
        if ts is not None:
            if c['first_seen'] is None or ts < c['first_seen']:
                c['first_seen'] = ts
            if c['last_seen'] is None or ts > c['last_seen']:
                c['last_seen'] = ts

    # ── Source 1: structured tool_error audit events ──
    for line in _safe_tail_lines(_facade.AUDIT_LOG_FILE):
        entry = _parse_audit_line(line)
        if not entry or entry.get('event') != 'tool_error':
            continue
        ts = _audit_ts_aware(entry)
        if ts is None or ts < cutoff_utc:
            continue
        fp = str(entry.get('fingerprint') or entry.get('detail') or 'unknown')
        _bump(f'tool_error::{fp}', source='tool_error',
              ts=ts, example=str(entry.get('detail') or fp),
              tool=entry.get('tool', '?'), exc_type=entry.get('exc_type', ''))

    # ── Source 2: error.log signature clustering ──
    for line in _safe_tail_lines(_facade.ERROR_LOG, max_bytes=2 * 1024 * 1024):
        ts = _parse_app_log_ts(line)
        if ts is not None and ts < cutoff_local:
            continue
        label = _classify_error_signature(line)
        if not label:
            continue
        _bump(f'errorlog::{label}', source='error_log', ts=ts, example=line)

    recurring = [c for c in clusters.values() if c['count'] >= min_count]
    recurring.sort(key=lambda c: c['count'], reverse=True)

    out: list[dict] = []
    for c in recurring[:15]:
        out.append({
            'fingerprint': c['fingerprint'][:200],
            'source': c['source'],
            'count': c['count'],
            'tool': c.get('tool', ''),
            'exc_type': c.get('exc_type', ''),
            'first_seen': c['first_seen'].isoformat() if c['first_seen'] else '',
            'last_seen': c['last_seen'].isoformat() if c['last_seen'] else '',
            'example': c['example'],
        })
    return out
