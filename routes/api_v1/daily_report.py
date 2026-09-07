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

Background: the owner-scoped ``Daily Report Backfill`` built-in runs through
the durable scheduler and auto-backfills yesterday on startup when missing,
then at a six-hour cron cadence.

Back-compat: ``invalidate_day_cost_cache`` is imported from this module
by ``routes/conversations.py``; the symbol is re-exported here so that
keeps working unchanged. New code should import from ``lib.daily_report``.
"""

import asyncio
import datetime as _dt
import os
import random
import threading
import time

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_not_found, api_ok
import lib.daily_report as _daily_report
from lib.log import get_logger
from lib.request_parser import async_parse_body
from routes.common import _db_safe

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_daily_report_bp = Blueprint('api_v1_daily_report', __name__)


def invalidate_day_cost_cache(*args, **kwargs):
    """Lazy compatibility seam for historical route-level imports."""
    return _daily_report.invalidate_day_cost_cache(*args, **kwargs)


def _get_today_inherited_todos(*args, **kwargs):
    """Patchable lazy seam for owner-scoped TODO inheritance."""
    return _daily_report._get_today_inherited_todos(*args, **kwargs)


def _load_report(*args, **kwargs):
    """Patchable lazy seam for owner-scoped report reads."""
    return _daily_report._load_report(*args, **kwargs)


def _update_report(*args, **kwargs):
    """Patchable lazy seam for atomic owner-scoped report mutations."""
    return _daily_report._update_report(*args, **kwargs)


def _request_owner_user_id() -> int:
    """Resolve the authenticated report owner once at the HTTP boundary."""
    return int(request_user_id())


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

    if not _daily_report._is_report_date(target_date):
        logger.warning('[DailyReport] Invalid date format: %s', target_date)
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    # Return cached report unless force regenerate
    if not force:
        existing = _load_report(
            target_date, owner_user_id=owner_user_id)
        if existing and (existing.get('streams') or existing.get('tasks')):
            n = len(existing.get('streams', existing.get('tasks', [])))
            logger.info('[DailyReport] POST %s: returning cached (%d items)',
                        target_date, n)
            return api_ok({**existing})
    # Extract conversations from DB. Off-loop: the scan fetches + json.loads-es
    # every in-window conversation's messages blob (hundreds of MB on an active
    # day) — synchronous here it stalls the event loop (LoopWatch 5s trips).
    convs = await asyncio.to_thread(
        _daily_report._extract_convs_for_date, target_date,
        owner_user_id=owner_user_id)
    if not convs:
        # Still create an empty report so manual tasks can be added
        empty_result = {
            'ok': True, 'tasks': [],
            'quote': random.choice(_daily_report._QUOTES),
            'persona': _daily_report._pick_persona({}),
            'stats': {'totalConversations': 0},
        }
        return api_ok(empty_result)

    # Manual-state preservation happens in the locked generated-report commit,
    # against the newest on-disk version after this potentially long analysis.
    # Off-loop: _run_llm_analysis inside is a synchronous LLM call.
    result = await asyncio.to_thread(
        _daily_report._analyse_conversations, convs, target_date,
        owner_user_id=owner_user_id, preserve_manual=False)

    # Persist if analysis succeeded
    if (result.get('streams') or result.get('tomorrow')) and not result.get('error'):
        result = _daily_report._save_generated_report(
            target_date, result, owner_user_id=owner_user_id)

    elapsed = time.monotonic() - t0
    stream_count = len(result.get('streams', []))
    done_count = sum(1 for s in result.get('streams', []) if s.get('status') == 'done')
    logger.info('[DailyReport] POST %s completed in %.1fs: %d convs → %d streams '
                '(%d done, %d open), error=%s',
                target_date, elapsed, len(convs), stream_count, done_count,
                stream_count - done_count, result.get('error', 'none'))
    return api_ok(result)


@api_v1_daily_report_bp.route('/api/v1/daily-report/<date_str>')
@require_auth
async def get_cached_report(date_str):
    """Get a previously generated report for a specific date."""
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    today_todos = _get_today_inherited_todos(
        date_str, owner_user_id=owner_user_id)

    report = _load_report(date_str, owner_user_id=owner_user_id)
    if report is None:
        # No report — check if previous day has tomorrow items to inherit
        if today_todos:
            logger.debug('[DailyReport] GET %s: no report, inheriting %d todos from prev day',
                         date_str, len(today_todos))
            # Off-loop: bounded legacy archives may still need JSON decoding.
            conv_count = await asyncio.to_thread(
                _daily_report._count_convs_for_date, date_str,
                owner_user_id=owner_user_id)
            return api_ok({
                'streams': [], 'tomorrow': [],
                'today_todos': today_todos,
                'tasks': [],
                'stats': {'totalConversations': conv_count},
                '_inherited': True,
                'quote': random.choice(_daily_report._QUOTES),
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
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    # Don't re-generate if cached
    existing = _load_report(date_str, owner_user_id=owner_user_id)
    if existing and (existing.get('streams') or existing.get('tasks')):
        n = len(existing.get('streams', existing.get('tasks', [])))
        logger.info('[DailyReport] Backfill %s: already cached (%d items)',
                     date_str, n)
        return api_ok({**existing})
    t0 = time.monotonic()
    # Off-loop: heavy messages scan/parse + synchronous LLM analysis (see POST).
    convs = await asyncio.to_thread(
        _daily_report._extract_convs_for_date, date_str,
        owner_user_id=owner_user_id)
    if not convs:
        return api_ok({'tasks': [],
                       'quote': random.choice(_daily_report._QUOTES),
                       'persona': _daily_report._pick_persona({}),
                       'stats': {'totalConversations': 0}})

    result = await asyncio.to_thread(
        _daily_report._analyse_conversations, convs, date_str,
        owner_user_id=owner_user_id, preserve_manual=False)

    if result.get('streams') and not result.get('error'):
        result = _daily_report._save_generated_report(
            date_str, result, owner_user_id=owner_user_id)

    elapsed = time.monotonic() - t0
    stream_count = len(result.get('streams', []))
    logger.info('[DailyReport] Backfill %s completed in %.1fs: %d convs → %d streams, error=%s',
                date_str, elapsed, len(convs), stream_count,
                result.get('error', 'none'))
    return api_ok(result)


@api_v1_daily_report_bp.route('/api/v1/daily-report/calendar/<int:year>/<int:month>')
@require_auth
async def get_calendar_month(year, month):
    """Month overview: which days have cached reports + task counts."""
    if month < 1 or month > 12:
        return api_bad_request('Invalid month')
    owner_user_id = _request_owner_user_id()

    prefix = f'{year:04d}-{month:02d}-'
    cache_key = (owner_user_id, year, month)
    cached = _daily_report._calendar_cache.get(cache_key)
    cache_fresh = cached is not None

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
            reports_dir = _daily_report._reports_dir_for_owner(
                owner_user_id=owner_user_id)
            for fname in os.listdir(reports_dir):
                if not (fname.startswith(prefix) and fname.endswith('.json')):
                    continue
                try:
                    day_num = int(fname[len(prefix):].replace('.json', ''))
                except ValueError as e:
                    logger.debug('[DailyReport] Skipping non-day filename %r: %s',
                                 fname, e)
                    continue
                report = _load_report(
                    fname.replace('.json', ''),
                    owner_user_id=owner_user_id)
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
            month_start = _dt.date(year, month, 1)
            if month < 12:
                month_end = _dt.date(year, month + 1, 1)
            else:
                month_end = _dt.date(year + 1, 1, 1)
            ms_start = int(_dt.datetime.combine(month_start, _dt.time.min).timestamp() * 1000)
            ms_end = int(_dt.datetime.combine(month_end, _dt.time.min).timestamp() * 1000)

            # Off-loop and authority-backed: Turn-native rows project only one
            # timestamp scalar; frozen archives decode in bounded Sidecar
            # batches, and transcript content never crosses this RPC.
            conv_days = await asyncio.to_thread(
                _daily_report._activity_counts_for_range, ms_start, ms_end,
                owner_user_id=owner_user_id,
                bound_created_end=True)
        except Exception as e:
            logger.warning('[DailyReport] Calendar conv-days %d-%02d: %s', year, month, e)

        # ── Server-side per-day cost calculation ──
        # _daily_report._get_monthly_costs does synchronous DB scans that can take several
        # seconds on a cache-cold month; run it in a worker thread so it does
        # NOT block the event loop (and every other in-flight request).
        cost_days = {}
        try:
            raw_costs = await asyncio.to_thread(
                _daily_report._get_monthly_costs, year, month,
                owner_user_id=owner_user_id)
            for day_num, day_data in raw_costs.items():
                cost_days[day_num] = {
                    'cost': day_data['cost'],
                    'conversations': day_data['conversations'],
                }
        except Exception as e:
            logger.warning('[DailyReport] Calendar cost calc %d-%02d: %s', year, month, e)

        # Store in cache (including the parsed per-day report summaries so
        # subsequent hits skip both the DB scans and the filesystem walk).
        _daily_report._calendar_cache.set(cache_key, {
            'days': days,
            'conv_days': conv_days,
            'cost_days': cost_days,
            'ts': time.monotonic(),
        })

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
        return api_error(f'Invalid status — must be one of {valid_statuses}', status=400)
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    def _apply(item) -> str:
        old_status = item.get('status', '?')
        resolved = _next_cycle_status(old_status) if cycle else new_status
        item['status'] = resolved
        item['_manual'] = True
        if resolved == 'done':
            item['remaining'] = None
        return old_status

    outcome = {'missing': False, 'found': False, 'status': new_status}

    def _mutate(report):
        if not report:
            outcome['missing'] = True
            return None
        # Try streams first, then legacy/manual tasks.
        for kind, items in (
                ('Stream', report.get('streams', [])),
                ('Task', report.get('tasks', []))):
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                matches = item.get('id') == item_id
                if kind == 'Task':
                    matches = matches or item.get('conv_id') == item_id
                if not matches:
                    continue
                old_status = _apply(item)
                outcome['status'] = item['status']
                outcome['found'] = True
                logger.info(
                    '[DailyReport] %s status updated %s: %s → %s (id=%s)',
                    kind, date_str, old_status, outcome['status'], item_id)
                return report
        return None

    _update_report(
        date_str, _mutate, owner_user_id=owner_user_id)
    if outcome['missing']:
        return api_not_found('No report for this date')
    if not outcome['found']:
        return api_not_found('Item not found in report')
    return api_ok({'status': outcome['status']})


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
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    outcome = {'missing': False, 'found': False}

    def _mutate(report):
        if not report:
            outcome['missing'] = True
            return None
        for item in report.get('tomorrow', []):
            if isinstance(item, dict) and item.get('id') == todo_id:
                item['done'] = bool(done)
                outcome['found'] = True
                return report
        return None

    _update_report(
        date_str, _mutate, owner_user_id=owner_user_id)
    if outcome['missing']:
        return api_not_found('No report for this date')
    if not outcome['found']:
        return api_not_found('TODO item not found')
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
    if not _daily_report._is_report_date(origin_date):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    outcome = {'missing': False, 'found': False}

    def _mutate(report):
        if not report:
            outcome['missing'] = True
            return None
        for item in report.get('tomorrow', []):
            if isinstance(item, dict) and item.get('id') == todo_id:
                item['done'] = bool(done)
                outcome['found'] = True
                return report
        return None

    _update_report(
        origin_date, _mutate, owner_user_id=owner_user_id)
    if outcome['missing']:
        return api_not_found('No report for origin date')
    if not outcome['found']:
        return api_not_found('TODO item not found in origin report')
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
    if not _daily_report._is_report_date(origin_date):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    outcome = {'missing': False, 'found': False}

    def _mutate(report):
        if not report:
            outcome['missing'] = True
            return None
        tomorrow = report.get('tomorrow', [])
        if not isinstance(tomorrow, list):
            return None
        new_tomorrow = [
            item for item in tomorrow
            if not (isinstance(item, dict) and item.get('id') == todo_id)
        ]
        if len(new_tomorrow) == len(tomorrow):
            return None
        outcome['found'] = True
        report['tomorrow'] = new_tomorrow
        return report

    _update_report(
        origin_date, _mutate, owner_user_id=owner_user_id)
    if outcome['missing']:
        return api_not_found('No report for origin date')
    if not outcome['found']:
        return api_not_found('TODO item not found in origin report')
    logger.info('[DailyReport] Inherited TODO deleted: origin=%s id=%s', origin_date, todo_id)
    return api_ok()


@api_v1_daily_report_bp.route('/api/v1/daily-report/conv-count/<date_str>')
@require_auth
async def get_conv_count(date_str):
    """Return the number of conversations with activity on a given date.

    Queries the database directly — reliable count regardless of frontend state.
    """
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    # Off-loop: Turn-native rows stay timestamp-only, but frozen archives still
    # require bounded Sidecar JSON decoding and can exceed the loop watchdog.
    count = await asyncio.to_thread(
        _daily_report._count_convs_for_date, date_str,
        owner_user_id=owner_user_id)
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
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    from lib.ids import short_id
    todo_id = short_id('todo-', 8)

    default_report = {
        'streams': [], 'tomorrow': [], 'tasks': [],
        'stats': {'totalConversations': 0},
        'quote': random.choice(_daily_report._QUOTES),
    }

    def _mutate(report):
        report.setdefault('tomorrow', []).append({
            'id': todo_id,
            'text': task_text[:60],
            'done': False,
            # Mark as user-authored so a report regeneration preserves it
            # (see lib/daily_report/todos.py::_merge_manual_state).
            '_manual': True,
        })
        return report

    report = _update_report(
        date_str, _mutate, owner_user_id=owner_user_id,
        default=default_report)
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
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    outcome = {'missing': False, 'found': False}

    def _mutate(report):
        if not report:
            outcome['missing'] = True
            return None
        tomorrow = report.get('tomorrow', [])
        if not isinstance(tomorrow, list):
            return None
        new_tomorrow = [
            item for item in tomorrow
            if not (isinstance(item, dict) and item.get('id') == task_id)
        ]
        if len(new_tomorrow) == len(tomorrow):
            return None
        outcome['found'] = True
        report['tomorrow'] = new_tomorrow
        return report

    report = _update_report(
        date_str, _mutate, owner_user_id=owner_user_id)
    if outcome['missing']:
        return api_not_found('No report for this date')
    if not outcome['found']:
        return api_not_found('TODO item not found')
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

    if not _daily_report._is_report_date(target_date):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    # Already generating?
    job = _daily_report._get_job(target_date, owner_user_id=owner_user_id)
    if job and job.get('status') == 'generating':
        logger.debug('[DailyReport] Generate %s: already running', target_date)
        return api_ok({'status': 'generating',
                       'progress': job.get('progress', {})})

    # Check cache (unless forced)
    if not force:
        existing = _load_report(
            target_date, owner_user_id=owner_user_id)
        if existing and (existing.get('streams') or existing.get('tasks')):
            return api_ok({'status': 'done', 'report': existing})
    # Launch background thread
    _daily_report._update_job(target_date, 'generating', owner_user_id=owner_user_id,
                progress={'stage': 'starting', 'message': '正在启动…'})
    t = threading.Thread(
        target=_daily_report._generate_in_background,
        args=(target_date, force),
        kwargs={'owner_user_id': owner_user_id},
        daemon=True,
        name=f'report-gen-{target_date}',
    )
    t.start()
    logger.info('[DailyReport] Background generation launched for %s (force=%s)',
                target_date, force)

    return api_ok({'status': 'generating',
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
    if not _daily_report._is_report_date(date_str):
        return api_bad_request('Invalid date format')
    owner_user_id = _request_owner_user_id()

    job = _daily_report._get_job(date_str, owner_user_id=owner_user_id)

    today_todos = _get_today_inherited_todos(
        date_str, owner_user_id=owner_user_id)

    if not job:
        # No active job — check disk cache
        existing = _load_report(date_str, owner_user_id=owner_user_id)
        if existing and (existing.get('streams') is not None or existing.get('tasks') is not None):
            existing['today_todos'] = today_todos
            return api_ok({'status': 'done', 'report': existing})
        # Check if previous day has inherited todos
        if today_todos:
            # Off-loop: bounded legacy archives may still need JSON decoding.
            conv_count = await asyncio.to_thread(
                _daily_report._count_convs_for_date, date_str,
                owner_user_id=owner_user_id)
            return api_ok({
                'status': 'done',
                'report': {
                    'streams': [], 'tomorrow': [], 'tasks': [],
                    'today_todos': today_todos,
                    'stats': {'totalConversations': conv_count},
                    '_inherited': True,
                    'quote': random.choice(_daily_report._QUOTES),
                },
            })
        return api_ok({'status': 'idle'})
    status = job.get('status', 'idle')

    if status == 'done':
        _daily_report._clear_job(date_str, owner_user_id=owner_user_id)
        report = _load_report(
            date_str, owner_user_id=owner_user_id) or {'tasks': []}
        report['today_todos'] = today_todos
        return api_ok({'status': 'done', 'report': report})
    if status == 'error':
        error_msg = job.get('error', 'Unknown error')
        _daily_report._clear_job(date_str, owner_user_id=owner_user_id)
        return api_ok({'status': 'error', 'error': error_msg})
    # Still generating
    return api_ok({'status': 'generating',
                   'progress': job.get('progress', {})})


__all__ = ['api_v1_daily_report_bp']
