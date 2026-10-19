#!/usr/bin/env python3
"""Read retained audit JSONL and summarize local tool search observations.

Usage: python3 scripts/report_tool_search_outcomes.py logs/audit.log [rotations...]
No storage/network access. Fixed-size groups and recent rows bound resident memory.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import heapq
import json
import os
from pathlib import Path

_REDUNDANT_REASONS = frozenset({'already_visible', 'all_tools_disclosed', 'page_exhausted'})


def _bounded_increment(counts, key, overflow):
    if key not in counts and len(counts) >= 1024:
        key = overflow
    counts[key] += 1
    return key == overflow


def _record_time(row):
    try:
        value = datetime.fromisoformat(str(row.get('timestamp') or '').replace('Z', '+00:00'))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (ValueError, OverflowError):
        return float('-inf')


def summarize(paths, *, model='', query=''):
    totals = Counter()
    groups = Counter()
    by_model = {}
    unsuccessful_queries = Counter()
    recent = []
    scanned_files = set()
    overflowed = {'groups': False, 'models': False, 'queries': False}
    repeated = 0
    malformed = 0
    for path in paths:
        with Path(path).open('r', encoding='utf-8', errors='replace') as stream:
            # Repeated arguments and symlink aliases must not double the sample.
            stat = os.fstat(stream.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if identity in scanned_files:
                continue
            scanned_files.add(identity)
            while line := stream.readline(32769):
                if len(line) > 32768:
                    while line and not line.endswith('\n'):
                        line = stream.readline(32769)
                    malformed += 1
                    continue
                try:
                    row = json.loads(line)
                except (ValueError, RecursionError):
                    malformed += 1
                    continue
                if not isinstance(row, dict) or row.get('event') != 'tool_search_outcome':
                    continue
                if model and row.get('model') != model:
                    continue
                if query and query not in str(row.get('query') or ''):
                    continue
                outcome = row.get('outcome')
                if outcome not in ('matched', 'empty', 'error'):
                    malformed += 1
                    continue
                totals[outcome] += 1
                key = tuple(str(row.get(field) or '')[:128] for field in
                            ('model', 'source', 'outcome', 'reason'))
                overflowed['groups'] |= _bounded_increment(groups, key, ('<other>', '', '', ''))
                model_name = str(row.get('model') or '')[:128]
                if model_name not in by_model and len(by_model) >= 1024:
                    model_name = '<other>'
                    overflowed['models'] = True
                model_totals = by_model.setdefault(model_name, Counter())
                model_totals[outcome] += 1
                if outcome == 'empty' and row.get('reason') in _REDUNDANT_REASONS:
                    model_totals['redundant'] += 1
                if isinstance(row.get('repeated_query'), int) and row['repeated_query'] > 1:
                    repeated += 1
                if outcome != 'matched':
                    query_key = tuple(str(row.get(field) or '')[:limit] for field, limit in (
                        ('model', 128), ('source', 128), ('reason', 128),
                        ('namespace', 128), ('query', 512)))
                    overflowed['queries'] |= _bounded_increment(
                        unsuccessful_queries, query_key, ('<other>', '', '', '', ''))
                    detail = {field: row.get(field) for field in (
                        'timestamp', 'model', 'source', 'outcome', 'reason',
                        'query', 'namespace', 'conv_id', 'task_id', 'attempt_id',
                        'call_id', 'llm_round', 'query_is_tool_name',
                        'query_name_in_catalog', 'catalog_count', 'hidden_count',
                        'resolved_name', 'repeated_query')}
                    item = (_record_time(row), sum(totals.values()), detail)
                    if len(recent) < 20:
                        heapq.heappush(recent, item)
                    else:
                        heapq.heappushpop(recent, item)
    observations = sum(totals.values())
    model_summary = []
    for model_name, counts in by_model.items():
        count = counts['matched'] + counts['empty'] + counts['error']
        model_summary.append({
            'model': model_name, 'observations': count,
            'matched': counts['matched'], 'empty': counts['empty'], 'errors': counts['error'],
            'redundant_lookups': counts['redundant'],
            'match_rate': counts['matched'] / count,
            'empty_rate': counts['empty'] / count,
            'error_rate': counts['error'] / count,
        })
    return {
        'observations': observations,
        'matched': totals['matched'], 'empty': totals['empty'],
        'errors': totals['error'], 'malformed_lines': malformed,
        'groups': [dict(zip(('model', 'source', 'outcome', 'reason'), key), count=count)
                   for key, count in groups.most_common(50)],
        'by_model': sorted(model_summary, key=lambda row: (-row['observations'], row['model']))[:50],
        'frequent_unsuccessful_queries': [dict(zip(
            ('model', 'source', 'reason', 'namespace', 'query'), key), count=count)
            for key, count in unsuccessful_queries.most_common(20)],
        'repeated_searches': repeated,
        'aggregation_overflow': overflowed,
        'recent_unsuccessful': [item[2] for item in sorted(recent)],
        'coverage': 'Retained local audit observations only; rotation/queue loss and hosted searches are not counted.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('paths', nargs='+', help='Audit JSONL files; timestamped rows may be in any order')
    parser.add_argument('--model', default='')
    parser.add_argument('--query', default='', help='Query substring filter')
    args = parser.parse_args()
    print(json.dumps(summarize(args.paths, model=args.model, query=args.query),
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
