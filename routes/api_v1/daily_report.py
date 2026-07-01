"""routes/daily_report.py — Daily report HTTP endpoints (thin route layer).

Every piece of business logic (storage, prompts, cost calculation, TODO
carryover, conversation extraction + LLM analysis, async generator,
scheduler) lives in ``lib.daily_report.*``.

Endpoints:
  POST /api/daily-report                          — Analyse a date
  GET  /api/daily-report/<date>                   — Load cached report
  POST /api/daily-report/backfill/<date>          — Server-side backfill
  GET  /api/daily-report/calendar/<year>/<month>  — Month overview
  PATCH /api/daily-report/task-status             — Manually set status
  PATCH /api/daily-report/todo-toggle             — Toggle a tomorrow TODO
  PATCH /api/daily-report/inherited-todo-toggle   — Toggle inherited TODO
  DELETE /api/daily-report/inherited-todo         — Delete inherited TODO
  GET  /api/daily-report/conv-count/<date>        — Conv count for a date
  POST /api/daily-report/task                     — Add manual TODO
  DELETE /api/daily-report/task                   — Delete manual TODO
  POST /api/daily-report/generate                 — Start async generation
  GET  /api/daily-report/status/<date>            — Poll generation status

Background: ``start_report_scheduler()`` (re-exported from
``lib.daily_report.scheduler``) is invoked from ``register_all`` to
auto-backfill yesterday on boot + every 6 h.

Back-compat: ``invalidate_day_cost_cache`` is imported from this module
by ``routes/conversations.py``; the symbol is re-exported here so that
keeps working unchanged. New code should import from ``lib.daily_report``.
"""

import datetime as _dt
import os
import random
import re
import threading
import time

from flask import Blueprint, jsonify

from lib.api_response import api_bad_request, api_not_found, api_ok
from lib.daily_report import (  # noqa: F401  — back-compat re-exports
    DEFAULT_USER_ID,
    _ANALYSIS_SYSTEM,
    _CALENDAR_CACHE_TTL,
    _LEGACY_PRESET_TO_MODEL,
    _QUOTES,
    _REPORTS_DIR,
    _TODO_TOOL_DEFAULTS,
    _TODO_TOOL_MAP,
    _active_jobs,
    _analyse_conversations,
    _backfill_yesterday_if_missing,
    _build_transcript_from_messages,
    _calc_msg_cost_cny,
    _calendar_cache,
    _clear_job,
    _close_yesterday_remaining_todos,
    _count_convs_for_date,
    _extract_convs_for_date,
    _extract_json_result,
    _fuzzy_todo_match,
    _generate_in_background,
    _get_job,
    _get_monthly_costs,
    _get_today_inherited_todos,
    _get_yesterday_carryover,
    _get_yesterday_todo_accountability,
    _jobs_lock,
    _load_cached_day_costs,
    _load_report,
    _mark_yesterday_todos_done,
    _normalize_todo_text,
    _persist_day_cost,
    _pick_persona,
    _qwen_cny,
    _report_path,
    _run_llm_analysis,
    _safe_int_ts,
    _save_report,
    _scan_costs_in_range,
    _scheduler_loop,
    _update_job,
    invalidate_day_cost_cache,
    start_report_scheduler,
)
from lib.log import get_logger
from lib.request_parser import async_parse_body
from routes.common import _db_safe

from .auth import require_auth

logger = get_logger(__name__)

api_v1_daily_report_bp = Blueprint('api_v1_daily_report', __name__)


# ═════════════════════════════════════════════════════════════
#  Endpoints
# ═════════════════════════════════════════════════════════════

@api_v1_daily_report_bp.route('/api/v1/daily-report', methods=['POST'])
@require_auth
@_db_safe
async def generate_daily_report():
    """Analyse conversations for a given date using DB-based extraction.

    Always extracts conversations from the database for accurate counts.
    Body: {date?: 'YYYY-MM-DD', force?: true}
    """
    t0 = time.monotonic()
    data = await async_parse_body()
    target_date = data.get('date', _dt.date.today().isoformat())
    force = data.get('force', False)

    logger.info('[DailyReport] POST request: date=%s, force=%s', target_date, force)

    if not re.match(r'^\d{4}-\d{2}-\d{2}$', target_date):
        logger.warning('[DailyReport] Invalid date format: %s', target_date)
        return api_bad_request('Invalid date format')

    # Return cached report unless force regenerate
    if not force:
        existing = _load_report(target_date)
        if existing and (existing.get('streams') or existing.get('tasks')):
            n = len(existing.get('streams', existing.get('tasks', [])))
            logger.info('[DailyReport] POST %s: returning cached (%d items)',
                        target_date, n)
            return api_ok({**existing})
    # Extract conversations from DB
    convs = _extract_convs_for_date(target_date)
    if not convs:
        # Still create an empty report so manual tasks can be added
        empty_result = {
            'ok': True, 'tasks': [],
            'quote': random.choice(_QUOTES),
            'persona': _pick_persona({}),
            'stats': {'totalConversations': 0},
        }
        return jsonify(empty_result)

    # Manual-state preservation (status overrides, TODO check-offs, manual
    # TODOs, legacy _todo tasks) is centralized in _analyse_conversations →
    # _merge_manual_state, so POST / backfill / async generator all inherit it.
    result = _analyse_conversations(convs, target_date)

    # Persist if analysis succeeded
    if (result.get('streams') or result.get('tomorrow')) and not result.get('error'):
        _save_report(target_date, result)

    elapsed = time.monotonic() - t0
    stream_count = len(result.get('streams', []))
    done_count = sum(1 for s in result.get('streams', []) if s.get('status') == 'done')
    logger.info('[DailyReport] POST %s completed in %.1fs: %d convs → %d streams '
                '(%d done, %d open), error=%s',
                target_date, elapsed, len(convs), stream_count, done_count,
                stream_count - done_count, result.get('error', 'none'))
    return jsonify(result)


@api_v1_daily_report_bp.route('/api/v1/daily-report/<date_str>')
@require_auth
async def get_cached_report(date_str):
    """Get a previously generated report for a specific date."""
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    today_todos = _get_today_inherited_todos(date_str)

    report = _load_report(date_str)
    if report is None:
        # No report — check if previous day has tomorrow items to inherit
        if today_todos:
            logger.debug('[DailyReport] GET %s: no report, inheriting %d todos from prev day',
                         date_str, len(today_todos))
            conv_count = _count_convs_for_date(date_str)
            return jsonify({
                'ok': True, 'streams': [], 'tomorrow': [],
                'today_todos': today_todos,
                'tasks': [],
                'stats': {'totalConversations': conv_count},
                '_inherited': True,
                'quote': random.choice(_QUOTES),
            })
        logger.debug('[DailyReport] GET %s: no cached report', date_str)
        return api_not_found('No report for this date')

    logger.debug('[DailyReport] GET %s: returning cached (%d streams, %d today_todos)',
                 date_str, len(report.get('streams', [])), len(today_todos))
    return api_ok({'today_todos': today_todos, **report})


@api_v1_daily_report_bp.route('/api/v1/daily-report/backfill/<date_str>', methods=['POST'])
@require_auth
@_db_safe
async def backfill_report(date_str):
    """Server-side backfill: extract conversations from DB and analyse.

    Used for past days when the frontend didn't generate a report.
    Also callable from the calendar UI.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    # Don't re-generate if cached
    existing = _load_report(date_str)
    if existing and (existing.get('streams') or existing.get('tasks')):
        n = len(existing.get('streams', existing.get('tasks', [])))
        logger.info('[DailyReport] Backfill %s: already cached (%d items)',
                     date_str, n)
        return api_ok({**existing})
    t0 = time.monotonic()
    convs = _extract_convs_for_date(date_str)
    if not convs:
        return jsonify({'ok': True, 'tasks': [],
                        'quote': random.choice(_QUOTES),
                        'persona': _pick_persona({}),
                        'stats': {'totalConversations': 0}})

    result = _analyse_conversations(convs, date_str)

    if result.get('streams') and not result.get('error'):
        _save_report(date_str, result)

    elapsed = time.monotonic() - t0
    stream_count = len(result.get('streams', []))
    logger.info('[DailyReport] Backfill %s completed in %.1fs: %d convs → %d streams, error=%s',
                date_str, elapsed, len(convs), stream_count,
                result.get('error', 'none'))
    return jsonify(result)


@api_v1_daily_report_bp.route('/api/v1/daily-report/calendar/<int:year>/<int:month>')
@require_auth
async def get_calendar_month(year, month):
    """Month overview: which days have cached reports + task counts."""
    if month < 1 or month > 12:
        return api_bad_request('Invalid month')

    prefix = f'{year:04d}-{month:02d}-'
    cache_key = (year, month)
    cached = _calendar_cache.get(cache_key)
    cache_fresh = cached and (time.monotonic() - cached['ts']) < _CALENDAR_CACHE_TTL

    # ── Per-day report summary: reuse the cached parse when fresh ──
    # Quick-win: previously we re-listed + re-parsed every YYYY-MM-*.json on
    # every call.  Now the parsed summary lives on the same cache entry as
    # conv_days/cost_days and is only rebuilt on TTL miss (or cost-cache
    # invalidation, which already clears this entry).
    days = None
    if cache_fresh and 'days' in cached:
        days = cached['days']
        logger.debug('[DailyReport] Calendar %d-%02d: days cache hit', year, month)
    if days is None:
        days = {}
        try:
            for fname in os.listdir(_REPORTS_DIR):
                if not (fname.startswith(prefix) and fname.endswith('.json')):
                    continue
                try:
                    day_num = int(fname[len(prefix):].replace('.json', ''))
                except ValueError as e:
                    logger.debug('[DailyReport] Skipping non-day filename %r: %s',
                                 fname, e)
                    continue
                report = _load_report(fname.replace('.json', ''))
                if not report or not ('streams' in report or 'tasks' in report):
                    continue
                streams = report.get('streams', [])
                tasks = report.get('tasks', [])
                if streams:
                    done = sum(1 for s in streams if s.get('status') == 'done')
                    total = len(streams)
                elif tasks:
                    done = sum(1 for t in tasks if t.get('status') == 'done')
                    total = len(tasks)
                else:
                    continue
                date_key = f'{year:04d}-{month:02d}-{day_num:02d}'
                days[date_key] = {
                    'total': total,
                    'done': done,
                    'incomplete': total - done,
                }
        except Exception as e:
            logger.warning('[DailyReport] Calendar scan %d-%02d: %s',
                           year, month, e)

    # ── TTL cache for expensive DB scans (conv_days + cost_days) ──
    if cache_fresh:
        conv_days = cached['conv_days']
        cost_days = cached['cost_days']
        logger.debug('[DailyReport] Calendar %d-%02d: cache hit (age %.1fs)',
                     year, month, time.monotonic() - cached['ts'])
    else:
        # ── Compute conv_days from DB (which days have conversations) ──
        conv_days = {}
        try:
            from lib.database import DOMAIN_CHAT, async_fetchall
            from lib.utils import safe_json

            month_start = _dt.date(year, month, 1)
            if month < 12:
                month_end = _dt.date(year, month + 1, 1)
            else:
                month_end = _dt.date(year + 1, 1, 1)
            ms_start = int(_dt.datetime.combine(month_start, _dt.time.min).timestamp() * 1000)
            ms_end = int(_dt.datetime.combine(month_end, _dt.time.min).timestamp() * 1000)

            # SQL-level date filter: only fetch convs whose window overlaps
            # the target month.  Bound both ends so we don't scan the whole
            # future history (created_at / updated_at are BIGINT epoch-ms).
            rows = await async_fetchall(
                'SELECT id, messages, created_at, updated_at '
                'FROM conversations WHERE user_id=? AND '
                'COALESCE(updated_at, created_at, 0) >= ? AND '
                'COALESCE(created_at, updated_at, 0) < ? '
                'ORDER BY updated_at DESC',
                (DEFAULT_USER_ID, ms_start, ms_end),
                domain=DOMAIN_CHAT,
            )
            for r in rows:
                msgs = safe_json(r['messages'], default=[], label='cal-conv-days')
                if not isinstance(msgs, list) or not msgs:
                    continue
                for msg in msgs:
                    ts = _safe_int_ts(msg.get('timestamp', 0))
                    if not ts:
                        ts = _safe_int_ts(r['updated_at'] or r['created_at'] or 0)
                    if ms_start <= ts < ms_end:
                        day_num = _dt.datetime.fromtimestamp(ts / 1000).day
                        conv_days[day_num] = conv_days.get(day_num, 0) + 1
                        break
        except Exception as e:
            logger.warning('[DailyReport] Calendar conv-days %d-%02d: %s', year, month, e)

        # ── Server-side per-day cost calculation ──
        cost_days = {}
        try:
            raw_costs = _get_monthly_costs(year, month)
            for day_num, day_data in raw_costs.items():
                cost_days[day_num] = {
                    'cost': day_data['cost'],
                    'conversations': day_data['conversations'],
                }
        except Exception as e:
            logger.warning('[DailyReport] Calendar cost calc %d-%02d: %s', year, month, e)

        # Store in cache (including the parsed per-day report summaries so
        # subsequent hits skip both the DB scans and the filesystem walk).
        _calendar_cache[cache_key] = {
            'days': days,
            'conv_days': conv_days,
            'cost_days': cost_days,
            'ts': time.monotonic(),
        }

    logger.debug('[DailyReport] Calendar %d-%02d: %d days with reports, %d days with convs, '
                 '%d days with costs',
                 year, month, len(days), len(conv_days), len(cost_days))
    return api_ok({'year': year, 'month': month, 'days': days,
                    'conv_days': conv_days, 'cost_days': cost_days})


# Canonical task-status cycle order. The "click to advance" UI used to
# hard-code this array in static/js/myday.js and compute the next status
# client-side; that domain rule now lives here so the frontend only renders
# the authoritative status the server returns.
_STATUS_CYCLE = ('in_progress', 'done', 'blocked')


def _next_cycle_status(current: str) -> str:
    """Return the status that follows ``current`` in the manual toggle cycle."""
    try:
        idx = _STATUS_CYCLE.index(current)
    except ValueError:
        # Unknown / LLM-only status (e.g. 'incomplete') — start the cycle.
        logger.debug('[DailyReport] status %r not in cycle — restarting', current)
        return _STATUS_CYCLE[0]
    return _STATUS_CYCLE[(idx + 1) % len(_STATUS_CYCLE)]


@api_v1_daily_report_bp.route('/api/v1/daily-report/task-status', methods=['PATCH'])
@require_auth
@_db_safe
async def update_task_status():
    """Update the status of a single task in a daily report.

    Allows users to manually override the LLM-assigned completion status.

    Body: ``{date, stream_id|conv_id|task_id, status?, action?}``
      * ``status`` — set an explicit status ('done'|'in_progress'|'blocked'|'incomplete').
      * ``action='cycle'`` — advance the item to the next status in the
        canonical cycle (in_progress → done → blocked → …). The server owns
        the cycle order; the response carries the resulting ``status``.
    """
    data = await async_parse_body()
    date_str = data.get('date', '')
    item_id = data.get('stream_id', '') or data.get('conv_id', '') or data.get('task_id', '')
    new_status = data.get('status', '')
    action = data.get('action', '')
    cycle = (action == 'cycle') or not new_status

    if not date_str or not item_id:
        return api_bad_request('Missing required fields')
    valid_statuses = ('done', 'in_progress', 'blocked', 'incomplete')
    if not cycle and new_status not in valid_statuses:
        return jsonify({'ok': False, 'error': f'Invalid status — must be one of {valid_statuses}'}), 400
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    report = _load_report(date_str)
    if not report:
        return api_not_found('No report for this date')

    def _apply(item) -> str:
        old_status = item.get('status', '?')
        resolved = _next_cycle_status(old_status) if cycle else new_status
        item['status'] = resolved
        item['_manual'] = True
        if resolved == 'done':
            item['remaining'] = None
        return old_status

    found = False
    resolved_status = new_status
    # Try streams first
    for stream in report.get('streams', []):
        if stream.get('id') == item_id:
            old_status = _apply(stream)
            resolved_status = stream['status']
            found = True
            logger.info('[DailyReport] Stream status updated %s: %s → %s (id=%s)',
                        date_str, old_status, resolved_status, item_id)
            break

    # Fall back to tasks (manual todos)
    if not found:
        for task in report.get('tasks', []):
            if task.get('conv_id') == item_id or task.get('id') == item_id:
                old_status = _apply(task)
                resolved_status = task['status']
                found = True
                logger.info('[DailyReport] Task status updated %s: %s → %s (id=%s)',
                            date_str, old_status, resolved_status, item_id)
                break

    if not found:
        return api_not_found('Item not found in report')

    _save_report(date_str, report)
    return api_ok({'status': resolved_status})


@api_v1_daily_report_bp.route('/api/v1/daily-report/todo-toggle', methods=['PATCH'])
@require_auth
@_db_safe
async def toggle_tomorrow_todo():
    """Toggle the done state of a tomorrow TODO item.

    Body: {date: 'YYYY-MM-DD', todo_id: 'todo-...', done: true|false}
    """
    data = await async_parse_body()
    date_str = data.get('date', '')
    todo_id = data.get('todo_id', '')
    done = data.get('done', False)

    if not date_str or not todo_id:
        return api_bad_request('Missing date or todo_id')

    report = _load_report(date_str)
    if not report:
        return api_not_found('No report for this date')

    found = False
    for item in report.get('tomorrow', []):
        if item.get('id') == todo_id:
            item['done'] = bool(done)
            found = True
            break

    if not found:
        return api_not_found('TODO item not found')

    _save_report(date_str, report)
    logger.info('[DailyReport] Tomorrow TODO toggled %s: %s done=%s', date_str, todo_id, done)
    return api_ok()


@api_v1_daily_report_bp.route('/api/v1/daily-report/inherited-todo-toggle', methods=['PATCH'])
@require_auth
@_db_safe
async def toggle_inherited_todo():
    """Toggle a TODO item that was inherited from a previous day's report.

    This is a cross-day operation: the item lives in ``origin_date``'s
    ``tomorrow[]`` array, but the user is toggling it from ``view_date``'s
    "今日待办" section.

    Body: {origin_date: 'YYYY-MM-DD', todo_id: 'todo-...', done: bool}
    """
    data = await async_parse_body()
    origin_date = data.get('origin_date', '')
    todo_id = data.get('todo_id', '')
    done = data.get('done', False)

    if not origin_date or not todo_id:
        return api_bad_request('Missing origin_date or todo_id')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', origin_date):
        return api_bad_request('Invalid date format')

    report = _load_report(origin_date)
    if not report:
        return api_not_found('No report for origin date')

    found = False
    for item in report.get('tomorrow', []):
        if item.get('id') == todo_id:
            item['done'] = bool(done)
            found = True
            break

    if not found:
        return api_not_found('TODO item not found in origin report')

    _save_report(origin_date, report)
    logger.info('[DailyReport] Inherited TODO toggled: origin=%s id=%s done=%s',
                origin_date, todo_id, done)
    return api_ok()


@api_v1_daily_report_bp.route('/api/v1/daily-report/inherited-todo', methods=['DELETE', 'POST'])
@require_auth
@_db_safe
async def delete_inherited_todo():
    """Delete a TODO item inherited from a previous day's report.

    This removes the item from the origin date's ``tomorrow[]`` array,
    so it won't appear in any subsequent day's "今日待办" section.

    Body: ``{origin_date: 'YYYY-MM-DD', todo_id: 'todo-...'}``
    """
    data = await async_parse_body()
    origin_date = data.get('origin_date', '')
    todo_id = data.get('todo_id', '')

    if not origin_date or not todo_id:
        return api_bad_request('Missing origin_date or todo_id')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', origin_date):
        return api_bad_request('Invalid date format')

    report = _load_report(origin_date)
    if not report:
        return api_not_found('No report for origin date')

    tomorrow = report.get('tomorrow', [])
    new_tomorrow = [t for t in tomorrow if t.get('id') != todo_id]

    if len(new_tomorrow) == len(tomorrow):
        return api_not_found('TODO item not found in origin report')

    report['tomorrow'] = new_tomorrow
    _save_report(origin_date, report)
    logger.info('[DailyReport] Inherited TODO deleted: origin=%s id=%s', origin_date, todo_id)
    return api_ok()


@api_v1_daily_report_bp.route('/api/v1/daily-report/conv-count/<date_str>')
@require_auth
async def get_conv_count(date_str):
    """Return the number of conversations with activity on a given date.

    Queries the database directly — reliable count regardless of frontend state.
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    count = _count_convs_for_date(date_str)
    logger.debug('[DailyReport] conv-count %s: %d conversations', date_str, count)
    return api_ok({'count': count, 'date': date_str})


@api_v1_daily_report_bp.route('/api/v1/daily-report/task', methods=['POST'])
@require_auth
@_db_safe
async def add_manual_task():
    """Add a manually created TODO item to a daily report.

    Body: ``{date: 'YYYY-MM-DD', task: 'task description'}``
    Creates the report file if it doesn't exist.
    """
    data = await async_parse_body()
    date_str = data.get('date', '')
    task_text = (data.get('task', '') or '').strip()

    if not date_str or not task_text:
        return api_bad_request('Missing date or task')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    import uuid
    todo_id = f'todo-{uuid.uuid4().hex[:8]}'

    report = _load_report(date_str) or {
        'streams': [], 'tomorrow': [], 'tasks': [],
        'stats': {'totalConversations': 0},
        'quote': random.choice(_QUOTES),
    }
    report.setdefault('tomorrow', []).append({
        'id': todo_id,
        'text': task_text[:60],
        'done': False,
        # Mark as user-authored so a report regeneration preserves it
        # (see lib/daily_report/todos.py::_merge_manual_state).
        '_manual': True,
    })
    _save_report(date_str, report)
    logger.info('[DailyReport] TODO added to %s: %s (id=%s)', date_str, task_text[:60], todo_id)
    return api_ok({'task_id': todo_id, 'report': report})


@api_v1_daily_report_bp.route('/api/v1/daily-report/task', methods=['DELETE'])
@require_auth
@_db_safe
async def delete_manual_task():
    """Delete a TODO item from a daily report.

    Body: ``{date: 'YYYY-MM-DD', task_id: 'todo-...'}``
    """
    data = await async_parse_body()
    date_str = data.get('date', '')
    task_id = data.get('task_id', '')

    if not date_str or not task_id:
        return api_bad_request('Missing date or task_id')
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    report = _load_report(date_str)
    if not report:
        return api_not_found('No report for this date')

    tomorrow = report.get('tomorrow', [])
    new_tomorrow = [t for t in tomorrow if t.get('id') != task_id]

    if len(new_tomorrow) == len(tomorrow):
        return api_not_found('TODO item not found')

    report['tomorrow'] = new_tomorrow
    _save_report(date_str, report)
    logger.info('[DailyReport] TODO deleted from %s: %s', date_str, task_id)
    return api_ok({'report': report})


@api_v1_daily_report_bp.route('/api/v1/daily-report/generate', methods=['POST'])
@require_auth
@_db_safe
async def start_generation():
    """Start async report generation.  Returns immediately.

    Body: {date?: 'YYYY-MM-DD', force?: true}
    Poll ``/api/daily-report/status/<date>`` for progress.
    """
    data = await async_parse_body()
    target_date = data.get('date', _dt.date.today().isoformat())
    force = data.get('force', False)

    if not re.match(r'^\d{4}-\d{2}-\d{2}$', target_date):
        return api_bad_request('Invalid date format')

    # Already generating?
    job = _get_job(target_date)
    if job and job.get('status') == 'generating':
        logger.debug('[DailyReport] Generate %s: already running', target_date)
        return jsonify({'ok': True, 'status': 'generating',
                        'progress': job.get('progress', {})})

    # Check cache (unless forced)
    if not force:
        existing = _load_report(target_date)
        if existing and (existing.get('streams') or existing.get('tasks')):
            return api_ok({'status': 'done', 'report': existing})
    # Launch background thread
    _update_job(target_date, 'generating',
                progress={'stage': 'starting', 'message': '正在启动…'})
    t = threading.Thread(
        target=_generate_in_background,
        args=(target_date, force),
        daemon=True,
        name=f'report-gen-{target_date}',
    )
    t.start()
    logger.info('[DailyReport] Background generation launched for %s (force=%s)',
                target_date, force)

    return jsonify({'ok': True, 'status': 'generating',
                    'progress': {'stage': 'starting', 'message': '正在启动…'}})


@api_v1_daily_report_bp.route('/api/v1/daily-report/status/<date_str>')
@require_auth
async def get_generation_status(date_str):
    """Poll generation progress for a date.

    Returns one of:
      ``{status: 'idle'}``                                — nothing running or cached
      ``{status: 'generating', progress: {…}}``           — work in progress
      ``{status: 'done', report: {…}}``                   — finished
      ``{status: 'error', error: '…'}``                   — failed
    """
    if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return api_bad_request('Invalid date format')

    job = _get_job(date_str)

    today_todos = _get_today_inherited_todos(date_str)

    if not job:
        # No active job — check disk cache
        existing = _load_report(date_str)
        if existing and (existing.get('streams') is not None or existing.get('tasks') is not None):
            existing['today_todos'] = today_todos
            return api_ok({'status': 'done', 'report': existing})
        # Check if previous day has inherited todos
        if today_todos:
            conv_count = _count_convs_for_date(date_str)
            return jsonify({
                'ok': True, 'status': 'done',
                'report': {
                    'streams': [], 'tomorrow': [], 'tasks': [],
                    'today_todos': today_todos,
                    'stats': {'totalConversations': conv_count},
                    '_inherited': True,
                    'quote': random.choice(_QUOTES),
                },
            })
        return api_ok({'status': 'idle'})
    status = job.get('status', 'idle')

    if status == 'done':
        _clear_job(date_str)
        report = _load_report(date_str) or {'tasks': []}
        report['today_todos'] = today_todos
        return api_ok({'status': 'done', 'report': report})
    if status == 'error':
        error_msg = job.get('error', 'Unknown error')
        _clear_job(date_str)
        return api_ok({'status': 'error', 'error': error_msg})
    # Still generating
    return jsonify({'ok': True, 'status': 'generating',
                    'progress': job.get('progress', {})})


__all__ = ['api_v1_daily_report_bp']
