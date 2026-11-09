"""My Day calendar month overview, one shared computation per owner month.

Responsibility
--------------
Build the ``GET /daily-report/calendar/<year>/<month>`` payload (per-day
report summaries, active-conversation counts and costs) and run at most one
computation per ``(owner, year, month)`` at a time. Concurrent callers await
the same in-flight work, a caller that goes away does not cancel it, and the
finished overview is published to ``cost._calendar_cache`` for its TTL. A
scan can outlive that TTL; without sharing, every poll during it would start
another scan of the same month.

Entry points
------------
``month_overview(year, month, *, owner_user_id)`` — async, serving loop only.
``invalidate(owner_user_id, year=None, month=None)`` — any thread. Drops the
cached overview and detaches in-flight computations: their own callers still
receive them, but they are never cached or joined again.

Dependencies
------------
``cost`` (TTL cache, ``_get_monthly_costs``), ``conversations``
(``_activity_counts_for_range``) and ``storage`` (report files), all called
through their modules so tests can substitute them. Blocking work runs in the
loop's default executor.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import threading
import time

from lib.identity import require_user_id
from lib.log import get_logger

from . import conversations, cost, storage

logger = get_logger(__name__)


class _Flight:
    """One in-flight overview computation and the loop that owns it."""

    __slots__ = ('loop', 'task', 'detached')

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.task: asyncio.Task | None = None
        self.detached = False


# Guards _flights and the publish/invalidate ordering of cost._calendar_cache.
_flights_lock = threading.Lock()
_flights: dict[tuple[int, int, int], _Flight] = {}


def _month_bounds_ms(year: int, month: int) -> tuple[int, int]:
    month_start = _dt.date(year, month, 1)
    month_end = (_dt.date(year + 1, 1, 1) if month == 12
                 else _dt.date(year, month + 1, 1))
    return (
        int(_dt.datetime.combine(month_start, _dt.time.min).timestamp() * 1000),
        int(_dt.datetime.combine(month_end, _dt.time.min).timestamp() * 1000),
    )


def _scan_report_days(year: int, month: int, *, owner_user_id: int) -> dict:
    """Summarize task completion for one month of cached report files."""
    prefix = f'{year:04d}-{month:02d}-'
    reports_dir = storage._reports_dir_for_owner(owner_user_id=owner_user_id)
    days = {}
    try:
        for fname in os.listdir(reports_dir):
            if not (fname.startswith(prefix) and fname.endswith('.json')):
                continue
            try:
                day_num = int(fname[len(prefix):].replace('.json', ''))
            except ValueError as e:
                logger.debug('[DailyReport] Skipping non-day filename %r: %s',
                             fname, e)
                continue
            report = storage._load_report(
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
            date_key = f'{prefix}{day_num:02d}'
            days[date_key] = {
                'total': total,
                'done': done,
                'incomplete': total - done,
            }
    except Exception as e:
        logger.warning('[DailyReport] Calendar scan %s: %s', prefix, e)
    return days


async def _compute(key: tuple[int, int, int], flight: _Flight) -> dict:
    owner_id, year, month = key
    try:
        days = await asyncio.to_thread(
            _scan_report_days, year, month, owner_user_id=owner_id)
        conv_days = {}
        try:
            ms_start, ms_end = _month_bounds_ms(year, month)
            conv_days = await asyncio.to_thread(
                conversations._activity_counts_for_range, ms_start, ms_end,
                owner_user_id=owner_id, bound_created_end=True)
        except Exception as e:
            logger.warning('[DailyReport] Calendar conv-days %d-%02d: %s',
                           year, month, e)
        cost_days = {}
        try:
            raw_costs = await asyncio.to_thread(
                cost._get_monthly_costs, year, month, owner_user_id=owner_id)
            cost_days = {
                day_num: {'cost': day_data['cost'],
                          'conversations': day_data['conversations']}
                for day_num, day_data in raw_costs.items()
            }
        except Exception as e:
            logger.warning('[DailyReport] Calendar cost calc %d-%02d: %s',
                           year, month, e)
        overview = {'days': days, 'conv_days': conv_days,
                    'cost_days': cost_days, 'ts': time.monotonic()}
        with _flights_lock:
            if not flight.detached:
                cost._calendar_cache.set(key, overview)
        return overview
    finally:
        with _flights_lock:
            if _flights.get(key) is flight:
                del _flights[key]


def _retrieve_outcome(task: asyncio.Task) -> None:
    # Every caller may have gone away; consume the outcome so a failure is
    # not reported as never retrieved.
    if not task.cancelled():
        task.exception()


async def month_overview(year: int, month: int, *, owner_user_id) -> dict:
    """Return ``{days, conv_days, cost_days, ts}`` for one owner month."""
    owner_id = require_user_id(owner_user_id, context='daily report calendar')
    key = (owner_id, year, month)
    loop = asyncio.get_running_loop()
    with _flights_lock:
        cached = cost._calendar_cache.get(key)
        if cached is not None:
            return cached
        flight = _flights.get(key)
        # A flight owned by another (closed) loop cannot be awaited here.
        if flight is None or flight.loop is not loop:
            flight = _Flight(loop)
            flight.task = loop.create_task(_compute(key, flight))
            flight.task.add_done_callback(_retrieve_outcome)
            _flights[key] = flight
    return await asyncio.shield(flight.task)


def invalidate(owner_user_id, year: int | None = None,
               month: int | None = None) -> None:
    """Forget one owner's cached overview (one month, or all months)."""
    owner_id = require_user_id(
        owner_user_id, context='daily report calendar invalidation')

    def matches(key):
        return key[0] == owner_id and (year is None or key[1:] == (year, month))

    with _flights_lock:
        for key in [key for key in _flights if matches(key)]:
            _flights.pop(key).detached = True
        for key in [key for key in list(cost._calendar_cache._data)
                    if matches(key)]:
            cost._calendar_cache.invalidate(key)
