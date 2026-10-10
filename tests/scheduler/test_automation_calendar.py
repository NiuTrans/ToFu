"""UTC slot identity, sparse calendars, timezone transitions and bounded catch-up."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from lib.scheduler.calendar import CronSchedule

pytestmark = pytest.mark.unit


def utc(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def test_slots_are_strictly_next_and_latest_is_upper_inclusive():
    calendar = CronSchedule('*/5 * * * *', 'Asia/Shanghai')
    start = utc('2026-10-03T03:00:00Z')
    assert calendar.next_after(start) == start + timedelta(minutes=5)
    assert calendar.next_after(start - timedelta(microseconds=1)) == start
    assert calendar.latest_between(start, start + timedelta(minutes=20)) == start + timedelta(minutes=20)
    assert calendar.latest_between(start, start) is None


@pytest.mark.parametrize('expression', ['0 0 31 2 *', '0 0 31 4,6,9,11 *'])
def test_impossible_month_day_combination_is_rejected(expression):
    with pytest.raises(ValueError, match='no possible calendar date'):
        CronSchedule(expression, 'UTC')


def test_leap_day_search_crosses_a_non_leap_year_and_century():
    calendar = CronSchedule('0 0 29 2 *', 'UTC')
    assert calendar.next_after(utc('2025-03-01T00:00:00Z')) == utc('2028-02-29T00:00:00Z')
    assert calendar.next_after(utc('2096-03-01T00:00:00Z')) == utc('2104-02-29T00:00:00Z')
    assert calendar.latest_between(utc('2096-03-01T00:00:00Z'), utc('2105-01-01T00:00:00Z')) == utc('2104-02-29T00:00:00Z')


def test_day_of_month_and_weekday_use_shared_scheduler_and_semantics():
    calendar = CronSchedule('0 0 29 2 1', 'UTC')
    assert calendar.next_after(utc('2016-02-29T00:00:00Z')) == utc('2044-02-29T00:00:00Z')


def test_nonexistent_dst_minute_is_skipped_without_normalizing_a_slot():
    calendar = CronSchedule('30 2 * * *', 'America/New_York')
    assert calendar.next_after(utc('2026-03-08T00:00:00Z')) == utc('2026-03-09T06:30:00Z')
    assert calendar.latest_between(utc('2026-03-08T00:00:00Z'), utc('2026-03-08T23:59:00Z')) is None


def test_repeated_dst_minute_has_two_distinct_utc_slots():
    calendar = CronSchedule('30 1 * * *', 'America/New_York')
    first, second = utc('2026-11-01T05:30:00Z'), utc('2026-11-01T06:30:00Z')
    assert calendar.next_after(first - timedelta(seconds=1)) == first
    assert calendar.next_after(first) == second
    assert calendar.next_after(second) == utc('2026-11-02T06:30:00Z')
    assert calendar.latest_between(first, second) == second
    assert first.astimezone(ZoneInfo('America/New_York')).fold == 0
    assert second.astimezone(ZoneInfo('America/New_York')).fold == 1


def test_repeated_hour_queries_compare_utc_instead_of_local_minute_order():
    calendar = CronSchedule('15,45 1 * * *', 'America/New_York')
    assert calendar.next_after(utc('2026-11-01T05:50:00Z')) == utc('2026-11-01T06:15:00Z')
    assert calendar.latest_between(utc('2026-11-01T05:00:00Z'), utc('2026-11-01T06:20:00Z')) == utc('2026-11-01T06:15:00Z')


def test_half_hour_transition_keeps_both_folds_and_skips_gap():
    fold = CronSchedule('45 1 * * *', 'Australia/Lord_Howe')
    first, second = utc('2026-04-04T14:45:00Z'), utc('2026-04-04T15:15:00Z')
    assert fold.next_after(first - timedelta(seconds=1)) == first
    assert fold.next_after(first) == second
    gap = CronSchedule('15 2 * * *', 'Australia/Lord_Howe')
    assert gap.next_after(utc('2026-10-03T16:00:00Z')) == utc('2026-10-04T15:15:00Z')


def test_date_line_gap_does_not_invent_an_occurrence():
    calendar = CronSchedule('0 0 30 12 *', 'Pacific/Apia')
    assert calendar.next_after(utc('2011-12-29T00:00:00Z')) == utc('2012-12-29T10:00:00Z')


def test_latest_catch_up_selects_one_slot_without_replaying_downtime():
    calendar = CronSchedule('* * * * *', 'UTC')
    after, now = utc('1970-01-01T00:00:00Z'), utc('2099-12-31T23:59:42Z')
    assert calendar.due_slot(after, now, catch_up='latest') == utc('2099-12-31T23:59:00Z')
    assert calendar.next_after(now) == utc('2100-01-01T00:00:00Z')


def test_skip_ignores_old_slots_but_allows_current_minute_poll_latency():
    calendar = CronSchedule('0 * * * *', 'UTC')
    after = utc('2026-10-03T01:00:00Z')
    assert calendar.due_slot(after, utc('2026-10-03T05:00:59Z'), catch_up='skip') == utc('2026-10-03T05:00:00Z')
    assert calendar.due_slot(after, utc('2026-10-03T05:01:00Z'), catch_up='skip') is None
    assert calendar.due_slot(after, utc('2026-10-03T05:01:00Z'), catch_up='latest') == utc('2026-10-03T05:00:00Z')


@pytest.mark.parametrize('expression,zone,start,hours', [
    ('*/7 0-3 * * *','UTC','2026-10-03T00:00:00Z',30),
    ('15,45 1 * * *','America/New_York','2026-11-01T05:20:00Z',4),
    ('15 2 * * *','Australia/Lord_Howe','2026-10-03T13:00:00Z',30),
])
def test_calendar_matches_independent_utc_minute_oracle(expression,zone,start,hours):
    from lib.scheduler.cron import cron_matches
    calendar = CronSchedule(expression, zone)
    lower = utc(start)
    upper = lower + timedelta(hours=hours)
    eligible = []
    slot = lower.replace(second=0,microsecond=0) + timedelta(minutes=1)
    while slot <= upper:
        if cron_matches(expression,slot.astimezone(ZoneInfo(zone))):
            eligible.append(slot)
        slot += timedelta(minutes=1)
    assert calendar.next_after(lower) == eligible[0]
    assert calendar.latest_between(lower,upper) == eligible[-1]


def test_queries_reject_naive_clocks_regressed_intervals_and_unknown_policies():
    calendar = CronSchedule('* * * * *', 'UTC')
    with pytest.raises(ValueError, match='timezone-aware'):
        calendar.next_after(datetime(2026,1,1))
    instant = utc('2026-01-01T00:00:00Z')
    with pytest.raises(ValueError, match='regressed'):
        calendar.latest_between(instant,instant-timedelta(seconds=1))
    with pytest.raises(ValueError, match='catch-up'):
        calendar.due_slot(instant,instant+timedelta(hours=1),catch_up='all')


def test_finite_datetime_range_has_an_explicit_exhausted_result():
    calendar = CronSchedule('0 0 1 1 *', 'UTC')
    assert calendar.next_after(datetime.max.replace(tzinfo=timezone.utc)) is None


def test_historical_second_offset_keeps_exact_utc_identity_and_skip_minute():
    calendar = CronSchedule('0 12 * * *', 'Europe/Paris')
    start = utc('1880-10-03T00:00:00Z')
    slot = utc('1880-10-03T11:50:39Z')
    assert calendar.next_after(start) == slot
    assert calendar.due_slot(start,slot+timedelta(seconds=5),catch_up='skip') == slot
    assert calendar.due_slot(start,slot+timedelta(minutes=1),catch_up='skip') is None


def test_date_line_fold_retains_two_slots_a_day_apart():
    zone = ZoneInfo('Pacific/Apia')
    wall = datetime(1892,7,4,12,tzinfo=zone)
    first = wall.replace(fold=0).astimezone(timezone.utc)
    second = wall.replace(fold=1).astimezone(timezone.utc)
    assert second-first == timedelta(days=1)
    calendar = CronSchedule('0 12 4 7 *', 'Pacific/Apia')
    assert calendar.next_after(first-timedelta(seconds=1)) == first
    assert calendar.next_after(first) == second
    assert calendar.latest_between(first,second) == second


@pytest.mark.parametrize('zone',['UTC','Pacific/Kiritimati','America/New_York'])
def test_next_slot_at_utc_year_limit_does_not_overflow_local_conversion(zone):
    calendar = CronSchedule('0 0 1 1 *',zone)
    assert calendar.next_after(datetime.max.replace(tzinfo=timezone.utc)) is None
