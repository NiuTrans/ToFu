"""Report storage + active-job tracking.

Reports persist to
``<project>/data/config/daily_reports/<owner_user_id>/YYYY-MM-DD.json``
(see :mod:`lib.config_dir`). Active background generation jobs live in
an in-process dict keyed by owner and date.
"""

import copy
import datetime as _dt
import os
import re
import threading
import time

from lib.config_dir import config_path as _config_path
from lib.identity import require_user_id
from lib.json_store import (
    JsonStoreReadError,
    read_json,
    update_json_atomic,
)
from lib.log import get_logger

logger = get_logger(__name__)


# ── Report storage ──────────────────────────────────────────
_REPORTS_DIR = _config_path('daily_reports')
os.makedirs(_REPORTS_DIR, exist_ok=True)
_REPORT_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


# ── Active generation jobs ──────────────────────────────────
_active_jobs: dict[tuple[int, str], dict] = {}
_jobs_lock = threading.Lock()


def _owner_id(owner_user_id, *, context):
    return require_user_id(owner_user_id, context=context)


def _job_key(date_str, *, owner_user_id):
    return (
        _owner_id(owner_user_id, context='daily report job'),
        _parse_report_date(date_str).isoformat(),
    )


def _update_job(date_str, status, *, owner_user_id, progress=None, error=None):
    """Thread-safe update of background generation job status."""
    key = _job_key(date_str, owner_user_id=owner_user_id)
    with _jobs_lock:
        if key not in _active_jobs:
            _active_jobs[key] = {'started_at': time.time()}
        job = _active_jobs[key]
        job['status'] = status
        if progress is not None:
            job['progress'] = progress
        if error is not None:
            job['error'] = error


def _get_job(date_str, *, owner_user_id):
    """Thread-safe read of job status.  Returns dict copy or None."""
    key = _job_key(date_str, owner_user_id=owner_user_id)
    with _jobs_lock:
        job = _active_jobs.get(key)
        return dict(job) if job else None


def _clear_job(date_str, *, owner_user_id):
    """Remove finished job from tracking dict."""
    key = _job_key(date_str, owner_user_id=owner_user_id)
    with _jobs_lock:
        _active_jobs.pop(key, None)


def _reports_dir_for_owner(*, owner_user_id):
    """Return the report directory for one explicit repository owner."""
    owner_id = _owner_id(owner_user_id, context='daily report storage')
    return os.path.join(_REPORTS_DIR, str(owner_id))


def _report_path(date_str, *, owner_user_id):
    """File path for a daily report.  date_str = 'YYYY-MM-DD'."""
    _parse_report_date(date_str)
    reports_dir = _reports_dir_for_owner(owner_user_id=owner_user_id)
    return os.path.join(reports_dir, f'{date_str}.json')


def _parse_report_date(date_str):
    """Parse a canonical report date or raise ``ValueError``."""
    if not isinstance(date_str, str) or not _REPORT_DATE_RE.fullmatch(date_str):
        raise ValueError('report date must use YYYY-MM-DD')
    try:
        parsed = _dt.date.fromisoformat(date_str)
    except ValueError as e:
        raise ValueError('report date is not a valid calendar date') from e
    if parsed.isoformat() != date_str:
        raise ValueError('report date must use canonical YYYY-MM-DD form')
    return parsed


def _is_report_date(date_str):
    """Return whether ``date_str`` is a real canonical calendar date."""
    try:
        _parse_report_date(date_str)
    except (TypeError, ValueError) as e:
        logger.debug('[DailyReport] Invalid report date %r: %s', date_str, e)
        return False
    return True


def _normalize_report(report):
    """Normalize status fields without rejecting unrelated legacy fields."""
    if not isinstance(report, dict):
        return None
    streams = report.get('streams', [])
    if isinstance(streams, list):
        for stream in streams:
            if (isinstance(stream, dict)
                    and stream.get('status') not in
                    ('done', 'in_progress', 'blocked')):
                stream['status'] = 'in_progress'
    tasks = report.get('tasks', [])
    if isinstance(tasks, list):
        for task in tasks:
            if (isinstance(task, dict)
                    and task.get('status') not in ('done', 'incomplete')):
                task['status'] = 'incomplete'
    return report


def _prepare_payload(date_str, report_data):
    if not isinstance(report_data, dict):
        raise TypeError('daily report payload must be a dict')
    payload = report_data
    payload['date'] = date_str
    payload['generated_at'] = int(time.time() * 1000)
    for key in ('ok', 'error'):
        payload.pop(key, None)
    return payload


def _invalidate_calendar(date_str, *, owner_user_id):
    # Local import avoids a circular import at module-load time.
    from .cost import _calendar_cache

    parsed = _parse_report_date(date_str)
    owner_id = _owner_id(owner_user_id, context='daily report calendar')
    _calendar_cache.invalidate((owner_id, parsed.year, parsed.month))


def _log_saved(date_str, payload, *, owner_user_id):
    n = len(payload.get('streams', payload.get('tasks', [])))
    owner_id = _owner_id(owner_user_id, context='daily report storage')
    logger.info(
        '[DailyReport] Saved report for owner=%d date=%s (%d items)',
        owner_id, date_str, n,
    )
    _invalidate_calendar(date_str, owner_user_id=owner_id)


def _save_report(date_str, report_data, *, owner_user_id):
    """Atomically replace a daily report and return the stored payload.

    Side effect: invalidates the calendar TTL cache for the report's
    month so the next calendar render picks up the change.

    All writers take the same per-path process/thread lock through
    :func:`update_json_atomic`. Existing malformed JSON is never silently
    overwritten, and write failures propagate so callers cannot report a
    successful edit that was not persisted.
    """
    owner_id = _owner_id(owner_user_id, context='daily report save')
    path = _report_path(date_str, owner_user_id=owner_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    candidate = copy.deepcopy(report_data)

    def _replace(_current):
        return _prepare_payload(date_str, candidate)

    payload = update_json_atomic(
        path, _replace, default=None, strict=True, indent=2)
    _log_saved(date_str, payload, owner_user_id=owner_id)
    return payload


def _save_generated_report(date_str, report_data, *, owner_user_id):
    """Commit a generated report without clobbering concurrent user edits.

    LLM analysis can take minutes. Manual state is therefore merged from the
    latest on-disk report *inside* the same locked read-modify-write cycle as
    the final replace, rather than from a stale pre-analysis snapshot.
    """
    owner_id = _owner_id(owner_user_id, context='daily report generated save')
    path = _report_path(date_str, owner_user_id=owner_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def _merge_latest(current):
        candidate = copy.deepcopy(report_data)
        if current is not None:
            if not isinstance(current, dict):
                raise JsonStoreReadError(
                    f'daily report is not a JSON object: {path}')
            # Local import avoids storage.py <-> todos.py import recursion.
            from .todos import _merge_manual_state
            _merge_manual_state(candidate, current)
        return _prepare_payload(date_str, candidate)

    payload = update_json_atomic(
        path, _merge_latest, default=None, strict=True, indent=2)
    _log_saved(date_str, payload, owner_user_id=owner_id)
    return payload


def _update_report(date_str, mutator, *, owner_user_id, default=None):
    """Conditionally mutate one report in a locked atomic transaction.

    ``mutator`` receives the latest report (or a copy of ``default`` when the
    file is absent). Returning ``None`` performs no write; otherwise the
    returned dict is normalized with storage-owned metadata and persisted.
    """
    owner_id = _owner_id(owner_user_id, context='daily report update')
    path = _report_path(date_str, owner_user_id=owner_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    def _mutate(current):
        if current is not None and not isinstance(current, dict):
            raise JsonStoreReadError(
                f'daily report is not a JSON object: {path}')
        working = copy.deepcopy(current)
        updated = mutator(working)
        if updated is None:
            return None
        return _prepare_payload(date_str, updated)

    payload = update_json_atomic(
        path, _mutate, default=copy.deepcopy(default), strict=True, indent=2)
    if payload is not None:
        _log_saved(date_str, payload, owner_user_id=owner_id)
    return payload


def _load_report(date_str, *, owner_user_id):
    """Load a cached report.  Returns dict or None.

    Handles both legacy per-conversation format (tasks) and new
    work-stream format (streams).
    """
    owner_id = _owner_id(owner_user_id, context='daily report load')
    report = read_json(
        _report_path(date_str, owner_user_id=owner_id), default=None)
    if report is not None and not isinstance(report, dict):
        logger.warning('[DailyReport] Ignoring non-object report for %s', date_str)
        return None
    return _normalize_report(report)
