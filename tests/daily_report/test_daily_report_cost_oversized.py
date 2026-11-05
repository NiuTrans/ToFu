"""Calendar costs over transcripts too large for one storage response.

The cost scan reads a refused oversized transcript back to its range start
through the repository time-window contract and must aggregate exactly what
whole hydration would. A transcript it cannot read fails the scan before any
day is persisted: never a silently empty month.
"""

from __future__ import annotations

import datetime as dt

import pytest

from lib.daily_report import cost
from lib.storage.errors import StorageError
from tests.daily_report.test_daily_report_oversized_transcript import (
    OWNER_USER_ID,
    _RESPONSE_REFUSALS,
    _Storage,
    _use_storage,
)

pytestmark = pytest.mark.unit


def _at(day: dt.date, hour: int, minute: int = 0) -> int:
    return int(dt.datetime.combine(day, dt.time(hour, minute)).timestamp()
               * 1000)


def _message(timestamp, *, tokens=0, size=1):
    message = {'role': 'assistant', 'content': f'at {timestamp}',
               'timestamp': timestamp, 'size': size}
    if tokens:
        message['usage'] = {'prompt_tokens': tokens, 'completion_tokens': 0}
    return message


def _transcripts(month_start: dt.date) -> dict[str, list[dict]]:
    before = month_start - dt.timedelta(days=1)
    day = month_start.replace

    return {
        'ordinary': [
            _message(_at(before, 10), tokens=10),
            _message(_at(day(day=2), 10), tokens=20),
            _message(_at(day(day=3), 10), tokens=30),
        ],
        'oversized': [
            _message(_at(month_start - dt.timedelta(days=11), 9)),
            _message(_at(before, 23), tokens=100, size=6),
            _message(_at(day(day=2), 9)),
            _message(_at(day(day=2), 9, 5), tokens=200, size=6),
            # Untimestamped usage is placed by its position in the whole
            # transcript, which a window must preserve.
            _message(0, tokens=300),
            _message(_at(day(day=5), 18), tokens=400),
            _message(_at(day(day=25), 8), tokens=500),
        ],
    }


@pytest.fixture(autouse=True)
def _token_priced_costs(monkeypatch):
    monkeypatch.setattr(
        cost, '_calc_msg_cost_cny',
        lambda usage, *_args, **_kwargs: usage['prompt_tokens'] / 100)


def _scan(monkeypatch, storage, month_start):
    _use_storage(monkeypatch, storage)
    return cost._scan_costs_in_range(
        _at(month_start, 0), _at(month_start.replace(day=20), 0),
        owner_user_id=OWNER_USER_ID)


@pytest.mark.parametrize('refusal', sorted(_RESPONSE_REFUSALS))
def test_oversized_conversation_costs_match_full_hydration(
        monkeypatch, refusal):
    month_start = dt.date(2026, 8, 1)
    updated_day = month_start.replace(day=31)
    expected = _scan(monkeypatch, _Storage(
        _transcripts(month_start), budget=10**9, day=updated_day),
        month_start)
    limited = _Storage(_transcripts(month_start), budget=8,
                       refusal=refusal, day=updated_day)

    got = _scan(monkeypatch, limited, month_start)

    assert got == expected
    assert {day: data['cost'] for day, data in got.items()} == {
        2: 2.2, 3: 0.3, 5: 4.0, 17: 3.0}
    assert set(got[2]['conversations']) == {'ordinary', 'oversized'}
    # The window stops at the newest message before the range.
    assert min(start for start, _ in limited.accepted_windows) == 1
    assert all(end - start < 7 for start, end in limited.accepted_windows)


def test_unreadable_oversized_transcript_fails_the_month_and_persists_nothing(
        monkeypatch):
    this_month = dt.date.today().replace(day=1)
    month_start = (this_month - dt.timedelta(days=40)).replace(day=1)
    transcripts = _transcripts(month_start)
    transcripts['oversized'][3]['size'] = 9
    _use_storage(monkeypatch, _Storage(
        transcripts, budget=8, day=month_start.replace(day=27)))
    persisted = []
    monkeypatch.setattr(
        cost, '_load_cached_day_costs', lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cost, '_persist_day_cost',
        lambda date_str, *_args, **_kwargs: persisted.append(date_str))

    with pytest.raises(StorageError) as caught:
        cost._get_monthly_costs(
            month_start.year, month_start.month, owner_user_id=OWNER_USER_ID)

    assert caught.value.message == 'database_resource_exhausted'
    assert persisted == []
