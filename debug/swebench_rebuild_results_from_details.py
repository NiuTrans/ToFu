#!/usr/bin/env python3
"""
Rebuild swebench_workdir/swebench_results.json from the per-instance
details/*.json files, which are the ground truth of every attempted run.

This is needed after a `--smart-resume` pipeline has stripped failed rows
from results.json — switching to plain `--resume` afterwards requires all
historical attempts (resolved AND failed) to be present as rows, otherwise
they'd be re-run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.log import get_logger

logger = get_logger(__name__)

WORKDIR = Path('swebench_workdir')
RESULTS_PATH = WORKDIR / 'swebench_results.json'
DETAILS_DIR = WORKDIR / 'details'
BACKUP_PATH = WORKDIR / 'swebench_results.before_rebuild.json'


def _detail_to_result_row(d: dict) -> dict:
    """Convert a detail file dict to a BenchmarkResult-compatible row."""
    inf = d.get('inference', {}) or {}
    ev = d.get('eval', {}) or {}
    f2p = ev.get('fail_to_pass', {}) or {}
    p2p = ev.get('pass_to_pass', {}) or {}

    def _count(mapping):
        passed = sum(1 for v in mapping.values() if v)
        total = len(mapping)
        return passed, total

    f2p_passed, f2p_total = _count(f2p)
    p2p_passed, p2p_total = _count(p2p)

    err = inf.get('error', '') or ev.get('error', '') or ''

    return {
        'instance_id': d['instance_id'],
        'repo': d.get('repo', ''),
        'difficulty': d.get('difficulty', ''),
        'tool': d['tool'],
        'resolved': bool(d.get('resolved') or ev.get('resolved', False)),
        'duration_s': float(inf.get('duration_s', 0.0) or 0.0),
        'cost_usd': float(inf.get('cost_usd', 0.0) or 0.0),
        'input_tokens': int(inf.get('input_tokens', 0) or 0),
        'output_tokens': int(inf.get('output_tokens', 0) or 0),
        'cache_read_tokens': int(inf.get('cache_read_tokens', 0) or 0),
        'cache_write_tokens': int(inf.get('cache_write_tokens', 0) or 0),
        'num_turns': int(inf.get('num_turns', 0) or 0),
        'fail_to_pass_passed': f2p_passed,
        'fail_to_pass_total': f2p_total,
        'pass_to_pass_passed': p2p_passed,
        'pass_to_pass_total': p2p_total,
        'patch_applies': bool(ev.get('patch_applies', d.get('patch_applies', False))),
        'error': str(err)[:2000],
    }


def main() -> int:
    if not DETAILS_DIR.exists():
        logger.error('details/ dir not found at %s', DETAILS_DIR)
        return 1

    detail_files = sorted(DETAILS_DIR.glob('*.json'))
    logger.info('Found %d detail files', len(detail_files))

    rows: list[dict] = []
    errors = 0
    for p in detail_files:
        try:
            d = json.loads(p.read_text())
            rows.append(_detail_to_result_row(d))
        except Exception as e:
            errors += 1
            logger.warning('Failed to parse %s: %s', p.name, e)

    # Stats
    from collections import Counter
    by_tool_total = Counter(r['tool'] for r in rows)
    by_tool_resolved = Counter(r['tool'] for r in rows if r['resolved'])
    logger.info('Rebuilt %d rows (%d parse errors)', len(rows), errors)
    for tool in sorted(by_tool_total):
        t = by_tool_total[tool]
        r = by_tool_resolved[tool]
        logger.info('  %-14s  attempted=%4d  resolved=%4d  failed=%4d',
                    tool, t, r, t - r)

    # Load existing results.json (for config, instances, timestamp)
    existing = {}
    if RESULTS_PATH.exists():
        existing = json.loads(RESULTS_PATH.read_text())
        # Back up first
        BACKUP_PATH.write_text(RESULTS_PATH.read_text())
        logger.info('Backed up existing results.json -> %s', BACKUP_PATH)

    out = {
        'timestamp': existing.get('timestamp', ''),
        'config': existing.get('config', {}),
        'instances': existing.get('instances', []),
        'results': rows,
        'summary': existing.get('summary', {}),
    }
    RESULTS_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    logger.info('Wrote %s with %d rows', RESULTS_PATH, len(rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
