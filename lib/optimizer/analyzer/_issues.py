"""lib/optimizer/analyzer/_issues.py — error-log excerpts and clustering.

The ``_ERROR_SIGNATURES`` table (mirrors debug/triage_errors.py) plus the
single-pass error-log projector and ``_collect_recurring_issues`` merger for
structured ``tool_error`` audit events and coarse signatures. Mutable log paths
are read via the facade package for test monkeypatching.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import datetime

from lib.log import get_logger

from lib.optimizer import analyzer as _facade
from ._logs import _safe_tail_lines, _parse_app_log_ts
from ._audit import _collect_audit_tool_error_clusters

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


def _scan_error_log(
    cutoff_local: datetime,
    *,
    log_lines: Sequence[str] | None,
    collect_excerpts: bool,
    collect_clusters: bool,
    max_excerpt_lines: int = 40,
) -> tuple[list[str], dict[str, dict]]:
    """Parse one error tail into excerpts and/or recurring-issue clusters."""
    excerpt_limit = max(1, max_excerpt_lines)
    excerpts: deque[str] = deque(maxlen=excerpt_limit)
    clusters: dict[str, dict] = {}
    lines = log_lines
    if lines is None:
        lines = _safe_tail_lines(
            _facade.ERROR_LOG, max_bytes=2 * 1024 * 1024)
    for line in lines:
        timestamp = _parse_app_log_ts(line)
        is_recent = timestamp is not None and timestamp >= cutoff_local
        if collect_excerpts and is_recent:
            excerpts.append(line[:300])
        if not collect_clusters or (timestamp is not None and not is_recent):
            continue
        label = _classify_error_signature(line)
        if not label:
            continue
        key = f'errorlog::{label}'
        cluster = clusters.get(key)
        if cluster is None:
            cluster = {
                'fingerprint': key,
                'source': 'error_log',
                'count': 0,
                'first_seen': timestamp,
                'last_seen': timestamp,
                'example': line[:240],
            }
            clusters[key] = cluster
        cluster['count'] += 1
        if timestamp is not None:
            if cluster['first_seen'] is None or timestamp < cluster['first_seen']:
                cluster['first_seen'] = timestamp
            if cluster['last_seen'] is None or timestamp > cluster['last_seen']:
                cluster['last_seen'] = timestamp
    return list(excerpts), clusters


def _collect_error_log_evidence(
    cutoff_local: datetime,
    *,
    log_lines: Sequence[str] | None = None,
    max_excerpt_lines: int = 40,
) -> tuple[list[str], dict[str, dict]]:
    return _scan_error_log(
        cutoff_local,
        log_lines=log_lines,
        collect_excerpts=True,
        collect_clusters=True,
        max_excerpt_lines=max_excerpt_lines,
    )


def _collect_error_log_excerpts(
    cutoff_local: datetime,
    max_lines: int = 40,
    *,
    log_lines: Sequence[str] | None = None,
) -> list[str]:
    excerpts, _clusters = _scan_error_log(
        cutoff_local,
        log_lines=log_lines,
        collect_excerpts=True,
        collect_clusters=False,
        max_excerpt_lines=max_lines,
    )
    return excerpts


def _collect_recurring_issues(cutoff_local: datetime,
                              cutoff_utc: datetime,
                              min_count: int = 2,
                              *,
                              owner_user_id: int,
                              allow_unowned: bool,
                              audit_log_lines: Sequence[str] | None = None,
                              error_log_lines: Sequence[str] | None = None,
                              audit_issue_clusters: Mapping[str, dict] | None = None,
                              error_issue_clusters: Mapping[str, dict] | None = None,
                              ) -> list[dict]:
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
    # ── Source 1: structured tool_error audit events ──
    if audit_issue_clusters is None:
        audit_issue_clusters = _collect_audit_tool_error_clusters(
            cutoff_utc,
            owner_user_id=owner_user_id,
            allow_unowned=allow_unowned,
            log_lines=audit_log_lines,
        )
    clusters = {
        key: dict(cluster)
        for key, cluster in audit_issue_clusters.items()
    }

    # ── Source 2: error.log signature clustering ──
    if allow_unowned:
        if error_issue_clusters is None:
            _excerpts, error_issue_clusters = _scan_error_log(
                cutoff_local,
                log_lines=error_log_lines,
                collect_excerpts=False,
                collect_clusters=True,
            )
        clusters.update(
            (key, dict(cluster))
            for key, cluster in error_issue_clusters.items()
        )

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
