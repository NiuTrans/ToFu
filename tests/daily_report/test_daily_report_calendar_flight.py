"""One in-flight calendar computation per (owner, year, month).

2026-11-08 P0: the calendar endpoint checked a 30 s TTL entry and, on a miss,
started a new storage scan for every GET. A scan that outlived the TTL made
every later GET miss too; one browser tab accumulated 249 concurrent scans of
the same month. Concurrent callers now await one shared computation: no cap,
no rejection, and a caller that goes away does not cancel the others' work.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

import lib.daily_report as daily_report_facade
from lib.daily_report import conversations, cost, storage

pytestmark = pytest.mark.unit
OWNER_USER_ID = 41
YEAR, MONTH = 2026, 11


class _Computation:
    """Blocking storage-scan doubles that count how often each one runs."""

    def __init__(self):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.cost_calls = 0
        self.activity_calls = 0

    def monthly_costs(self, year, month, *, owner_user_id):
        assert (year, month, owner_user_id) == (YEAR, MONTH, OWNER_USER_ID)
        with self.lock:
            self.cost_calls += 1
            number = self.cost_calls
        self.entered.set()
        assert self.release.wait(10), 'test never released the scan'
        return {3: {'cost': float(number),
                    'conversations': {'c': {'name': 'C', 'cost': float(number),
                                            'tokens': 7}}}}

    def activity_counts(self, ms_start, ms_end, *, owner_user_id,
                        bound_created_end=True):
        assert owner_user_id == OWNER_USER_ID and bound_created_end
        with self.lock:
            self.activity_calls += 1
        return {3: 1}


@pytest.fixture
def computation(monkeypatch, tmp_path):
    scan = _Computation()
    monkeypatch.setattr(storage, '_REPORTS_DIR', str(tmp_path))
    for owner in (cost, daily_report_facade):
        monkeypatch.setattr(owner, '_get_monthly_costs', scan.monthly_costs,
                            raising=False)
    for owner in (conversations, daily_report_facade):
        monkeypatch.setattr(owner, '_activity_counts_for_range',
                            scan.activity_counts, raising=False)
    cost._calendar_cache.clear()
    yield scan
    scan.release.set()
    cost._calendar_cache.clear()


async def _until(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, 'condition never became true'
        await asyncio.sleep(0.005)


def test_concurrent_overviews_share_one_computation(computation):
    from lib.daily_report import calendar_overview as calendar

    async def scenario():
        callers = [asyncio.create_task(calendar.month_overview(
            YEAR, MONTH, owner_user_id=OWNER_USER_ID)) for _ in range(16)]
        await _until(computation.entered.is_set)
        await asyncio.sleep(0.05)
        computation.release.set()
        return await asyncio.gather(*callers)

    results = asyncio.run(scenario())

    assert computation.cost_calls == 1
    assert computation.activity_calls == 1
    assert all(result == results[0] for result in results)
    assert results[0]['cost_days'][3]['cost'] == 1.0
    assert results[0]['conv_days'] == {3: 1}
    # The finished computation is cached; the next caller does no scan.
    again = asyncio.run(calendar.month_overview(
        YEAR, MONTH, owner_user_id=OWNER_USER_ID))
    assert again['cost_days'] == results[0]['cost_days']
    assert computation.cost_calls == 1


def test_departed_caller_does_not_cancel_the_shared_computation(computation):
    from lib.daily_report import calendar_overview as calendar

    async def scenario():
        departed = asyncio.create_task(calendar.month_overview(
            YEAR, MONTH, owner_user_id=OWNER_USER_ID))
        staying = asyncio.create_task(calendar.month_overview(
            YEAR, MONTH, owner_user_id=OWNER_USER_ID))
        await _until(computation.entered.is_set)
        departed.cancel()
        computation.release.set()
        with pytest.raises(asyncio.CancelledError):
            await departed
        return await staying

    result = asyncio.run(scenario())

    assert computation.cost_calls == 1
    assert result['cost_days'][3]['cost'] == 1.0
    assert cost._calendar_cache.get(
        (OWNER_USER_ID, YEAR, MONTH))['cost_days'] == result['cost_days']


def test_invalidation_during_a_computation_starts_a_fresh_one(computation):
    from lib.daily_report import calendar_overview as calendar

    async def scenario():
        before = asyncio.create_task(calendar.month_overview(
            YEAR, MONTH, owner_user_id=OWNER_USER_ID))
        await _until(computation.entered.is_set)
        # A report save for this month invalidates the calendar entry.
        storage._invalidate_calendar(
            f'{YEAR:04d}-{MONTH:02d}-03', owner_user_id=OWNER_USER_ID)
        after = asyncio.create_task(calendar.month_overview(
            YEAR, MONTH, owner_user_id=OWNER_USER_ID))
        await _until(lambda: computation.cost_calls == 2)
        computation.release.set()
        return await before, await after

    before, after = asyncio.run(scenario())

    assert computation.cost_calls == 2
    assert {before['cost_days'][3]['cost'],
            after['cost_days'][3]['cost']} == {1.0, 2.0}
    # Only the computation started after the invalidation is cached.
    cached = cost._calendar_cache.get((OWNER_USER_ID, YEAR, MONTH))
    assert cached['cost_days'] == after['cost_days']


def test_failed_cost_scan_settles_every_caller(computation, monkeypatch):
    from lib.daily_report import calendar_overview as calendar

    def unavailable(*_args, **_kwargs):
        computation.entered.set()
        assert computation.release.wait(10)
        raise RuntimeError('authority unavailable')

    monkeypatch.setattr(cost, '_get_monthly_costs', unavailable)

    async def scenario():
        callers = [asyncio.create_task(calendar.month_overview(
            YEAR, MONTH, owner_user_id=OWNER_USER_ID)) for _ in range(4)]
        await _until(computation.entered.is_set)
        computation.release.set()
        return await asyncio.gather(*callers)

    results = asyncio.run(scenario())

    assert [result['cost_days'] for result in results] == [{}] * 4
    assert results[0]['conv_days'] == {3: 1}
    assert computation.activity_calls == 1


def test_concurrent_calendar_gets_run_one_computation(computation, monkeypatch):
    from quart import Quart

    import routes.api_v1.auth as auth
    from routes.api_v1 import daily_report as route

    owners = []

    def owner():
        owners.append(OWNER_USER_ID)
        return OWNER_USER_ID

    monkeypatch.setattr(
        auth, 'current_auth', lambda: SimpleNamespace(is_authenticated=True))
    monkeypatch.setattr(route, 'request_user_id', owner)
    app = Quart(__name__)
    app.register_blueprint(route.api_v1_daily_report_bp)

    async def scenario():
        client = app.test_client()
        requests = [asyncio.create_task(client.get(
            f'/api/v1/daily-report/calendar/{YEAR}/{MONTH}'))
            for _ in range(8)]
        await _until(lambda: len(owners) == 8 and computation.entered.is_set())
        await asyncio.sleep(0.2)
        computation.release.set()
        responses = await asyncio.gather(*requests)
        return [(response.status_code, await response.get_json())
                for response in responses]

    answers = asyncio.run(scenario())

    assert computation.cost_calls == 1
    assert computation.activity_calls == 1
    assert {status for status, _body in answers} == {200}
    bodies = [body for _status, body in answers]
    for body in bodies:
        body.pop('request_id', None)
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]['cost_days'] == {
        '3': {'cost': 1.0,
              'conversations': {'c': {'name': 'C', 'cost': 1.0, 'tokens': 7}}}}
    assert bodies[0]['conv_days'] == {'3': 1}
