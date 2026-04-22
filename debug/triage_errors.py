#!/usr/bin/env python3
"""debug/triage_errors.py — Cluster error-log signatures.

Reads ``logs/error.log`` (override with ``CHATUI_LOG_DIR``), groups lines by
well-known signatures, and prints a top-N table with counts, first-seen /
last-seen timestamps, and a representative example per signature.

Usage::

    python3 debug/triage_errors.py                 # default: top 15, all time
    python3 debug/triage_errors.py --top 5          # top 5 clusters
    python3 debug/triage_errors.py --since 24h      # last 24 hours only
    python3 debug/triage_errors.py --since 7d       # last 7 days
    python3 debug/triage_errors.py --log custom.log # explicit file

Exit codes::
    0  — ran successfully (even if the log was empty or missing)
    2  — CLI usage error (bad ``--since`` or unreadable file)

Pure stdlib, no third-party deps.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

# Project logger, per CLAUDE.md §2.1. Falls back to stdlib logging if the
# project layout isn't available (e.g. running from a tarball slice).
try:
    from lib.log import get_logger  # type: ignore
    logger = get_logger(__name__)
except Exception:  # pragma: no cover — best-effort fallback
    import logging
    logger = logging.getLogger('debug.triage_errors')
    if not logger.handlers:
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s [%(levelname)s] %(message)s')

# Signatures mirror docs/DEVELOPMENT_DIRECTION.md §3.1 — keep in sync.
# Each entry: (label, regex). Order matters (first match wins).
SIGNATURES: list[tuple[str, re.Pattern]] = [
    ('PREMATURE STREAM CLOSE',   re.compile(r'PREMATURE STREAM CLOSE', re.I)),
    ('PREFIX MUTATION',          re.compile(r'PREFIX MUTATION', re.I)),
    ('run_command timed out',    re.compile(r'run_command timed out', re.I)),
    ('429 rate-limited',         re.compile(r'\b429\b.*rate.?limited', re.I)),
    ('DISCONNECTED PREMATURELY', re.compile(r'DISCONNECTED PREMATURELY', re.I)),
    ('Traceback',                re.compile(r'Traceback \(most recent call last\)')),
    ('AttributeError',           re.compile(r'AttributeError')),
    ('ConnectionError',          re.compile(r'ConnectionError|ConnectionResetError')),
    ('Timeout',                  re.compile(r'\bTimeout(Error)?\b')),
]

# Matches the log formatter in server.py (``%Y-%m-%d %H:%M:%S``).
_TS_RE = re.compile(r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')


def _parse_since(spec: str) -> datetime | None:
    """Convert a ``--since`` spec like ``24h`` / ``7d`` / ``30m`` to a datetime cutoff.

    Returns ``None`` for unlimited. Raises ``ValueError`` for bad input.
    """
    if not spec:
        return None
    s = spec.strip().lower()
    m = re.match(r'^(\d+)\s*(s|m|h|d|w)$', s)
    if not m:
        raise ValueError(f'Unrecognised --since value: {spec!r} (try 24h, 7d, 30m)')
    n, unit = int(m.group(1)), m.group(2)
    delta = {
        's': timedelta(seconds=n),
        'm': timedelta(minutes=n),
        'h': timedelta(hours=n),
        'd': timedelta(days=n),
        'w': timedelta(weeks=n),
    }[unit]
    return datetime.now() - delta


def _classify(line: str) -> str:
    for label, rx in SIGNATURES:
        if rx.search(line):
            return label
    return 'OTHER'


def _extract_ts(line: str) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def triage(log_path: str, since: datetime | None) -> dict:
    """Walk the log and return a ``label -> stats`` dict.

    stats = {'count': int, 'first_ts': datetime|None, 'last_ts': datetime|None,
             'example': str}
    """
    stats: dict[str, dict] = defaultdict(
        lambda: {'count': 0, 'first_ts': None, 'last_ts': None, 'example': ''})
    total_lines = 0
    skipped_pre_cutoff = 0

    if not os.path.isfile(log_path):
        logger.warning('[Triage] Log file not found: %s', log_path)
        return {'_meta': {'path': log_path, 'total_lines': 0,
                          'skipped_pre_cutoff': 0, 'missing': True}}

    try:
        with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                total_lines += 1
                line = line.rstrip('\n')
                if not line:
                    continue
                ts = _extract_ts(line)
                if since is not None and ts is not None and ts < since:
                    skipped_pre_cutoff += 1
                    continue
                label = _classify(line)
                entry = stats[label]
                entry['count'] += 1
                if ts is not None:
                    if entry['first_ts'] is None or ts < entry['first_ts']:
                        entry['first_ts'] = ts
                    if entry['last_ts'] is None or ts > entry['last_ts']:
                        entry['last_ts'] = ts
                if not entry['example']:
                    entry['example'] = line[:240]
    except OSError as e:
        logger.error('[Triage] Failed to read %s: %s', log_path, e, exc_info=True)
        return {'_meta': {'path': log_path, 'total_lines': 0,
                          'skipped_pre_cutoff': 0, 'error': str(e)}}

    stats['_meta'] = {
        'path': log_path,
        'total_lines': total_lines,
        'skipped_pre_cutoff': skipped_pre_cutoff,
    }
    return dict(stats)


def _fmt_ts(ts: datetime | None) -> str:
    return ts.strftime('%Y-%m-%d %H:%M:%S') if ts else '-'


def render(stats: dict, top_n: int) -> str:
    meta = stats.pop('_meta', {})
    clusters = sorted(stats.items(), key=lambda kv: kv[1]['count'], reverse=True)
    if top_n > 0:
        clusters = clusters[:top_n]

    lines: list[str] = []
    lines.append('')
    lines.append('=' * 100)
    lines.append(f'Error-log triage  —  {meta.get("path", "(unknown)")}')
    lines.append(f'Total lines scanned: {meta.get("total_lines", 0)}   '
                 f'Skipped (pre-cutoff): {meta.get("skipped_pre_cutoff", 0)}')
    if meta.get('missing'):
        lines.append('  (log file does not exist — nothing to triage)')
    if meta.get('error'):
        lines.append(f'  (read error: {meta["error"]})')
    lines.append('=' * 100)

    if not clusters:
        lines.append('No matching lines.')
        return '\n'.join(lines)

    header = f'{"#":>3}  {"Count":>7}  {"First seen":<20}  {"Last seen":<20}  Signature'
    lines.append(header)
    lines.append('-' * len(header))
    for i, (label, entry) in enumerate(clusters, 1):
        lines.append(
            f'{i:>3}  {entry["count"]:>7}  '
            f'{_fmt_ts(entry["first_ts"]):<20}  '
            f'{_fmt_ts(entry["last_ts"]):<20}  '
            f'{label}'
        )
    lines.append('')
    lines.append('Examples (one per cluster, truncated 240 chars):')
    lines.append('-' * 100)
    for label, entry in clusters:
        ex = entry.get('example', '') or '(no example)'
        lines.append(f'[{label}]')
        lines.append(f'  {ex}')
        lines.append('')
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Cluster logs/error.log by known failure signatures.')
    parser.add_argument('--log', default=None,
                        help='Path to error log (default: $CHATUI_LOG_DIR/error.log '
                             'or logs/error.log)')
    parser.add_argument('--top', type=int, default=15,
                        help='Show only the top-N clusters by count (default: 15, '
                             '0 = show all)')
    parser.add_argument('--since', type=str, default='',
                        help='Only scan lines newer than this (e.g. 24h, 7d, 30m). '
                             'Lines without parseable timestamps are always included.')
    args = parser.parse_args(argv)

    # Resolve log path: explicit --log > $CHATUI_LOG_DIR/error.log > ./logs/error.log
    if args.log:
        log_path = args.log
    else:
        log_dir = os.environ.get('CHATUI_LOG_DIR', '').strip()
        if not log_dir:
            # Anchor relative to this script's repo, not the cwd
            repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            log_dir = os.path.join(repo_root, 'logs')
        log_path = os.path.join(log_dir, 'error.log')

    try:
        since = _parse_since(args.since)
    except ValueError as e:
        print(f'error: {e}', file=sys.stderr)
        return 2

    t0 = time.time()
    logger.info('[Triage] Scanning %s (since=%s)', log_path, args.since or 'all')
    stats = triage(log_path, since)
    out = render(stats, args.top)
    print(out)
    logger.info('[Triage] Done in %.2fs', time.time() - t0)
    return 0


if __name__ == '__main__':
    sys.exit(main())
