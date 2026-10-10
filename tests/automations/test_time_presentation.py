"""Time output is independent of delivery time and disambiguates timezone folds."""
from datetime import datetime

import pytest

from lib.automations.errors import AutomationError
from lib.automations.presentation import show_task_start_time

pytestmark = pytest.mark.unit


def event(started):
    return {'kind':'task.started','occurredAt':started,'evidence':{'startedAt':started}}


def test_output_replays_exact_actual_start_and_explicit_zone(monkeypatch):
    monkeypatch.setattr('time.time',lambda:9876543210)
    started=datetime.fromisoformat('2026-10-04T00:00:00.123+00:00').timestamp()
    output=show_task_start_time({'timezone':'Asia/Shanghai'},event(started))
    assert output=={'startedAt':started,'timezone':'Asia/Shanghai',
        'text':'任务开始时间：2026-10-04T08:00:00.123+08:00 [Asia/Shanghai]'}
    assert show_task_start_time({'timezone':'Asia/Shanghai'},event(started))==output
    assert show_task_start_time({},event(started))['text'].endswith('+00:00 [UTC]')


def test_dst_fold_keeps_two_distinct_utc_offsets():
    first=datetime.fromisoformat('2024-11-03T05:30:00+00:00').timestamp()
    second=datetime.fromisoformat('2024-11-03T06:30:00+00:00').timestamp()
    one=show_task_start_time({'timezone':'America/New_York'},event(first))
    two=show_task_start_time({'timezone':'America/New_York'},event(second))
    assert '01:30:00.000-04:00' in one['text']
    assert '01:30:00.000-05:00' in two['text']
    assert one['startedAt']!=two['startedAt']


@pytest.mark.parametrize('bad',[{'kind':'schedule','occurredAt':100,'evidence':{'startedAt':100}},
    {'kind':'task.started','occurredAt':101,'evidence':{'startedAt':100}},
    {'kind':'task.started','occurredAt':100,'evidence':{}},event(True),event(float('nan')),
    event(float('inf')),event(-1),event(1e18),event(10**1000),
    {'kind':'task.started','occurredAt':True,'evidence':{'startedAt':1}}])
def test_missing_mismatched_or_unrepresentable_start_cannot_be_presented(bad):
    with pytest.raises(AutomationError):show_task_start_time({},bad)


@pytest.mark.parametrize('zone',['Not/A_Zone','../../etc/passwd'])
def test_invalid_timezone_is_a_typed_action_error(zone):
    with pytest.raises(AutomationError,match='IANA') as error:
        show_task_start_time({'timezone':zone},event(100))
    assert error.value.code=='invalid_action_timezone'
