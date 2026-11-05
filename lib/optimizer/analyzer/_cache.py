"""lib/optimizer/analyzer/_cache.py — same-route prompt-cache split evidence.

Streams the last window of ``[CacheRoundRecord]`` lines from the app log and
its rotated siblings through the shared read-share tally owned by
``lib/tasks_pkg/cache_tracking/_health.py``. The analyzer's bounded 4 MiB tail
covers only a few hours of a busy log, so this collector reads whole files
line by line and keeps only per-group counters. Process logs carry no owner;
callers use it only where unowned observability is allowed.
"""

from __future__ import annotations

from datetime import datetime
import glob
import os

from lib.optimizer import analyzer as _facade
from lib.tasks_pkg.cache_tracking._health import (
    RECORD_MARKER,
    SPLIT_DIMENSIONS,
    ReadShareTally,
    parse_round_record,
)


def _app_log_files(app_log: str, cutoff_local: datetime) -> list[str]:
    """The live log plus plain rotated siblings written after the cutoff."""
    cutoff = cutoff_local.timestamp()
    paths = []
    for path in sorted(glob.glob(glob.escape(app_log) + '*')):
        suffix = path[len(app_log):]
        # ``app.log``, ``app.log.2026-10-27`` and ``app.log.1``; never locks
        # or compressed archives.
        if suffix and not suffix[1:].replace('-', '').isdigit():
            continue
        if os.path.getmtime(path) < cutoff:
            continue
        paths.append(path)
    return paths


def _collect_cache_read_share_divergence(cutoff_local: datetime) -> list[dict]:
    """Every (dimension, model) whose request groups diverged after ``cutoff_local``."""
    cutoff = cutoff_local.strftime('%Y-%m-%d %H:%M:%S')
    tally = ReadShareTally()
    for path in _app_log_files(_facade.APP_LOG, cutoff_local):
        with open(path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                if RECORD_MARKER not in line:
                    continue
                parsed = parse_round_record(line)
                if parsed is None:
                    continue
                stamp, rec = parsed
                if stamp and stamp >= cutoff:
                    tally.add(rec)
    found = []
    for dimension in SPLIT_DIMENSIONS:
        for divergence in tally.split(dimension)['divergences']:
            found.append({
                'dimension': dimension,
                **divergence,
                'cold_share': round(divergence['cold_share'], 3),
                'warm_share': round(divergence['warm_share'], 3),
            })
    return found
