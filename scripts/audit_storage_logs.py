#!/usr/bin/env python3
"""Read a bounded app-log window and report redacted storage failure evidence.

Reads only the requested log; never contacts or changes a database. Counts log
records, not requests: coalescing and rotation prevent exact incident totals.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re

HEADER = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[(\w+)\] (\S+)')
CODE = re.compile(r'\b(database_[a-z_]+|storage_payload_too_large|deadline_elapsed|operation_not_implemented|unknown_operation)\b')


def scan_log(path: Path, *, since: str = '', maximum_bytes: int = 8 * 1024 * 1024) -> dict:
    if not 1 <= maximum_bytes <= 64 * 1024 * 1024:
        raise ValueError('log window must be within 1..64 MiB')
    if since:
        datetime.strptime(since, '%Y-%m-%d %H:%M:%S')
    size = path.stat().st_size
    offset = max(0, size - maximum_bytes)
    with path.open('rb') as stream:
        stream.seek(offset)
        data = stream.read(maximum_bytes)
    lines = data.decode('utf-8', errors='replace').splitlines()
    if offset:
        lines = lines[1:]  # The first physical record may have been cut.
    counts, components = Counter(), Counter()
    samples, first, last, matched = [], '', '', 0
    for line in lines:
        header = HEADER.match(line)
        if header is None:
            continue  # Traceback continuations are not additional incidents.
        stamp, level, component = header.groups()
        first = first or stamp
        last = max(last, stamp)
        if (since and stamp < since) or level not in ('WARNING', 'ERROR', 'CRITICAL'):
            continue
        codes = sorted(set(CODE.findall(line)))
        if not codes:
            continue
        matched += 1
        counts.update(codes)
        components[component] += 1
        samples.append({'timestamp': stamp, 'level': level,
                        'component': component, 'codes': codes})
        samples = samples[-20:]
    return {'source': str(path.resolve()), 'requested_since': since,
            'bytes_read': len(data), 'tail_truncated': bool(offset),
            'first_timestamp': first, 'last_timestamp': last,
            'requested_start_in_window': bool(first) and (not since or since >= first),
            'matching_log_records': matched, 'codes': dict(counts),
            'components': dict(components), 'recent_records': samples,
            'count_semantics': 'log records, not requests; coalescing and rotation may hide repetitions'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--log', type=Path, default=Path('logs/app.log'))
    parser.add_argument('--since', default='')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = scan_log(args.log, since=args.since)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({key: report[key] for key in ('matching_log_records', 'codes', 'last_timestamp')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
