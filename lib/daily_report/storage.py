"""Report storage + active-job tracking.

Reports persist to ``<project>/data/config/daily_reports/YYYY-MM-DD.json``
(see :mod:`lib.config_dir`). Active background generation jobs live in
an in-process dict keyed by date string.
"""

import json
import os
import threading
import time

from lib.config_dir import config_path as _config_path
from lib.log import get_logger

logger = get_logger(__name__)


# Shared default user id — single-user deployment. Mirrors
# ``routes.common.DEFAULT_USER_ID`` to avoid a routes→lib import cycle.
DEFAULT_USER_ID = 1


# ── Report storage ──────────────────────────────────────────
_REPORTS_DIR = _config_path('daily_reports')
os.makedirs(_REPORTS_DIR, exist_ok=True)


# ── Active generation jobs ──────────────────────────────────
_active_jobs: dict = {}     # date_str → {status, progress, error, started_at}
_jobs_lock = threading.Lock()


def _update_job(date_str, status, progress=None, error=None):
    """Thread-safe update of background generation job status."""
    with _jobs_lock:
        if date_str not in _active_jobs:
            _active_jobs[date_str] = {'started_at': time.time()}
        job = _active_jobs[date_str]
        job['status'] = status
        if progress is not None:
            job['progress'] = progress
        if error is not None:
            job['error'] = error


def _get_job(date_str):
    """Thread-safe read of job status.  Returns dict copy or None."""
    with _jobs_lock:
        job = _active_jobs.get(date_str)
        return dict(job) if job else None


def _clear_job(date_str):
    """Remove finished job from tracking dict."""
    with _jobs_lock:
        _active_jobs.pop(date_str, None)


def _report_path(date_str):
    """File path for a daily report.  date_str = 'YYYY-MM-DD'."""
    return os.path.join(_REPORTS_DIR, f'{date_str}.json')


def _save_report(date_str, report_data):
    """Persist a daily report to disk.

    Side effect: invalidates the calendar TTL cache for the report's
    month so the next calendar render picks up the change.
    """
    # Local import to avoid a circular import at module-load time
    # (cost.py imports lib.utils which transitively touches config).
    from .cost import _calendar_cache
    try:
        payload = dict(report_data)
        payload['date'] = date_str
        payload['generated_at'] = int(time.time() * 1000)
        for k in ('ok', 'error'):
            payload.pop(k, None)
        with open(_report_path(date_str), 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        n = len(payload.get('streams', payload.get('tasks', [])))
        logger.info('[DailyReport] Saved report for %s (%d items)', date_str, n)
        # Invalidate calendar cache for this month so fresh data appears
        try:
            parts = date_str.split('-')
            cache_key = (int(parts[0]), int(parts[1]))
            _calendar_cache.pop(cache_key, None)
        except (ValueError, IndexError) as e:
            logger.debug('[DailyReport] Cache key parse failed for %s: %s', date_str, e)
    except Exception as e:
        logger.error('[DailyReport] Failed to save %s: %s', date_str, e, exc_info=True)


def _load_report(date_str):
    """Load a cached report.  Returns dict or None.

    Handles both legacy per-conversation format (tasks) and new
    work-stream format (streams).
    """
    path = _report_path(date_str)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            report = json.load(f)
        # Normalize stream statuses
        for s in report.get('streams', []):
            if s.get('status') not in ('done', 'in_progress', 'blocked'):
                s['status'] = 'in_progress'
        # Normalize legacy per-task statuses
        for t in report.get('tasks', []):
            if t.get('status') not in ('done', 'incomplete'):
                t['status'] = 'incomplete'
        return report
    except Exception as e:
        logger.warning('[DailyReport] Failed to load %s: %s', date_str, e)
        return None
